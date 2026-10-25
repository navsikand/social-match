"""Interest similarity: a small hand-made taxonomy plus rarity weighting (BRAINSTORM §1.3-1.4).

Pure functions, no database, no network, no model. Everything here is a lookup or a formula, so a
score is reproducible and every partial match can be explained in one sentence.

  sim(a, b)   1.0  same canonical tag
              0.5  the two tags share a domain          (climbing ~ hiking: both outdoors)
              0.2  their domains are listed compatible  (outdoors ~ team sports)
              0.0  otherwise

  weight(t)   how much a tag says about a pair, from how many live users have it. A tag almost
              everyone has says little; a rare one says a lot. Bounded to [0.5, 2.0], and exactly 1.0
              while there are too few users for the counts to mean anything (cold start).

  tag_points  the interest part of a card's score. Each tag takes its BEST match on the other side
              (max, not a cross-product, so eight tags cannot inflate every comparison), weighted by
              rarity, averaged over the two directions, scaled so an exact shared tag is worth 3
              points (the old rule), and capped so one interest cannot outvote the calendar.

Honest limits: the domains are our own assumptions, and rarity weights from a few dozen users are
noisy. Both are heuristics, listed as such in CONTRACT §16.
"""
import math

# A tag may sit in more than one domain (running is outdoors and fitness).
DOMAINS: dict[str, tuple[str, ...]] = {
    "outdoors": ("bouldering", "climbing", "hiking", "cycling", "running"),
    "fitness": ("gym", "swimming", "running", "bouldering", "climbing"),
    "team_sports": ("basketball", "soccer", "ultimate_frisbee", "tennis"),
    "games": ("board_games", "chess", "video_games"),
    "screen": ("video_games", "anime", "cinema"),
    "creative": ("painting", "drawing", "photography", "pottery"),
    "music_making": ("guitar", "piano", "singing"),
    "music_scene": ("live_music", "edm", "techno", "rock", "jazz"),
    "food": ("coffee", "baking", "cooking"),
    "community": ("language_exchange", "debate", "volunteering"),
}

# Domain pairs that are "kind of the same sort of evening". Unordered.
COMPATIBLE: tuple[tuple[str, str], ...] = (
    ("outdoors", "team_sports"),
    ("fitness", "team_sports"),
    ("creative", "music_making"),
    ("music_making", "music_scene"),
    ("games", "screen"),
)

SIM_SAME = 1.0
SIM_DOMAIN = 0.5
SIM_COMPATIBLE = 0.2

POINTS_PER_EXACT = 3.0      # the pre-taxonomy rule: 3 points per shared tag
TAG_POINTS_CAP = 9.0        # BRAINSTORM §1.4: one obsessive interest cannot outvote the calendar

MIN_USERS_FOR_WEIGHTS = 8   # below this, counts are noise: every weight is 1.0
WEIGHT_MIN = 0.5
WEIGHT_MAX = 2.0

_TAG_DOMAINS: dict[str, frozenset[str]] = {}
for _domain, _tags in DOMAINS.items():
    for _tag in _tags:
        _TAG_DOMAINS[_tag] = _TAG_DOMAINS.get(_tag, frozenset()) | {_domain}
_COMPAT = {frozenset(pair) for pair in COMPATIBLE}


def domains_of(tag: str) -> frozenset[str]:
    return _TAG_DOMAINS.get(tag, frozenset())


def shared_domain(a: str, b: str) -> str | None:
    """The alphabetically first domain both tags belong to, or None."""
    common = domains_of(a) & domains_of(b)
    return min(common) if common else None


def sim(a: str, b: str) -> float:
    if a == b:
        return SIM_SAME
    da, db_ = domains_of(a), domains_of(b)
    if da & db_:
        return SIM_DOMAIN
    for x in da:
        for y in db_:
            if frozenset((x, y)) in _COMPAT:
                return SIM_COMPATIBLE
    return 0.0


def weight(users_with_tag: int, live_users: int) -> float:
    """Rarity weight in [0.5, 2.0]; 1.0 during cold start.

    2 * ln((N+1)/(n+1)) / ln(N+1): a tag only one person has scores near the top, a tag half the
    users have lands near 1.0, and a tag nearly everyone has bottoms out at 0.5.
    """
    if live_users < MIN_USERS_FOR_WEIGHTS:
        return 1.0
    raw = 2.0 * math.log((live_users + 1) / (users_with_tag + 1)) / math.log(live_users + 1)
    return max(WEIGHT_MIN, min(WEIGHT_MAX, raw))


def best_match(tag: str, others: set[str] | frozenset[str], sim_fn=None) -> float:
    f = sim_fn or sim
    return max((f(tag, other) for other in others), default=0.0)


def tag_points(mine: set[str], theirs: set[str], weights: dict[str, float] | None = None,
               sim_fn=None) -> float:
    """The interest component of the score (see module docstring). Symmetric in its arguments.

    `sim_fn(a, b) -> 0..1` defaults to the hand-made taxonomy; the matcher passes the learned
    encoder's similarity when one is ready (server/encoder.py).
    """
    if not mine or not theirs:
        return 0.0
    w = weights or {}
    forward = sum(best_match(t, theirs, sim_fn) * w.get(t, 1.0) for t in mine)
    backward = sum(best_match(t, mine, sim_fn) * w.get(t, 1.0) for t in theirs)
    return min(TAG_POINTS_CAP, POINTS_PER_EXACT * (forward + backward) / 2.0)


def max_similarity(mine: set[str], theirs: set[str], sim_fn=None) -> float:
    f = sim_fn or sim
    return max((f(a, b) for a in mine for b in theirs), default=0.0)


def related_pairs(mine: set[str], theirs: set[str], limit: int = 3, sim_fn=None,
                  related_at: float = SIM_DOMAIN) -> list[dict]:
    """Same-domain pairs that are NOT the same tag, e.g. climbing ~ hiking, for the card to explain.

    Perspective-neutral on purpose (the stored reason is shown to both students): the pair's tags
    are sorted, so it reads the same from either side. Both tags are already visible on the card.
    """
    if sim_fn is not None:
        # Learned similarity: no domain label exists, so the card shows the score instead.
        scored: dict[tuple[str, str], float] = {}
        for a in sorted(mine):
            for b in sorted(theirs):
                if a == b:
                    continue
                s = sim_fn(a, b)
                if s >= related_at:
                    key = tuple(sorted((a, b)))
                    scored[key] = max(scored.get(key, 0.0), s)
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"tags": list(k), "domain": shared_domain(*k), "score": round(s, 2)}
                for k, s in ranked[:limit]]
    seen: dict[tuple[str, str], str] = {}
    for a in sorted(mine):
        for b in sorted(theirs):
            if a == b:
                continue
            domain = shared_domain(a, b)
            if domain is None:
                continue
            key = tuple(sorted((a, b)))
            seen.setdefault(key, domain)
    pairs = [{"tags": list(key), "domain": dom} for key, dom in sorted(seen.items())]
    return pairs[:limit]
