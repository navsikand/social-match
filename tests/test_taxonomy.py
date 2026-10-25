"""Interest similarity: taxonomy, rarity weights, bounded tag score, and how the matcher uses them.

Unit tests hit the pure functions in server/taxonomy.py. Integration tests reuse the seeded-pair
helpers from test_matching so the card/score paths are the real ones.
"""
import json

import pytest

from server import config, db, taxonomy
from server.schema import TAG_VOCAB
from tests import util
from tests.test_matching import (  # noqa: F401  (frozen is an autouse fixture)
    TZ,
    WINDOW_19_21,
    _setup,
    all_rows,
    candidate_for,
    frozen,
    gen,
)


# ------------------------------------------------------------------ the taxonomy itself


def test_every_domain_tag_is_in_the_closed_vocabulary():
    for domain, tags in taxonomy.DOMAINS.items():
        for tag in tags:
            assert tag in TAG_VOCAB, f"{tag!r} in domain {domain!r} is not a vocabulary tag"


def test_every_vocabulary_tag_belongs_to_a_domain():
    # A tag with no domain can only ever match itself; that is allowed, but we want to know.
    orphans = [t for t in TAG_VOCAB if not taxonomy.domains_of(t)]
    assert orphans == [], f"tags with no domain: {orphans}"


@pytest.mark.parametrize("a,b,expected", [
    ("climbing", "climbing", 1.0),
    ("climbing", "hiking", 0.5),          # both outdoors
    ("climbing", "bouldering", 0.5),
    ("running", "gym", 0.5),              # both fitness
    ("hiking", "basketball", 0.2),        # outdoors ~ team sports (compatible)
    ("guitar", "painting", 0.2),          # music_making ~ creative (compatible)
    ("hiking", "chess", 0.0),
    ("coffee", "techno", 0.0),
])
def test_similarity_levels(a, b, expected):
    assert taxonomy.sim(a, b) == expected
    assert taxonomy.sim(b, a) == expected  # symmetric


def test_unknown_tag_only_matches_itself():
    assert taxonomy.sim("mycology", "mycology") == 1.0
    assert taxonomy.sim("mycology", "hiking") == 0.0


# ------------------------------------------------------------------ rarity weights


def test_weights_are_one_while_there_are_too_few_users():
    assert taxonomy.weight(1, taxonomy.MIN_USERS_FOR_WEIGHTS - 1) == 1.0
    assert taxonomy.weight(5, 3) == 1.0


def test_rare_tags_weigh_more_than_common_ones_and_stay_bounded():
    n = 100
    rare, middle, common = (taxonomy.weight(c, n) for c in (1, 10, 95))
    assert rare > middle > common
    assert taxonomy.WEIGHT_MIN <= common and rare <= taxonomy.WEIGHT_MAX
    assert common == taxonomy.WEIGHT_MIN  # nearly everyone has it
    assert 1.4 < rare <= 2.0


# ------------------------------------------------------------------ the tag score


def test_exact_shared_tag_is_worth_three_points_as_before():
    assert taxonomy.tag_points({"climbing"}, {"climbing"}) == 3.0
    assert taxonomy.tag_points({"climbing", "coffee"}, {"climbing", "coffee"}) == 6.0


def test_a_related_tag_earns_partial_credit():
    pts = taxonomy.tag_points({"climbing"}, {"hiking"})
    assert pts == 1.5          # 3 * 0.5, in both directions
    assert 0 < pts < 3.0


def test_tag_points_are_symmetric():
    a, b = {"climbing", "coffee", "chess"}, {"hiking", "coffee"}
    assert taxonomy.tag_points(a, b) == taxonomy.tag_points(b, a)


def test_many_tags_cannot_inflate_the_score_past_the_cap():
    many = set(TAG_VOCAB[:8])
    pts = taxonomy.tag_points(many, many)
    assert pts == taxonomy.TAG_POINTS_CAP == 9.0


def test_max_per_tag_not_cross_product():
    # One climber vs five hikers-ish tags: A's single tag counts once, not five times.
    forward_only = taxonomy.tag_points({"climbing"}, {"hiking", "cycling", "running", "bouldering"})
    assert forward_only < 3.0 * 2   # a cross-product sum would be far larger


def test_no_tags_no_points():
    assert taxonomy.tag_points(set(), {"climbing"}) == 0.0
    assert taxonomy.tag_points({"climbing"}, set()) == 0.0


def test_weights_scale_the_points_but_the_cap_still_holds():
    w = {"bouldering": 2.0, "climbing": 2.0, "hiking": 2.0}
    pts = taxonomy.tag_points({"bouldering", "climbing", "hiking"}, {"bouldering", "climbing", "hiking"}, w)
    assert pts == 9.0
    assert taxonomy.tag_points({"bouldering"}, {"bouldering"}, {"bouldering": 0.5}) == 1.5


def test_related_pairs_are_perspective_neutral_and_exclude_identical_tags():
    ab = taxonomy.related_pairs({"climbing", "coffee"}, {"hiking", "coffee"})
    ba = taxonomy.related_pairs({"hiking", "coffee"}, {"climbing", "coffee"})
    assert ab == ba == [{"tags": ["climbing", "hiking"], "domain": "outdoors"}]


# ------------------------------------------------------------------ the matcher


def reasons(a, b):
    return json.loads(candidate_for(a, b)["reasons_json"])


def test_related_interests_now_score_and_are_explained_on_the_card():
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert gen(a)
    row = candidate_for(a, b)
    # interest 1.5 -> 2 (rounded half up) + 2*2 overlap hours = 6; before this change it was 4
    assert row["score"] == 6
    r = reasons(a, b)
    assert r["shared_tags"] == []          # nothing EXACTLY shared, so the activity menu is unchanged
    assert r["related"] == [{"tags": ["climbing", "hiking"], "domain": "outdoors"}]


def test_unrelated_interests_score_zero_interest_points():
    a, b = _setup(a_tags=("climbing",), b_tags=("coffee",))
    assert gen(a)
    row = candidate_for(a, b)
    assert row["score"] == 4              # only the 2 overlap hours
    assert "related" not in reasons(a, b)


def test_default_still_makes_a_card_on_time_alone():
    a, b = _setup(a_tags=("climbing",), b_tags=("chess",))
    assert gen(a)


def test_similarity_floor_requires_at_least_a_shared_domain(monkeypatch):
    monkeypatch.setenv("SIMILARITY_FLOOR", "0.5")
    a, b = _setup(a_tags=("climbing",), b_tags=("chess",))
    assert gen(a) == []                    # no card: nothing similar enough
    assert candidate_for(a, b) is None


def test_similarity_floor_lets_related_pairs_through(monkeypatch):
    monkeypatch.setenv("SIMILARITY_FLOOR", "0.5")
    a, b = _setup(a_tags=("climbing",), b_tags=("hiking",))
    assert len(gen(a)) == 1


def test_similarity_floor_config_is_clamped_and_survives_garbage(monkeypatch):
    monkeypatch.setenv("SIMILARITY_FLOOR", "banana")
    assert config.similarity_floor() == 0.0
    monkeypatch.setenv("SIMILARITY_FLOOR", "7")
    assert config.similarity_floor() == 1.0
    monkeypatch.setenv("SIMILARITY_FLOOR", "-2")
    assert config.similarity_floor() == 0.0


def test_rarity_weights_only_apply_once_there_are_enough_live_users():
    a, b = _setup(a_tags=("bouldering",), b_tags=("bouldering",))
    c = db.connect()
    try:
        # Two users: cold start, no weights at all.
        from server import matching
        assert matching._tag_weights(c, db.utcnow()) == {}
    finally:
        c.close()

    # Add enough other live users, all with the same common tag, to leave cold start.
    for i in range(taxonomy.MIN_USERS_FOR_WEIGHTS):
        uid = util.mk_user(f"u{i}@example.com", f"U{i}")
        util.tags(uid, ["coffee"])
        util.windows(uid, list(WINDOW_19_21), tz=TZ)
    c = db.connect()
    try:
        w = matching._tag_weights(c, db.utcnow())
    finally:
        c.close()
    assert w["coffee"] < w["bouldering"]         # common < rare
    assert taxonomy.WEIGHT_MIN <= w["coffee"] and w["bouldering"] <= taxonomy.WEIGHT_MAX
    assert any(r["tag"] == "coffee" for r in all_rows("SELECT tag FROM hobby_tags WHERE tag='coffee'"))


def test_rare_shared_tag_outscores_a_common_one_in_a_crowd():
    from server import matching

    a, b = _setup(a_tags=("bouldering", "coffee"), b_tags=("bouldering", "coffee"))
    ids = [a, b]
    for i in range(taxonomy.MIN_USERS_FOR_WEIGHTS + 4):
        uid = util.mk_user(f"c{i}@example.com", f"C{i}")
        util.tags(uid, ["coffee"])
        util.windows(uid, list(WINDOW_19_21), tz=TZ)
        ids.append(uid)
    c = db.connect()
    try:
        w = matching._tag_weights(c, db.utcnow())
    finally:
        c.close()
    # Same two shared tags, but the rare one carries more of the score than the common one.
    rare_only = taxonomy.tag_points({"bouldering"}, {"bouldering"}, w)
    common_only = taxonomy.tag_points({"coffee"}, {"coffee"}, w)
    assert rare_only > common_only
