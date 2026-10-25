"""Pairing signals: public events + busy_load on POST /api/state, and how the matcher uses them.

Uses the same frozen clock as test_payload so the 30-day horizon and "already ended" checks are
deterministic.
"""
import json

import pytest

from server import db, scheduling
from server.schema import StatePayload
from tests import util

FROZEN = "2026-10-24T12:00:00Z"
WINDOW = {"start": "2026-10-29T19:00", "end": "2026-10-29T21:00", "tz": "America/Los_Angeles"}
FAIR = "https://careers.asu.example.edu/fall-fair"
SHOW = "https://tickets.example.com/shows/synthwave-night"
NOW_ACK = "2026-10-24T11:00:00Z"


@pytest.fixture(autouse=True)
def frozen_now():
    previous = db._FIXED_NOW
    db._FIXED_NOW = FROZEN
    try:
        yield FROZEN
    finally:
        db._FIXED_NOW = previous


def event(url=FAIR, category="career", start="2026-10-30T10:00", end="2026-10-30T14:00", **extra):
    return {"url": url, "start": start, "end": end, "tz": "America/Phoenix",
            "category": category, **extra}


def body(**overrides):
    out = {
        "windows": [dict(WINDOW)],
        "tags": ["climbing"],
        "wants": ["hangout", "event"],
        "free_time": {"source": "student"},
        "consent": {"matching": True, "publication": {"tier": "card"}},
    }
    out.update(overrides)
    return out


def with_events(events, **overrides):
    return body(events=events, events_confirmed_at=NOW_ACK, **overrides)


@pytest.fixture()
def two(client):
    a = util.mk_user("ava@example.com", "Ava")
    b = util.mk_user("ben@example.com", "Ben")
    ta, _ = util.mint_pairing(a)
    tb, _ = util.mint_pairing(b)
    return {"client": client, "a": a, "b": b, "ta": ta, "tb": tb}


def post(p, who, payload):
    return p["client"].post("/api/state", json=payload, headers=util.auth_header(p["t" + who]))


def stored_events(uid):
    c = util.conn()
    try:
        return [dict(r) for r in c.execute(
            "SELECT url, category, starts_at, ends_at, tz FROM user_events WHERE user_id=?"
            " ORDER BY url", (uid,))]
    finally:
        c.close()


def stored_load(uid):
    c = util.conn()
    try:
        row = c.execute("SELECT busy_load FROM user_load WHERE user_id=?", (uid,)).fetchone()
        return None if row is None else row["busy_load"]
    finally:
        c.close()


def candidate(a, b):
    c = util.conn()
    try:
        row = c.execute("SELECT * FROM candidates WHERE user_a=? AND user_b=?",
                        (min(a, b), max(a, b))).fetchone()
        return None if row is None else {**dict(row), "reasons": json.loads(row["reasons_json"])}
    finally:
        c.close()


# ------------------------------------------------------------------ the allowlist


def test_events_and_load_are_accepted_and_stored(two):
    r = post(two, "a", with_events([event(), event(SHOW, "music", "2026-11-01T20:00", "2026-11-01T23:00")],
                                   busy_load=2))
    assert r.status_code == 201, r.text
    assert sorted(r.json()["events"]["stored"]) == sorted([FAIR, SHOW])
    rows = stored_events(two["a"])
    assert [(x["url"], x["category"]) for x in rows] == [(FAIR, "career"), (SHOW, "music")]
    assert rows[0]["starts_at"] == "2026-10-30T17:00:00Z"  # 10:00 Phoenix (UTC-7) -> 17:00Z
    assert stored_load(two["a"]) == 2


def test_both_fields_are_optional(two):
    assert post(two, "a", body()).status_code == 201
    assert stored_events(two["a"]) == []
    assert stored_load(two["a"]) is None


def test_zero_load_is_stored_not_confused_with_absent(two):
    assert post(two, "a", body(busy_load=0)).status_code == 201
    assert stored_load(two["a"]) == 0


@pytest.mark.parametrize("bad", [4, -1, 1.5, "2", True, "heavy"])
def test_busy_load_must_be_a_plain_integer_0_to_3(two, bad):
    r = post(two, "a", body(busy_load=bad))
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden"
    assert stored_load(two["a"]) is None


def test_events_require_the_students_approval_stamp(two):
    r = post(two, "a", body(events=[event()]))
    assert r.status_code == 422
    assert "events_confirmed_at" in r.json()["detail"]
    assert stored_events(two["a"]) == []


def test_event_extra_fields_are_refused(two):
    # The whole point: no title, sender, location or notes can ride along.
    for extra in ({"title": "Interview with Acme"}, {"location": "Room 12"},
                  {"organizer": "x"}, {"notes": "private"}):
        r = post(two, "a", with_events([event(**extra)]))
        assert r.status_code == 422, extra
        assert next(iter(extra)) in r.json()["detail"]
    assert stored_events(two["a"]) == []


@pytest.mark.parametrize("url", [
    "http://careers.example.edu/fair",          # not https
    "https://user:pw@careers.example.edu/fair",  # credentials
    "https://careers.example.edu/fair#rsvp",     # fragment
    "https://10.0.0.1/fair",                     # IP literal
    "https://localhost/fair",                    # not a real domain
    "https://careers.example.edu:8443/fair",     # odd port
    "https://careers.example.edu/fa ir",         # whitespace
    "https://careers.example.edu/" + "a" * 300,  # too long
    "",
])
def test_event_links_are_validated_like_activity_links(two, url):
    r = post(two, "a", with_events([event(url=url)]))
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden"
    assert stored_events(two["a"]) == []


def test_unknown_category_is_refused(two):
    r = post(two, "a", with_events([event(category="interview")]))
    assert r.status_code == 422


def test_at_most_ten_events(two):
    many = [event(f"https://e.example.com/{i}") for i in range(11)]
    assert post(two, "a", with_events(many)).status_code == 422
    assert post(two, "a", with_events(many[:10])).status_code == 201
    assert len(stored_events(two["a"])) == 10


@pytest.mark.parametrize("start,end", [
    ("2026-10-20T10:00", "2026-10-20T12:00"),  # already over
    ("2026-12-30T10:00", "2026-12-30T12:00"),  # beyond the 30 day horizon
    ("2026-10-30T14:00", "2026-10-30T10:00"),  # backwards
    ("2026-10-30T10:00", "2026-11-02T10:00"),  # longer than 24h
])
def test_event_times_must_be_sane(two, start, end):
    assert post(two, "a", with_events([event(start=start, end=end)])).status_code == 422


def test_event_in_progress_is_allowed(two):
    # Started an hour ago, ends later today: still something a friend could join.
    r = post(two, "a", with_events([event(start="2026-10-24T04:00", end="2026-10-24T09:00")]))
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ canonical links


@pytest.mark.parametrize("variant", [
    "https://careers.asu.example.edu/fall-fair/",
    "https://CAREERS.asu.example.edu/fall-fair",
    "https://careers.asu.example.edu:443/fall-fair",
    "https://careers.asu.example.edu/fall-fair?utm_source=mail&rsvp_token=abc123secret",
])
def test_same_page_compares_equal_and_query_is_never_stored(two, variant):
    assert post(two, "a", with_events([event(url=variant)])).status_code == 201
    assert [x["url"] for x in stored_events(two["a"])] == [FAIR]
    assert "secret" not in json.dumps(stored_events(two["a"]))


def test_duplicate_links_collapse_to_one_row(two):
    assert post(two, "a", with_events([event(), event(FAIR + "/")])).status_code == 201
    assert len(stored_events(two["a"])) == 1


def test_model_canonicalises_in_the_schema_itself():
    p = StatePayload.model_validate(with_events([event(url=FAIR + "/?x=1")]))
    assert p.events[0].url == FAIR


# ------------------------------------------------------------------ replace + cleanup


def test_each_post_replaces_events_and_load(two):
    assert post(two, "a", with_events([event()], busy_load=3)).status_code == 201
    assert post(two, "a", with_events([event(SHOW, "music")], busy_load=1)).status_code == 201
    assert [x["url"] for x in stored_events(two["a"])] == [SHOW] and stored_load(two["a"]) == 1
    assert post(two, "a", body()).status_code == 201  # absent == cleared
    assert stored_events(two["a"]) == [] and stored_load(two["a"]) is None


def test_refusing_matching_stores_nothing_and_clears_old_rows(two):
    assert post(two, "a", with_events([event()], busy_load=2)).status_code == 201
    refusal = with_events([event()], busy_load=2,
                          consent={"matching": False})
    r = post(two, "a", refusal)
    assert r.status_code == 201
    assert r.json()["events"]["stored"] == []
    assert stored_events(two["a"]) == [] and stored_load(two["a"]) is None


def test_delete_account_removes_events_and_load(two):
    assert post(two, "a", with_events([event()], busy_load=2)).status_code == 201
    c = util.conn()
    try:
        scheduling.delete_account(c, two["a"])
    finally:
        c.close()
    assert stored_events(two["a"]) == [] and stored_load(two["a"]) is None


def test_expiry_sweep_removes_events_with_the_availability_row(two):
    from server import sweep

    assert post(two, "a", with_events([event()], busy_load=1)).status_code == 201
    c = util.conn()
    try:
        c.execute("UPDATE availability SET expires_at='2020-01-01T00:00:00Z' WHERE user_id=?",
                  (two["a"],))
        sweep.run_sweep(c)
    finally:
        c.close()
    assert stored_events(two["a"]) == [] and stored_load(two["a"]) is None


# ------------------------------------------------------------------ the matcher


def match_two(two, a_events, b_events, a_load=None, b_load=None):
    kw_a = {"busy_load": a_load} if a_load is not None else {}
    kw_b = {"busy_load": b_load} if b_load is not None else {}
    assert post(two, "a", with_events(a_events, **kw_a)).status_code == 201
    assert post(two, "b", with_events(b_events, **kw_b)).status_code == 201
    return candidate(two["a"], two["b"])


def test_a_shared_event_link_is_a_strong_reason_and_scores_four(two):
    plain = match_two(two, [], [])
    assert plain is not None and "shared_events" not in plain["reasons"]
    base = plain["score"]

    # Fresh pair for the comparison, same everything except the shared event.
    c = util.conn()
    try:
        c.execute("DELETE FROM candidates")
    finally:
        c.close()
    got = match_two(two, [event()], [event(FAIR + "/", "career")])
    assert got is not None
    assert got["reasons"]["shared_events"] == [{"url": FAIR, "category": "career"}]
    assert got["score"] == base + 4


def test_only_the_intersection_is_shown(two):
    got = match_two(two, [event(), event(SHOW, "music")], [event(), event("https://x.example.org/private-ish", "club")])
    urls = [e["url"] for e in got["reasons"]["shared_events"]]
    assert urls == [FAIR]
    blob = json.dumps(got["reasons"])
    assert SHOW not in blob and "private-ish" not in blob


def test_disagreeing_categories_fall_back_to_other(two):
    got = match_two(two, [event(category="career")], [event(category="club")])
    assert got["reasons"]["shared_events"] == [{"url": FAIR, "category": "other"}]


def test_no_shared_event_means_no_reason_key(two):
    got = match_two(two, [event()], [event(SHOW, "music")])
    assert "shared_events" not in got["reasons"]


def test_an_event_that_already_ended_is_not_shared(two):
    assert post(two, "a", with_events([event()])).status_code == 201
    assert post(two, "b", with_events([event()])).status_code == 201
    c = util.conn()
    try:
        c.execute("UPDATE user_events SET ends_at='2020-01-01T00:00:00Z'")
        c.execute("DELETE FROM candidates")
    finally:
        c.close()
    # Re-run matching for the pair: neither side's event is live, so nothing is shared.
    from server import matching

    c = util.conn()
    try:
        with db.immediate(c):
            matching.generate_for_user(c, two["a"])
    finally:
        c.close()
    got = candidate(two["a"], two["b"])
    assert got is not None and "shared_events" not in got["reasons"]


def test_events_sent_after_the_card_exists_still_count_and_can_be_removed(two):
    # Both students already have a card (no events yet). Then they each add the same event.
    first = match_two(two, [], [])
    base = first["score"]
    assert post(two, "a", with_events([event()])).status_code == 201
    assert "shared_events" not in candidate(two["a"], two["b"])["reasons"]  # B has none yet
    assert post(two, "b", with_events([event()])).status_code == 201
    got = candidate(two["a"], two["b"])
    assert got["id"] == first["id"]  # same card, not a new one
    assert got["reasons"]["shared_events"] == [{"url": FAIR, "category": "career"}]
    assert got["score"] == base + 4
    # B drops the event: the card loses the reason and the points again.
    assert post(two, "b", with_events([])).status_code == 201
    back = candidate(two["a"], two["b"])
    assert "shared_events" not in back["reasons"] and back["score"] == base


def test_busy_load_is_stored_but_the_rule_baseline_ignores_it(two):
    light = match_two(two, [], [], a_load=0, b_load=0)
    c = util.conn()
    try:
        c.execute("DELETE FROM candidates")
    finally:
        c.close()
    heavy = match_two(two, [], [], a_load=3, b_load=3)
    assert light["score"] == heavy["score"]
    assert "busy_load" not in json.dumps(heavy["reasons"])
