"""Deterministic matching engine tests (CONTRACT §6, §13 P2a). P2a owns this file.

Time is frozen so TTLs and availability expiry are exact. `generate_for_user` is exercised
both on a fresh autocommit connection and INSIDE its own `db.immediate()` transaction —
the latter is the production call shape from `server/state.py` and proves the engine never
issues a nested BEGIN.
"""
import pytest

from server import db, matching, wording
from tests import util

FROZEN = "2026-10-24T12:00:00Z"
TZ = "America/Los_Angeles"
WINDOW_19_21 = (("2026-10-29T19:00:00", "2026-10-29T21:00:00"),)


@pytest.fixture(autouse=True)
def frozen(db_env):
    db.init_db()  # the `client` fixture normally does this via app lifespan
    previous = db._FIXED_NOW
    db._FIXED_NOW = FROZEN
    try:
        yield FROZEN
    finally:
        db._FIXED_NOW = previous


# ------------------------------------------------------------------ helpers


def _setup(
    *,
    a_tags=("climbing",),
    b_tags=("climbing",),
    a_windows=WINDOW_19_21,
    b_windows=None,
    a_matching=True,
    b_matching=True,
    a_pub="card",
    b_pub="card",
    a_expires_days=None,
    b_expires_days=None,
    a_wants=None,
    b_wants=None,
):
    """Two seeded users (Ava, Ben) with grants/tags/windows. Returns (a_id, b_id)."""
    if b_windows is None:
        b_windows = a_windows
    a = util.mk_user("ava@example.com", "Ava")
    b = util.mk_user("ben@example.com", "Ben")
    if a_matching:
        util.grant(a, "matching")
    if b_matching:
        util.grant(b, "matching")
    if a_pub == "card":
        util.pub_card(a)
    elif a_pub == "card+name":
        util.pub_name(a)
    if b_pub == "card":
        util.pub_card(b)
    elif b_pub == "card+name":
        util.pub_name(b)
    util.tags(a, list(a_tags))
    util.tags(b, list(b_tags))
    if a_wants is not None:
        util.wants(a, list(a_wants))
    if b_wants is not None:
        util.wants(b, list(b_wants))
    util.windows(a, list(a_windows), tz=TZ, expires_days=a_expires_days)
    util.windows(b, list(b_windows), tz=TZ, expires_days=b_expires_days)
    return a, b


def gen(uid):
    """Run the engine on a fresh connection (autocommit, like a read-only caller)."""
    c = db.connect()
    try:
        return matching.generate_for_user(c, uid)
    finally:
        c.close()


def one(sql, args=()):
    c = db.connect()
    try:
        return db.one(c, sql, args)
    finally:
        c.close()


def all_rows(sql, args=()):
    c = db.connect()
    try:
        return db.q(c, sql, args)
    finally:
        c.close()


def candidate_for(a, b):
    return one(
        "SELECT * FROM candidates WHERE user_a=? AND user_b=? ORDER BY id DESC LIMIT 1",
        (min(a, b), max(a, b)),
    )


def inbox(uid):
    return all_rows(
        "SELECT seq, kind, payload_json FROM inbox_items WHERE user_id=? ORDER BY seq", (uid,)
    )


# ------------------------------------------------------------------ scoring


def test_score_arithmetic_two_shared_tags_two_hours_both_event():
    a, b = _setup(
        a_tags=("climbing", "live_music"),
        b_tags=("climbing", "live_music"),
        a_wants=("event",),
        b_wants=("event",),
    )

    ids = gen(a)

    assert len(ids) == 1
    row = candidate_for(a, b)
    assert row["id"] == ids[0]
    # 3*2 shared tags + 2*2 whole overlap hours + 1 both-want-event = 11
    assert row["score"] == 11
    assert row["state"] == "open"
    reasons = row["reasons_json"]
    assert '"shared_tags": ["climbing", "live_music"]' in reasons
    assert '"overlap_hours": 2' in reasons
    assert '"both_want_event": true' in reasons

    proposal = one("SELECT * FROM proposals WHERE candidate_id=?", (row["id"],))
    assert proposal["version"] == 1
    assert proposal["state"] == "proposed"
    assert proposal["window_start"] == util.to_utc("2026-10-29T19:00:00", TZ)
    assert proposal["window_end"] == util.to_utc("2026-10-29T21:00:00", TZ)
    assert proposal["expires_at"] == row["expires_at"]


def test_candidate_expires_at_is_min_of_both_availabilities():
    a, b = _setup(b_expires_days=2)  # Ben's availability expires first
    gen(a)
    row = candidate_for(a, b)
    assert row["expires_at"] == db.utcnow(days=2)


def test_no_both_event_flag_without_event_tag():
    a, b = _setup(a_tags=("climbing",), b_tags=("climbing",))
    gen(a)
    row = candidate_for(a, b)
    # 3*1 + 2*2 + 0 = 7
    assert row["score"] == 7
    assert '"both_want_event": false' in row["reasons_json"]


# ------------------------------------------------------------------ overlap


def test_longest_overlap_wins_tie_earliest_start():
    a, b = _setup(
        a_windows=(
            ("2026-10-29T10:00:00", "2026-10-29T13:00:00"),  # 3h with Ben's morning window
            ("2026-10-29T19:00:00", "2026-10-29T22:00:00"),  # 1h with Ben's evening window
        ),
        b_windows=(
            ("2026-10-29T10:00:00", "2026-10-29T13:00:00"),
            ("2026-10-29T18:00:00", "2026-10-29T20:00:00"),
        ),
    )
    gen(a)
    proposal = one(
        "SELECT * FROM proposals WHERE candidate_id=?",
        (candidate_for(a, b)["id"],),
    )
    assert proposal["window_start"] == util.to_utc("2026-10-29T10:00:00", TZ)
    assert proposal["window_end"] == util.to_utc("2026-10-29T13:00:00", TZ)
    assert '"overlap_hours": 3' in candidate_for(a, b)["reasons_json"]


def test_sub_30_minute_overlap_produces_no_candidate():
    tiny = (("2026-10-29T10:00:00", "2026-10-29T10:20:00"),)
    a, b = _setup(a_windows=tiny, b_windows=tiny)
    assert gen(a) == []
    assert candidate_for(a, b) is None
    assert inbox(a) == [] and inbox(b) == []


# ------------------------------------------------------------------ exclusions


def test_blocked_pair_is_skipped_both_directions():
    a, b = _setup()
    c = db.connect()
    try:
        now = db.utcnow()
        exp = db.utcnow(days=365)
        ah, bh = db.user_hash(a), db.user_hash(b)
        c.execute(
            "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at) VALUES (?,?,?,?)",
            (ah, bh, now, exp),
        )
        c.execute(
            "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at) VALUES (?,?,?,?)",
            (bh, ah, now, exp),
        )
    finally:
        c.close()

    assert gen(a) == []
    assert gen(b) == []
    assert candidate_for(a, b) is None


def test_swiped_pair_is_skipped():
    a, b = _setup()
    cid, _pid = util.mk_candidate(a, b)
    c = db.connect()
    try:
        # Void the candidate so ONLY the swipe rule can suppress regeneration.
        c.execute("UPDATE candidates SET state='voided' WHERE id=?", (cid,))
        c.execute(
            "INSERT INTO swipes (user_id, candidate_id, direction, created_at) VALUES (?,?,?,?)",
            (a, cid, "left", db.utcnow()),
        )
    finally:
        c.close()

    assert gen(a) == []
    assert candidate_for(a, b)["state"] == "voided"


def test_expired_counterpart_is_skipped():
    a, b = _setup(b_expires_days=-1)  # Ben observed a day ago → stale
    assert gen(a) == []
    assert candidate_for(a, b) is None


def test_missing_matching_grant_on_one_side_means_no_candidate():
    a, b = _setup(b_matching=False)
    assert gen(a) == []
    assert gen(b) == []
    assert candidate_for(a, b) is None


def test_duplicate_pair_not_regenerated():
    a, b = _setup()
    first = gen(a)
    assert len(first) == 1
    assert gen(a) == []
    assert gen(b) == []
    rows = all_rows(
        "SELECT id FROM candidates WHERE user_a=? AND user_b=?", (min(a, b), max(a, b))
    )
    assert len(rows) == 1


# ------------------------------------------------------------------ inbox


def test_candidate_inbox_pushed_to_both_users():
    a, b = _setup()
    ids = gen(a)
    cid = ids[0]

    a_items = inbox(a)
    b_items = inbox(b)
    assert [r["kind"] for r in a_items] == ["candidate"]
    assert [r["kind"] for r in b_items] == ["candidate"]

    import json

    a_payload = json.loads(a_items[0]["payload_json"])
    b_payload = json.loads(b_items[0]["payload_json"])
    for payload in (a_payload, b_payload):
        assert payload["candidate_id"] == cid
        assert payload["card_url"] == wording.card_url(cid)
        assert "is up for" in payload["message"]
        assert payload["card_url"] in payload["message"]


def test_message_asymmetry_obeys_publication_grants():
    a, b = _setup(a_pub="card+name", b_pub="card")  # Ava shareable by name, Ben by card only
    gen(a)

    import json

    # Ava hears about Ben → pseudonym (Ben granted only `card`).
    msg_to_a = json.loads(inbox(a)[0]["payload_json"])["message"]
    conn = db.connect()
    try:
        ben_pseudonym = wording.pseudonym_for(conn, b)
    finally:
        conn.close()
    assert ben_pseudonym in msg_to_a
    assert "Ben" not in msg_to_a
    assert ben_pseudonym.startswith("climbing-")

    # Ben hears about Ava → display name (Ava granted card+name).
    msg_to_b = json.loads(inbox(b)[0]["payload_json"])["message"]
    assert msg_to_b.startswith("Ava is up for")


# ------------------------------------------------------------------ transaction discipline


def test_generate_inside_immediate_transaction_does_not_nest_begin():
    """Production call shape: state.py invokes the engine inside an OPEN db.immediate txn."""
    a, b = _setup()
    conn = db.connect()
    try:
        with db.immediate(conn):
            ids = matching.generate_for_user(conn, a)
    finally:
        conn.close()

    assert len(ids) == 1
    assert candidate_for(a, b)["state"] == "open"
    assert len(inbox(a)) == 1 and len(inbox(b)) == 1
