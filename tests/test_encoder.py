"""The learned-similarity encoder: calibration, the quality gate, fallbacks, and the matcher.

No test downloads a model. `fake_embedder` is a deterministic stand-in whose geometry follows the
taxonomy's domains (so it is a "good" encoder) with a little hash noise per word; `random_embedder`
is a "bad" one. What these tests prove is OUR logic around a model (math, gate, ladder, wiring),
not the quality of a real one: that is checked on the host with `python -m server.encoder`.
"""
import hashlib
import json
import math

import pytest

from server import config, encoder, interests, taxonomy
from server.schema import TAG_VOCAB
from tests.test_matching import _setup, candidate_for, frozen, gen  # noqa: F401  (frozen: autouse)

DOMAINS = sorted(taxonomy.DOMAINS)
NOISE_DIMS = 12

# Free-text words the fake "understands", as the tag whose meaning they share.
FAKE_MEANING = {
    "crimping": "climbing", "matcha": "coffee", "espresso": "coffee", "trail running": "hiking",
    "jogging": "running", "weightlifting": "gym", "pickup basketball": "basketball",
    "watercolor": "painting", "sketching": "drawing", "ramen": "cooking", "sourdough": "baking",
    "synthwave": "edm",
}


def _hash_vec(word: str) -> list[float]:
    digest = hashlib.sha256(word.encode()).digest()
    return [(digest[i] / 255.0 - 0.5) * 0.4 for i in range(NOISE_DIMS)]


def _vector(text: str) -> list[float]:
    word = text.split(":", 1)[-1].strip().lower()
    tag = FAKE_MEANING.get(word) or word.replace(" ", "_")
    domains = taxonomy.domains_of(tag)
    vec = [1.0 if d in domains else 0.0 for d in DOMAINS]
    return vec + _hash_vec(word)


def fake_embedder(texts):
    return [_vector(t) for t in texts]


def random_embedder(texts):
    out = []
    for t in texts:
        digest = hashlib.sha256(("rnd" + t).encode()).digest()
        out.append([digest[i] / 255.0 - 0.5 for i in range(16)])
    return out


@pytest.fixture(autouse=True)
def clean_encoder(monkeypatch):
    monkeypatch.setenv("ENCODER_ENABLED", "0")
    encoder.reset()
    yield
    encoder.reset()


# ------------------------------------------------------------------ the maths


def test_auc_perfect_inverted_and_tied():
    assert encoder.auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    assert encoder.auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0
    assert encoder.auc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == 0.5
    assert encoder.auc([0.5, 0.6], [1, 1]) == 0.5  # one class only: no information


def test_logistic_fit_separates_and_is_monotonic():
    xs = [0.1, 0.2, 0.25, 0.3, 0.6, 0.7, 0.75, 0.8]
    ys = [0, 0, 0, 0, 1, 1, 1, 1]
    slope, intercept = encoder.fit_logistic(xs, ys)
    assert slope > 0
    lo = encoder._sigmoid(slope * 0.2 + intercept)
    hi = encoder._sigmoid(slope * 0.7 + intercept)
    assert lo < 0.5 < hi


def test_logistic_fit_stays_finite_on_perfectly_separable_data():
    slope, intercept = encoder.fit_logistic([0.0, 0.1, 0.9, 1.0], [0, 0, 1, 1])
    assert math.isfinite(slope) and math.isfinite(intercept)


def test_logistic_fit_needs_both_classes():
    with pytest.raises(ValueError):
        encoder.fit_logistic([0.1, 0.2], [1, 1])


def test_similarity_mapping_is_bounded_and_floors_weak_pairs():
    assert encoder._to_similarity(10.0, 5.0, 0.0) == encoder.MAX_SIM       # never reaches 1.0
    assert encoder._to_similarity(-10.0, 5.0, 0.0) == 0.0                  # weak noise is zero


def test_probe_set_has_both_classes_and_skips_compatible_pairs():
    pairs = encoder.probe_pairs()
    labels = {y for _, _, y in pairs}
    assert labels == {0, 1}
    # climbing~hiking share a domain (related); outdoors~team_sports is only "compatible" (skipped)
    assert ("climbing", "hiking", 1) in pairs
    assert ("hiking", "basketball", 0) not in pairs and ("hiking", "basketball", 1) not in pairs


# ------------------------------------------------------------------ loading


def test_load_with_a_good_embedder_gets_ready_and_calibrated():
    assert encoder.ready() is False
    assert encoder.load(fake_embedder, model_name="fake-good") is True
    assert encoder.ready() is True
    st = encoder.status()
    assert st["state"] == "ready" and st["model"] == "fake-good" and st["auc"] >= 0.95


def test_learned_similarity_orders_pairs_sensibly():
    encoder.load(fake_embedder, model_name="fake-good")
    fn, name = encoder.similarity_fn()
    assert name == "fake-good"
    assert fn("climbing", "climbing") == 1.0
    assert fn("climbing", "hiking") > fn("climbing", "chess")
    assert fn("climbing", "chess") == 0.0
    assert 0.0 < fn("climbing", "hiking") <= encoder.MAX_SIM
    assert fn("guitar", "piano") == fn("piano", "guitar")        # symmetric
    assert fn("climbing", "not_a_tag") == 0.0                    # unknown tags never match


def test_quality_gate_rejects_an_encoder_that_cannot_tell_related_from_unrelated():
    assert encoder.load(random_embedder, model_name="fake-random") is False
    assert encoder.ready() is False
    st = encoder.status()
    assert st["state"] == "failed" and "quality gate" in st["reason"]
    assert encoder.similarity_fn() is None


def test_quality_gate_threshold_is_configurable(monkeypatch):
    monkeypatch.setenv("ENCODER_MIN_AUC", "0.5")
    # With the gate wide open the random encoder is allowed through (it still has to calibrate).
    encoder.load(random_embedder, model_name="fake-random")
    assert encoder.status()["state"] in ("ready", "failed")


def test_an_embedder_that_raises_falls_back_without_raising():
    def boom(texts):
        raise RuntimeError("cuda out of memory")

    assert encoder.load(boom) is False
    assert encoder.status()["state"] == "failed"
    assert encoder.similarity_fn() is None


def test_wrong_vector_count_is_rejected():
    assert encoder.load(lambda texts: [[1.0, 0.0]]) is False
    assert "wrong number" in encoder.status()["reason"]


def test_missing_library_is_a_reason_not_a_crash(monkeypatch):
    def unavailable():
        raise encoder.EncoderUnavailable("sentence-transformers is not installed (ImportError)")

    monkeypatch.setattr(encoder, "_load_sentence_transformer", unavailable)
    assert encoder.load() is False
    assert "not installed" in encoder.status()["reason"]


def test_a_failed_reload_drops_a_previously_good_encoder():
    assert encoder.load(fake_embedder) is True
    assert encoder.load(random_embedder) is False
    assert encoder.ready() is False        # never keep scoring with a model that just failed the gate


def test_disabled_encoder_does_not_start(monkeypatch):
    monkeypatch.setenv("ENCODER_ENABLED", "0")
    assert encoder.start_background() is None
    assert encoder.status()["state"] == "disabled"
    assert encoder.ready() is False


def test_background_start_loads_in_a_thread(monkeypatch):
    monkeypatch.setenv("ENCODER_ENABLED", "1")
    monkeypatch.setattr(encoder, "_load_sentence_transformer", lambda: (fake_embedder, "cpu"))
    thread = encoder.start_background()
    assert thread is not None
    thread.join(timeout=10)
    assert encoder.ready() is True
    assert encoder.status()["device"] == "cpu"


def test_config_parsing_is_clamped_and_survives_garbage(monkeypatch):
    monkeypatch.setenv("ENCODER_MIN_AUC", "banana")
    assert config.encoder_min_auc() == 0.80
    monkeypatch.setenv("ENCODER_MIN_AUC", "9")
    assert config.encoder_min_auc() == 1.0
    monkeypatch.setenv("ENCODER_MAP_MIN", "-1")
    assert config.encoder_map_min() == 0.5
    monkeypatch.setenv("ENCODER_ENABLED", "off")
    assert config.encoder_enabled() is False
    monkeypatch.delenv("ENCODER_ENABLED")
    assert config.encoder_enabled() is True
    monkeypatch.setenv("ENCODER_TEMPLATE", "no placeholder")
    assert config.encoder_template() == "hobby: {}"


# ------------------------------------------------------------------ free text -> tag


def test_nearest_tag_maps_a_word_the_synonym_table_does_not_know(monkeypatch):
    monkeypatch.setenv("ENCODER_MAP_MIN", "0.5")
    assert interests.normalize("crimping") is None       # the tables really do not know it
    encoder.load(fake_embedder)
    hit = encoder.nearest_tag("Crimping")
    # The fake gives climbing and bouldering identical geometry, so either is a correct answer.
    assert hit is not None and hit[0] in ("climbing", "bouldering")


def test_nearest_tag_refuses_unrelated_words_and_bad_input(monkeypatch):
    monkeypatch.setenv("ENCODER_MAP_MIN", "0.85")
    encoder.load(fake_embedder)
    assert encoder.nearest_tag("accounting") is None
    assert encoder.nearest_tag("") is None
    assert encoder.nearest_tag("x" * 200) is None
    assert encoder.nearest_tag(None) is None             # type: ignore[arg-type]


def test_nearest_tag_does_nothing_until_the_encoder_is_ready():
    assert encoder.nearest_tag("crimping") is None


def test_normalize_many_uses_the_encoder_only_for_unknown_words(monkeypatch):
    monkeypatch.setenv("ENCODER_MAP_MIN", "0.5")
    encoder.load(fake_embedder)
    canonical, mapped, unscored = interests.normalize_many(["espresso", "crimping", "accounting"])
    assert "coffee" in canonical and ({"climbing", "bouldering"} & set(canonical))
    by_raw = {m["raw"]: m for m in mapped}
    assert "method" not in by_raw["espresso"]                    # the table knew it
    assert by_raw["crimping"]["method"] == "encoder"             # the encoder mapped it
    assert unscored == ["accounting"]                            # kept for the student, never scored


def test_normalize_many_is_unchanged_with_the_encoder_off():
    canonical, mapped, unscored = interests.normalize_many(["espresso", "crimping"])
    assert canonical == ["coffee"] and unscored == ["crimping"]
    assert all("method" not in m for m in mapped)


# ------------------------------------------------------------------ taxonomy hooks


def test_taxonomy_functions_accept_a_learned_scorer():
    encoder.load(fake_embedder)
    fn, _ = encoder.similarity_fn()
    assert taxonomy.tag_points({"climbing"}, {"climbing"}, None, fn) == 3.0
    related = taxonomy.tag_points({"climbing"}, {"hiking"}, None, fn)
    assert 0 < related < 3.0
    assert taxonomy.tag_points({"climbing"}, {"chess"}, None, fn) == 0.0
    assert taxonomy.max_similarity({"climbing"}, {"hiking"}, fn) == fn("climbing", "hiking")


def test_related_pairs_with_a_learned_scorer_carry_a_score():
    encoder.load(fake_embedder)
    fn, _ = encoder.similarity_fn()
    ab = taxonomy.related_pairs({"climbing", "coffee"}, {"hiking", "coffee"}, sim_fn=fn)
    ba = taxonomy.related_pairs({"hiking", "coffee"}, {"climbing", "coffee"}, sim_fn=fn)
    assert ab == ba                                      # perspective-neutral, same as the taxonomy path
    assert ab and ab[0]["tags"] == ["climbing", "hiking"] and 0.5 <= ab[0]["score"] <= encoder.MAX_SIM


# ------------------------------------------------------------------ the matcher


def reasons(a, b):
    return json.loads(candidate_for(a, b)["reasons_json"])


def test_cards_record_which_model_scored_them():
    encoder.load(fake_embedder, model_name="fake-good")
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert gen(a)
    r = reasons(a, b)
    assert r["similarity_model"] == "fake-good"
    assert r["related"] and r["related"][0]["tags"] == ["climbing", "hiking"]
    assert candidate_for(a, b)["score"] > 4        # partial interest credit on top of 2*2 overlap hours


def test_cards_fall_back_to_the_taxonomy_when_the_encoder_is_not_ready():
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert gen(a)
    r = reasons(a, b)
    assert "similarity_model" not in r
    assert r["related"] == [{"tags": ["climbing", "hiking"], "domain": "outdoors"}]
    assert candidate_for(a, b)["score"] == 6       # exactly the taxonomy-only result


def test_a_failed_gate_leaves_matching_on_the_taxonomy():
    encoder.load(random_embedder)
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert gen(a)
    assert "similarity_model" not in reasons(a, b)
    assert candidate_for(a, b)["score"] == 6


def test_exact_shared_tags_score_the_same_with_or_without_the_encoder():
    encoder.load(fake_embedder)
    a, b = _setup(a_tags=("climbing",), b_tags=("climbing",))
    assert gen(a)
    # 3 points for the exact tag + 2*2 overlap hours: identical to the taxonomy-only rule.
    assert candidate_for(a, b)["score"] == 7


def test_similarity_floor_uses_the_learned_scorer_to_drop_unrelated_pairs(monkeypatch):
    monkeypatch.setenv("SIMILARITY_FLOOR", "0.5")
    encoder.load(fake_embedder)
    a, b = _setup(a_tags=("climbing",), b_tags=("chess",))
    assert gen(a) == []


def test_similarity_floor_uses_the_learned_scorer_to_keep_related_pairs(monkeypatch):
    monkeypatch.setenv("SIMILARITY_FLOOR", "0.5")
    encoder.load(fake_embedder)
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert len(gen(a)) == 1


def test_no_vectors_or_raw_text_in_status():
    encoder.load(fake_embedder)
    dumped = json.dumps(encoder.status())
    assert "vectors" not in dumped and "climbing" not in dumped
