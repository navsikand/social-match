"""Learned interest similarity: a sentence encoder on the server, cached in `.cache/`.

What it does
------------
At startup (in a background thread, so the server answers requests immediately) the server loads a
small sentence encoder, downloading it into `ENCODER_CACHE_DIR` (default `./.cache`) the first time
and reusing the cached copy after that. It then embeds our 34 interest tags once and keeps the
vectors in memory. Matching never calls the model during a request: two canonical tags are compared
by a dot product of stored vectors, mapped to a 0..1 similarity.

  cosine(a, b)  ->  sigmoid(slope * cosine + intercept)  ->  similarity in [0, 0.95]

Raw cosine values mean different things for different models (some compress everything into
0.4..0.9), so the mapping is CALIBRATED each time a model loads: a two-parameter logistic fit on a
probe set of tag pairs we judged related or unrelated (the hand-made taxonomy in `taxonomy.py`, plus
a few free-text word pairs). That makes the thresholds model-agnostic.

Safety rails
------------
* QUALITY GATE: if the encoder cannot tell our related pairs from unrelated ones (AUC below
  `ENCODER_MIN_AUC`, default 0.80) it is switched off and the hand-made taxonomy keeps scoring.
  A bad download or the wrong model can therefore never silently degrade matching.
* FALLBACK LADDER: encoder -> taxonomy -> exact tag. Not installed, not downloaded yet, load error,
  failed gate, or `ENCODER_ENABLED=0` all land on the taxonomy and log one line saying why.
* Cards record which scorer produced them (`reasons.similarity_model`), so a score is never
  re-derived under a different model after the fact.
* Nothing student-written is logged, and no vector is ever returned by any endpoint.

Install: `uv sync --extra encoder` (pulls sentence-transformers and torch; uses the GPU when torch
can see one). Check a real model without starting the server: `uv run python -m server.encoder`.
"""
from __future__ import annotations

import dataclasses
import math
import os
import threading
import time
from typing import Callable

from server import config, taxonomy
from server import logging as smlog
from server.schema import TAG_VOCAB

log = smlog.get("encoder")

MAX_SIM = 0.95          # a learned similarity never reaches an exact tag match (1.0)
MIN_SIM = 0.30          # below this a pair counts as unrelated (0.0): keeps weak noise out of scores
RELATED_AT = 0.5        # at or above this, a pair is explained on the card as "related"
WORD_CACHE_MAX = 2048

# Free-text pairs the encoder must also get right: (word, tag-or-word, related?).
EXTRA_PROBES: tuple[tuple[str, str, int], ...] = (
    ("crimping", "climbing", 1),
    ("matcha", "coffee", 1),
    ("espresso", "coffee", 1),
    ("trail running", "hiking", 1),
    ("jogging", "running", 1),
    ("weightlifting", "gym", 1),
    ("pickup basketball", "basketball", 1),
    ("watercolor", "painting", 1),
    ("sketching", "drawing", 1),
    ("ramen", "cooking", 1),
    ("sourdough", "baking", 1),
    ("synthwave", "edm", 1),
    ("mycology", "chess", 0),
    ("accounting", "hiking", 0),
    ("knitting", "soccer", 0),
    ("tax law", "painting", 0),
    ("astronomy", "swimming", 0),
    ("welding", "jazz", 0),
)

Embedder = Callable[[list[str]], list[list[float]]]


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """One immutable, fully-calibrated encoder state. Swapped in atomically when ready."""

    model: str
    device: str
    vectors: dict[str, tuple[float, ...]]   # canonical tag -> unit vector
    slope: float
    intercept: float
    auc: float
    pairs: int
    template: str

    def similarity(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        va, vb = self.vectors.get(a), self.vectors.get(b)
        if va is None or vb is None:
            return 0.0
        return _to_similarity(_dot(va, vb), self.slope, self.intercept)


_lock = threading.Lock()
_status = {"state": "idle", "reason": "not started"}
_snapshot: Snapshot | None = None
_embed: Embedder | None = None
_embed_lock = threading.Lock()
_word_cache: dict[str, tuple[float, ...]] = {}
_thread: threading.Thread | None = None


# --------------------------------------------------------------------------- small maths


def _dot(a, b) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def _unit(vec) -> tuple[float, ...]:
    norm = math.sqrt(math.fsum(x * x for x in vec))
    if norm == 0:
        return tuple(float(x) for x in vec)
    return tuple(float(x) / norm for x in vec)


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    ez = math.exp(max(z, -60.0))
    return ez / (1.0 + ez)


def _to_similarity(cosine: float, slope: float, intercept: float) -> float:
    p = _sigmoid(slope * cosine + intercept)
    return min(MAX_SIM, p) if p >= MIN_SIM else 0.0


def auc(scores: list[float], labels: list[int]) -> float:
    """Probability a random related pair outscores a random unrelated one (ties count half)."""
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    if not pos or not neg:
        return 0.5
    ranked = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(ranked):
        j = i
        while j + 1 < len(ranked) and scores[ranked[j + 1]] == scores[ranked[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[ranked[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    rank_sum = sum(r for r, y in zip(ranks, labels) if y == 1)
    return (rank_sum - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))


def fit_logistic(xs: list[float], ys: list[int]) -> tuple[float, float]:
    """Class-balanced logistic regression y ~ sigmoid(slope*x + intercept), Newton steps, ridge on slope."""
    n = len(xs)
    n_pos = sum(ys)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        raise ValueError("calibration needs both related and unrelated pairs")
    w_pos, w_neg = n / (2.0 * n_pos), n / (2.0 * n_neg)
    slope, intercept, ridge = 1.0, 0.0, 1e-3
    for _ in range(200):
        g_s = g_i = h_ss = h_si = h_ii = 0.0
        for x, y in zip(xs, ys):
            w = w_pos if y == 1 else w_neg
            p = _sigmoid(slope * x + intercept)
            r = w * (p - y)
            s = w * p * (1.0 - p)
            g_s += r * x
            g_i += r
            h_ss += s * x * x
            h_si += s * x
            h_ii += s
        g_s += ridge * slope
        h_ss += ridge
        det = h_ss * h_ii - h_si * h_si
        if abs(det) < 1e-12:
            break
        d_s = (h_ii * g_s - h_si * g_i) / det
        d_i = (-h_si * g_s + h_ss * g_i) / det
        # Damp wild steps (near-separable data) so the fit stays finite.
        scale = max(1.0, abs(d_s) / 20.0, abs(d_i) / 20.0)
        slope -= d_s / scale
        intercept -= d_i / scale
        if max(abs(d_s), abs(d_i)) / scale < 1e-9:
            break
    return slope, intercept


# --------------------------------------------------------------------------- probe set


def _label(tag: str) -> str:
    return tag.replace("_", " ")


def probe_pairs() -> list[tuple[str, str, int]]:
    """(text_a, text_b, related?) from the taxonomy's own judgments plus the free-text extras.

    Pairs the taxonomy rates only 'compatible' (0.2) are left out: they are neither clearly
    related nor clearly unrelated, so they must not pull the calibration either way.
    """
    out: list[tuple[str, str, int]] = []
    for i, a in enumerate(TAG_VOCAB):
        for b in TAG_VOCAB[i + 1:]:
            s = taxonomy.sim(a, b)
            if s >= taxonomy.SIM_DOMAIN:
                out.append((_label(a), _label(b), 1))
            elif s == 0.0:
                out.append((_label(a), _label(b), 0))
    out.extend(EXTRA_PROBES)
    return out


# --------------------------------------------------------------------------- loading


class EncoderUnavailable(Exception):
    """The encoder cannot be used right now; `args[0]` is the one-line reason that gets logged."""


def _resolve_device(setting: str) -> str:
    if setting != "auto":
        return setting
    try:
        import torch  # type: ignore[import-not-found]

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _load_sentence_transformer() -> tuple[Embedder, str]:
    """Load the configured model into the cache dir. Raises EncoderUnavailable with a reason."""
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except Exception as exc:  # ImportError, or a broken torch install
        raise EncoderUnavailable(
            f"sentence-transformers is not installed ({type(exc).__name__}); run `uv sync --extra encoder`"
        ) from exc

    name = config.encoder_model()
    device = _resolve_device(config.encoder_device())
    cache = os.path.join(os.path.abspath(config.encoder_cache_dir()), "models")
    os.makedirs(cache, exist_ok=True)
    started = time.monotonic()
    try:
        try:
            # Warm cache first: works with no network and never re-downloads.
            model = SentenceTransformer(name, cache_folder=cache, device=device, local_files_only=True)
            source = "cache"
        except Exception:
            model = SentenceTransformer(name, cache_folder=cache, device=device)
            source = "download"
    except Exception as exc:
        raise EncoderUnavailable(f"could not load model {name!r} ({type(exc).__name__})") from exc
    log.info("model=%s device=%s source=%s seconds=%.1f cache=%s", name, device, source,
             time.monotonic() - started, cache)

    def embed(texts: list[str]) -> list[list[float]]:
        vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True,
                            show_progress_bar=False, batch_size=64)
        return [v.tolist() for v in vecs]

    return embed, device


def load(embedder: Embedder | None = None, *, device: str = "test", model_name: str | None = None) -> bool:
    """Load, calibrate and (if good enough) activate an encoder. Returns True when it is ready.

    Synchronous. The server calls it from a background thread; tests pass a fake `embedder`.
    Never raises: every failure is a status plus one log line, and matching falls back.
    """
    global _snapshot, _embed
    name = model_name or config.encoder_model()
    with _lock:
        _status.update(state="loading", reason="loading")
    try:
        if embedder is None:
            embedder, device = _load_sentence_transformer()
        template = config.encoder_template()
        pairs = probe_pairs()
        texts = sorted({template.format(t) for a, b, _ in pairs for t in (a, b)}
                       | {template.format(_label(t)) for t in TAG_VOCAB})
        raw = embedder(texts)
        if len(raw) != len(texts):
            raise EncoderUnavailable("encoder returned the wrong number of vectors")
        by_text = {t: _unit(v) for t, v in zip(texts, raw)}
        cosines = [_dot(by_text[template.format(a)], by_text[template.format(b)]) for a, b, _ in pairs]
        labels = [y for _, _, y in pairs]
        quality = auc(cosines, labels)
        min_auc = config.encoder_min_auc()
        if quality < min_auc:
            raise EncoderUnavailable(
                f"quality gate failed: AUC {quality:.3f} < {min_auc:.2f} on {len(pairs)} probe pairs"
            )
        slope, intercept = fit_logistic(cosines, labels)
        if slope <= 0:
            raise EncoderUnavailable("calibration failed: cosine does not increase with relatedness")
        snap = Snapshot(
            model=name,
            device=device,
            vectors={t: by_text[template.format(_label(t))] for t in TAG_VOCAB},
            slope=slope,
            intercept=intercept,
            auc=quality,
            pairs=len(pairs),
            template=template,
        )
    except EncoderUnavailable as exc:
        _fail(str(exc))
        return False
    except Exception as exc:  # a model/runtime bug must never take the server down
        _fail(f"unexpected {type(exc).__name__}")
        return False
    with _lock:
        _snapshot = snap
        _embed = embedder
        _word_cache.clear()
        _status.update(state="ready", reason="ok")
    log.info("ready model=%s device=%s auc=%.3f slope=%.2f intercept=%.2f probe_pairs=%d",
             snap.model, snap.device, snap.auc, snap.slope, snap.intercept, snap.pairs)
    return True


def _fail(reason: str) -> None:
    global _snapshot, _embed
    with _lock:
        _snapshot = None
        _embed = None
        _status.update(state="failed", reason=reason)
    log.warning("unavailable, scoring falls back to the taxonomy: %s", reason)


def start_background() -> threading.Thread | None:
    """Kick off loading in a daemon thread. Idempotent; a disabled encoder is a logged no-op."""
    global _thread
    if not config.encoder_enabled():
        with _lock:
            _status.update(state="disabled", reason="ENCODER_ENABLED is off")
        log.info("disabled (ENCODER_ENABLED off); scoring uses the taxonomy")
        return None
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(target=load, name="encoder-loader", daemon=True)
        _thread.start()
        return _thread


def reset() -> None:
    """Forget everything (tests)."""
    global _snapshot, _embed, _thread
    with _lock:
        _snapshot = None
        _embed = None
        _thread = None
        _word_cache.clear()
        _status.update(state="idle", reason="not started")


# --------------------------------------------------------------------------- using it


def status() -> dict:
    snap = _snapshot
    out = dict(_status)
    if snap is not None:
        out.update(model=snap.model, device=snap.device, auc=round(snap.auc, 3), probe_pairs=snap.pairs)
    return out


def ready() -> bool:
    return _snapshot is not None


def similarity_fn() -> tuple[Callable[[str, str], float], str] | None:
    """A snapshot-bound (sim function, model label), or None when the encoder is not usable.

    The function closes over ONE snapshot, so a whole matching pass is scored consistently even if
    the encoder finishes loading halfway through.
    """
    snap = _snapshot
    if snap is None:
        return None
    return snap.similarity, snap.model


def nearest_tag(raw: str) -> tuple[str, float] | None:
    """The canonical tag closest to a free-text interest, if it is close enough to trust.

    Used only for words the synonym table did not know. Returns (tag, similarity) when the best
    tag clears `ENCODER_MAP_MIN`, else None (the word is kept for the student but never scored).
    """
    snap, embed = _snapshot, _embed
    if snap is None or embed is None or not isinstance(raw, str):
        return None
    word = " ".join(raw.strip().lower().split())
    if not word or len(word) > 64:
        return None
    vec = _word_cache.get(word)
    if vec is None:
        try:
            with _embed_lock:
                vec = _unit(embed([snap.template.format(word)])[0])
        except Exception:
            return None
        if len(_word_cache) >= WORD_CACHE_MAX:
            _word_cache.clear()
        _word_cache[word] = vec
    best_tag, best = None, 0.0
    for tag, tvec in snap.vectors.items():
        s = _to_similarity(_dot(vec, tvec), snap.slope, snap.intercept)
        if s > best:
            best_tag, best = tag, s
    if best_tag is not None and best >= config.encoder_map_min():
        return best_tag, best
    return None


# --------------------------------------------------------------------------- manual check


def main() -> int:  # pragma: no cover - needs the real model
    """`uv run python -m server.encoder`: load the real model and print what it thinks."""
    ok = load()
    print("status:", status())
    if not ok:
        return 1
    fn, _ = similarity_fn()  # type: ignore[misc]
    print("\nsimilarity (0..1) between canonical tags:")
    for a, b in (("climbing", "hiking"), ("climbing", "bouldering"), ("coffee", "baking"), ("guitar", "piano"),
                 ("running", "gym"), ("hiking", "basketball"), ("climbing", "chess"), ("coffee", "techno")):
        print(f"  {a:>10} ~ {b:<10} {fn(a, b):.2f}")
    print("\nfree-text word -> nearest tag:")
    for word in ("crimping", "matcha", "espresso", "trail running", "ramen", "synthwave", "mycology", "accounting"):
        print(f"  {word:>14} -> {nearest_tag(word)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
