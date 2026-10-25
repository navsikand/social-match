"""Lane A: GET /api/matches + the /matches page (CONTRACT-photos.md §3, PLAN §A).

Every row is an authorization decision made at read time; `wording.label` only renders the
person after those checks pass. Seeds come from tests/util.py, state changes are direct SQL so a
test names exactly the one grant/row it revokes.
"""
import pytest

from server import db
from tests import util

WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")
PAST_WINDOW = ("2026-10-20T19:00:00", "2026-10-20T21:00:00")
TZ = "America/Los_Angeles"


# ------------------------------------------------------------------ helpers


@pytest.fixture()
def seeded(db_env):
    db.init_db()
    return db_env


def _sql(stmt, args=()):
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(stmt, args)
    finally:
        c.close()


def _pair(window=WINDOW, tz=TZ, **kw):
    a = util.mk_user("ava@example.com", "Ava", avatar_id=3)
    b = util.mk_user("bea@example.com", "Bea", avatar_id=4)
    cid, pid = util.mk_candidate(a, b, window=window, tz=tz, **kw)
    return a, b, cid, pid


def _confirmed(cid, pid, a, b):
    """Both sides confirmed: candidate matched, proposal both_confirmed, two confirmation rows."""
    now = db.utcnow()
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
            c.execute("UPDATE proposals SET state='both_confirmed' WHERE id=?", (pid,))
            for uid in (a, b):
                c.execute(
                    "INSERT OR IGNORE INTO proposal_confirmations (proposal_id, user_id, confirmed_at)"
                    " VALUES (?,?,?)",
                    (pid, uid, now),
                )
    finally:
        c.close()


def _rows(client, uid):
    util.direct_session(client, uid)
    r = client.get("/api/matches", headers={"Accept": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()["matches"]


# ------------------------------------------------------------------ happy path


def test_two_sided_confirm_appears_for_both_users_with_own_label(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    # Only A publishes their name; B keeps the pseudonymous card grant.
    util.pub_name(a)

    a_view = _rows(client, a)
    b_view = _rows(client, b)
    assert [m["candidate_id"] for m in a_view] == [cid]
    assert [m["candidate_id"] for m in b_view] == [cid]

    # `about` is always the counterpart, labelled for the viewer.
    # B only has a `card` grant, so A sees B's pseudonym — never the display name.
    assert a_view[0]["about"] != "Bea"
    assert b_view[0]["about"] == "Ava"
    assert a_view[0]["state"] == "matched"
    assert a_view[0]["historical"] is False
    assert a_view[0]["card_url"].endswith(f"/m/{cid}")
    assert "·" in a_view[0]["window"]["display_with_date"]
    assert a_view[0]["window"]["display"] in a_view[0]["window"]["display_with_date"]
    assert a_view[0]["confirmed_at"]


def test_pseudonym_used_when_name_not_published(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    row = _rows(client, a)[0]
    import hashlib

    assert row["about"] == f"climbing-{hashlib.sha256(f'pseudo:{b}'.encode()).hexdigest()[:4]}"
    assert row["about"] != "Bea"


# ------------------------------------------------------------------ never listed


def test_proposed_and_a_confirmed_never_appear(seeded, client):
    a, b, cid, pid = _pair()
    # proposal still 'proposed'
    assert _rows(client, a) == []
    _sql("UPDATE proposals SET state='a_confirmed' WHERE id=?", (pid,))
    _sql("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
    assert _rows(client, a) == []


def test_blocked_pair_absent_both_directions(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    ah, bh = db.user_hash(a), db.user_hash(b)
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (ah, bh, db.utcnow(), db.utcnow(days=30)),
            )
    finally:
        c.close()
    assert _rows(client, a) == []
    assert _rows(client, b) == []


def test_counterpart_withdraws_matching_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='matching'", (db.utcnow(), b))
    assert _rows(client, a) == []


def test_counterpart_withdraws_publication_card_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql(
        "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='publication'",
        (db.utcnow(), b),
    )
    assert _rows(client, a) == []


def test_expired_availability_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE availability SET expires_at='2000-01-01T00:00:00Z' WHERE user_id=?", (b,))
    assert _rows(client, a) == []
    assert _rows(client, b) == []


def test_hidden_candidate_absent(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE candidates SET state='hidden' WHERE id=?", (cid,))
    assert _rows(client, a) == []
    assert _rows(client, b) == []


# ------------------------------------------------------------------ current proposal


def test_counter_superseded_proposal_shows_only_current(seeded, client):
    a, b, cid, pid = _pair()
    new_start = util.to_utc("2026-10-30T18:00:00", TZ)
    new_end = util.to_utc("2026-10-30T20:00:00", TZ)
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO proposals (candidate_id, window_start, window_end, version, state,"
                " expires_at) VALUES (?,?,?,2,'both_confirmed',?)",
                (cid, new_start, new_end, db.utcnow(days=30)),
            )
            pid2 = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "UPDATE proposals SET state='voided', superseded_by=? WHERE id=?", (pid2, pid)
            )
            c.execute("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
            for uid in (a, b):
                c.execute(
                    "INSERT INTO proposal_confirmations (proposal_id, user_id, confirmed_at)"
                    " VALUES (?,?,?)",
                    (pid2, uid, db.utcnow()),
                )
    finally:
        c.close()

    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["window"]["start_utc"] == new_start
    assert rows[0]["window"]["end_utc"] == new_end


# ------------------------------------------------------------------ history


def test_past_window_is_historical_without_card_link(seeded, client):
    a, b, cid, pid = _pair(window=PAST_WINDOW)
    _confirmed(cid, pid, a, b)
    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["historical"] is True
    assert rows[0]["card_url"] is None
    # date suffix present, computed in the window's tz ('20 Oct' in America/Los_Angeles)
    assert rows[0]["window"]["display_with_date"].endswith("· 20 Oct")


def test_closed_candidate_still_listed_while_authorized(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE candidates SET state='closed' WHERE id=?", (cid,))
    _sql("UPDATE proposals SET state='closed' WHERE id=?", (pid,))
    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["state"] == "closed"


# ------------------------------------------------------------------ auth / page


def test_unauthenticated_matches_is_401(seeded, client):
    r = client.get("/api/matches")
    assert r.status_code == 401


def test_matches_page_redirects_to_login(seeded, client):
    r = client.get("/matches", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/matches"
