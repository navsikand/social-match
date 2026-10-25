"""P2b concurrency + scheduling tests (CONTRACT §8, §13).

Race tests drive the plain transition functions on their own `db.connect()` connections from real
threads with a `threading.Barrier` — that is the actual serialization point (SQLite's write lock),
not TestClient-in-threads. Behaviour tests go through the HTTP endpoints with a signed session.
"""
import threading

import pytest

from server import db
from server import scheduling as sched
from tests import util

WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")
TZ = "America/Los_Angeles"


# ------------------------------------------------------------------ helpers


@pytest.fixture()
def seeded(db_env):
    db.init_db()
    return db_env


def _conn():
    return db.connect()


def _one(sql, args=()):
    c = _conn()
    try:
        return db.one(c, sql, args)
    finally:
        c.close()


def _all(sql, args=()):
    c = _conn()
    try:
        return db.q(c, sql, args)
    finally:
        c.close()


def _proposal_state(pid: int) -> str:
    return _one("SELECT state FROM proposals WHERE id=?", (pid,))["state"]


def _candidate_state(cid: int) -> str:
    return _one("SELECT state FROM candidates WHERE id=?", (cid,))["state"]


def _inbox(user_id: int, kind: str) -> list:
    return _all(
        "SELECT * FROM inbox_items WHERE user_id=? AND kind=? ORDER BY seq", (user_id, kind)
    )


def _swipe(client, uid, cid, version, direction):
    util.direct_session(client, uid)
    return client.post(
        "/api/swipe",
        json={"candidate_id": cid, "proposal_version": version, "direction": direction},
    )


def _err(resp) -> str:
    body = resp.json()
    return body.get("error") or body.get("detail", {}).get("error", "")


# ------------------------------------------------------------------ revoke intent ordering


def test_intent_then_confirm_409(seeded):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO revoke_intents (user_id, created_at) VALUES (?,?)", (b, db.utcnow())
        )
        with pytest.raises(sched.SchedulingError) as exc:
            sched.confirm(conn, a, cid, 1)
    finally:
        conn.close()

    assert exc.value.status == 409
    assert exc.value.code == "intent_present"
    assert _proposal_state(pid) == "proposed"


def test_confirm_then_revoke_interleaving_voids(seeded):
    """Deterministic: confirmation commits first, revoke must still erase it."""
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    conn = _conn()
    try:
        assert sched.confirm(conn, a, cid, 1)["state"] == "a_confirmed"
        assert sched.revoke(conn, b, cid)["state"] == "voided"
    finally:
        conn.close()

    assert _proposal_state(pid) == "voided"
    assert _candidate_state(cid) == "voided"


def test_revoke_then_confirm_interleaving_voids(seeded):
    """Deterministic: the intent row exists first; the confirmation may not survive."""
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    conn = _conn()
    try:
        conn.execute("INSERT INTO revoke_intents (user_id, created_at) VALUES (?,?)", (b, db.utcnow()))
        with pytest.raises(sched.SchedulingError) as exc:
            sched.confirm(conn, a, cid, 1)
        assert exc.value.code == "intent_present"
        sched.revoke(conn, b, cid)
    finally:
        conn.close()

    assert _proposal_state(pid) == "voided"
    assert _candidate_state(cid) == "voided"


def test_race_confirm_vs_revoke_real_threads(seeded):
    """Real threads race a contiguous window on two connections; end state must always be voided."""
    for i in range(6):
        a = util.mk_user(f"a{i}@x.test", "Ava")
        b = util.mk_user(f"b{i}@x.test", "Ben")
        cid, pid = util.mk_candidate(a, b, WINDOW)

        barrier = threading.Barrier(2)
        out: dict = {}

        # Bind the loop variables as defaults so each thread closes over its own pair (B023).
        def do_confirm(a=a, cid=cid, barrier=barrier, out=out):
            conn = db.connect()
            try:
                barrier.wait()
                try:
                    out["confirm"] = sched.confirm(conn, a, cid, 1)
                except sched.SchedulingError as exc:
                    out["confirm_error"] = exc.code
            finally:
                conn.close()

        def do_revoke(b=b, cid=cid, barrier=barrier, out=out):
            conn = db.connect()
            try:
                barrier.wait()
                out["revoke"] = sched.revoke(conn, b, cid)
            finally:
                conn.close()

        # Alternate which thread starts first; the barrier still releases both together. Measured
        # behaviour: confirm reliably reaches BEGIN IMMEDIATE first, so this exercises the
        # "confirmation commits, revoke voids it" order. The mirror order (intent row already
        # committed → 409) is covered deterministically by the two tests above.
        pair = [threading.Thread(target=do_confirm), threading.Thread(target=do_revoke)]
        threads = pair if i % 2 == 0 else pair[::-1]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "a racing thread deadlocked"

        # Whichever side won, the surviving state is voided — never confirmed.
        assert _proposal_state(pid) == "voided", out
        assert _candidate_state(cid) == "voided", out
        if "confirm_error" in out:
            # Post-revoke, confirm may legitimately see either the intent row (409
            # intent_present) or the already-voided candidate (404 not_found). Both are
            # correct revoke-wins outcomes; the end state below is what must hold.
            assert out["confirm_error"] in ("intent_present", "not_found"), out


# ------------------------------------------------------------------ confirmation idempotency


def test_duplicate_confirm_is_idempotent(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    assert _swipe(client, a, cid, 1, "right").json()["state"] == "a_confirmed"
    assert _swipe(client, b, cid, 1, "right").json()["state"] == "both_confirmed"

    again = _swipe(client, b, cid, 1, "right")
    assert again.status_code == 200, again.text
    assert again.json()["state"] == "both_confirmed"

    rows = _all("SELECT * FROM proposal_confirmations WHERE proposal_id=?", (pid,))
    assert len(rows) == 2
    assert len(_inbox(b, "match")) == 1  # no second inbox flood
    assert _candidate_state(cid) == "matched"


def test_stale_version_409(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    resp = _swipe(client, a, cid, 2, "right")
    assert resp.status_code == 409
    assert _err(resp) == "stale_version"
    assert _proposal_state(pid) == "proposed"


def test_overlapping_slot_conflict(seeded):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    c = util.mk_user("c@x.test", "Cy")
    cid1, _ = util.mk_candidate(a, b, WINDOW)
    cid2, _ = util.mk_candidate(a, c, WINDOW)

    conn = _conn()
    try:
        assert sched.confirm(conn, a, cid1, 1)["state"] == "a_confirmed"
        with pytest.raises(sched.SchedulingError) as exc:
            sched.confirm(conn, a, cid2, 1)
    finally:
        conn.close()

    assert exc.value.status == 409
    assert exc.value.code == "slot_conflict"


def test_second_direction_swipe_409(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, _ = util.mk_candidate(a, b, WINDOW)

    assert _swipe(client, a, cid, 1, "right").status_code == 200
    resp = _swipe(client, a, cid, 1, "left")
    assert resp.status_code == 409
    assert _err(resp) == "already_swiped"


def test_swipe_on_declined_404(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, _ = util.mk_candidate(a, b, WINDOW)

    assert _swipe(client, b, cid, 1, "left").status_code == 200
    assert _candidate_state(cid) == "declined"
    resp = _swipe(client, a, cid, 1, "right")
    assert resp.status_code == 404


# ------------------------------------------------------------------ block / report


def test_block_404s_future_card_and_blocks_both_directions(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    assert _swipe(client, a, cid, 1, "block").status_code == 200
    assert _candidate_state(cid) == "voided"

    ah, bh = db.user_hash(a), db.user_hash(b)
    rows = _all("SELECT * FROM blocks")
    assert len(rows) == 2
    pairs = {(r["user_hash"], r["blocked_user_hash"]) for r in rows}
    assert pairs == {(ah, bh), (bh, ah)}
    assert all(r["expires_at"] > db.utcnow() for r in rows)

    card = client.get(f"/api/card/{cid}")  # still sitting on b's session
    assert card.status_code == 404

    # revocation inbox to the counterpart only
    assert len(_inbox(b, "revocation")) == 1
    assert len(_inbox(a, "revocation")) == 0


def test_report_hides_without_blocking(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, _ = util.mk_candidate(a, b, WINDOW)

    assert _swipe(client, a, cid, 1, "report").status_code == 200
    assert _candidate_state(cid) == "hidden"
    assert len(_all("SELECT * FROM reports")) == 1
    assert _all("SELECT * FROM blocks") == []
    assert len(_all("SELECT * FROM inbox_items")) == 0
    assert client.get(f"/api/card/{cid}").status_code == 404


# ------------------------------------------------------------------ counter


def test_counter_voids_old_and_bumps_version(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)
    assert _swipe(client, a, cid, 1, "right").status_code == 200

    util.direct_session(client, b)
    resp = client.post(
        "/api/counter",
        json={
            "candidate_id": cid,
            "window_start": "2026-10-29T19:30",
            "window_end": "2026-10-29T20:30",
            "tz": TZ,
        },
    )
    assert resp.status_code == 200, resp.text

    old = _one("SELECT * FROM proposals WHERE id=?", (pid,))
    new = _one("SELECT * FROM proposals WHERE candidate_id=? AND version=2", (cid,))
    assert new is not None
    assert old["state"] == "voided"
    assert old["superseded_by"] == new["id"]
    assert new["state"] == "proposed"
    assert new["expires_at"] == _one("SELECT expires_at FROM candidates WHERE id=?", (cid,))["expires_at"]
    # confirmations do not carry to the new version
    assert _all("SELECT * FROM proposal_confirmations WHERE proposal_id=?", (new["id"],)) == []
    assert len(_inbox(a, "counter")) == 1


def test_counter_outside_free_time_422(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, _ = util.mk_candidate(a, b, WINDOW)

    util.direct_session(client, a)
    resp = client.post(
        "/api/counter",
        json={
            "candidate_id": cid,
            "window_start": "2026-10-29T12:00",
            "window_end": "2026-10-29T13:00",
            "tz": TZ,
        },
    )
    assert resp.status_code == 422
    assert _err(resp) == "payload_forbidden"


# ------------------------------------------------------------------ card + dashboard


def test_card_shape_and_non_counterpart_404(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    outsider = util.mk_user("c@x.test", "Cy")
    cid, pid = util.mk_candidate(a, b, WINDOW)

    util.direct_session(client, a)  # viewer a → card is about b (publication tier 'card')
    card = client.get(f"/api/card/{cid}")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["candidate_id"] == cid
    assert body["proposal_id"] == pid
    assert body["proposal_version"] == 1
    assert body["about"]["display_name"] is None  # b granted only 'card'
    assert body["about"]["pseudonym"]
    assert body["about"]["preset_avatar_id"] == 1
    assert body["about"]["hobby_tags"] == ["climbing"]
    assert body["window"]["tz"] == TZ
    assert body["window"]["display"] == "Thu 19:00–21:00"
    assert body["card_url"].endswith(f"/m/{cid}")
    assert body["state"] == "open"

    util.direct_session(client, outsider)
    assert client.get(f"/api/card/{cid}").status_code == 404


def test_dashboard_lists_open_candidates(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, _ = util.mk_candidate(a, b, WINDOW)

    util.direct_session(client, a)
    body = client.get("/api/dashboard").json()
    assert [c["candidate_id"] for c in body["candidates"]] == [cid]
    assert body["candidates"][0]["card_url"].endswith(f"/m/{cid}")
    assert body["candidates"][0]["state"] == "open"


# ------------------------------------------------------------------ leave-matching


def test_leave_matching_cascades_and_notifies_counterpart(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    cid, pid = util.mk_candidate(a, b, WINDOW)
    assert _swipe(client, a, cid, 1, "right").status_code == 200

    util.direct_session(client, a)
    resp = client.post("/api/leave-matching")
    assert resp.status_code == 200 and resp.json() == {"ok": True}

    assert _candidate_state(cid) == "voided"
    assert _proposal_state(pid) == "voided"
    assert _one("SELECT * FROM availability WHERE user_id=?", (a,)) is None
    assert _all("SELECT * FROM availability_windows WHERE user_id=?", (a,)) == []
    matching = _one(
        "SELECT * FROM grants WHERE user_id=? AND kind='matching' AND withdrawn_at IS NULL", (a,)
    )
    assert matching is None
    assert len(_inbox(b, "revocation")) == 1
    # publication grant untouched
    assert _one("SELECT * FROM grants WHERE user_id=? AND kind='publication' AND withdrawn_at IS NULL", (a,))


# ------------------------------------------------------------------ delete account


def test_delete_account_cascade(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    b = util.mk_user("b@x.test", "Ben")
    c = util.mk_user("c@x.test", "Cy")
    cid, pid = util.mk_candidate(a, b, WINDOW)
    assert _swipe(client, a, cid, 1, "right").status_code == 200

    util.mint_pairing(a)
    db.init_db()  # no-op, keeps the schema warm for the direct writes below
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (db.user_hash(a), "state:received", "state", '{"payload_hash": "deadbeef"}', db.utcnow()),
        )
        conn.execute("INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                     " VALUES (?,?,?,?)",
                     (db.user_hash(a), db.user_hash(c), db.utcnow(), db.utcnow(days=365)))
        conn.execute("INSERT INTO reports (reporter_id, candidate_id, created_at, state)"
                     " VALUES (?,?,?,'open')", (a, cid, db.utcnow()))
        db.push_inbox(conn, a, "candidate", {"message": "someone is up for something"})
    finally:
        conn.close()

    util.direct_session(client, a)
    resp = client.post("/api/delete-account", json={"confirm": True})
    assert resp.status_code == 200 and resp.json() == {"ok": True}

    user = _one("SELECT * FROM users WHERE id=?", (a,))
    # Integrator update (reviewer P1 #1): tombstones are unique + non-identifying, not '' —
    # two deletions would otherwise collide on UNIQUE(users.email) and roll back the second.
    assert user["email"].startswith("deleted-") and user["email"].endswith("@invalid")
    assert user["display_name"] == "deleted user"
    assert user["password_hash"] == ""
    assert user["deleted_at"] is not None

    assert _one("SELECT * FROM availability WHERE user_id=?", (a,)) is None
    assert _all("SELECT * FROM availability_windows WHERE user_id=?", (a,)) == []
    assert _all("SELECT * FROM hobby_tags WHERE user_id=?", (a,)) == []
    assert _all("SELECT * FROM swipes WHERE user_id=?", (a,)) == []
    assert _all("SELECT * FROM inbox_items WHERE user_id=?", (a,)) == []
    assert _one("SELECT * FROM grants WHERE user_id=? AND withdrawn_at IS NULL", (a,)) is None
    assert _one("SELECT * FROM pairings WHERE user_id=? AND revoked_at IS NULL", (a,)) is None
    assert _proposal_state(pid) == "voided"
    assert _candidate_state(cid) == "voided"
    assert _all("SELECT * FROM proposal_confirmations WHERE proposal_id=?", (pid,)) == []

    residue = _all("SELECT * FROM consent_log WHERE user_id_hash=?", (db.user_hash(a),))
    assert residue
    assert all(r["detail_json"] == "{}" for r in residue)
    assert all(r["purge_after"] is not None for r in residue)
    assert all(r["purge_after"] > db.utcnow() for r in residue)

    # blocks and reports are pseudonymous residue and survive
    assert len(_all("SELECT * FROM blocks")) == 1
    assert len(_all("SELECT * FROM reports")) == 1


def test_delete_account_requires_confirm(seeded, client):
    a = util.mk_user("a@x.test", "Ava")
    util.direct_session(client, a)
    resp = client.post("/api/delete-account", json={"confirm": False})
    assert resp.status_code == 422
    assert _err(resp) == "payload_forbidden"
    assert _one("SELECT deleted_at FROM users WHERE id=?", (a,))["deleted_at"] is None
