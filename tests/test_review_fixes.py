"""Regression tests for the mid-build reviewer findings (P1 #1-8 + P2s). Integrator-owned.

Each test names the finding it pins. Function-level tests drive the real module functions
on fresh connections (same seam the concurrency tests use); HTTP tests go through the app."""

import tests.util as util
from server import db, matching, scheduling, sweep
from server.scheduling import SchedulingError, confirm, counter, leave_matching

TZW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")  # Thu evening, LA


def _payload(windows, tags=("climbing",), matching=True, tier="card"):
    return {
        "windows": [
            {"start": s, "end": e, "tz": "America/Los_Angeles"} for (s, e) in windows
        ],
        "tags": list(tags),
        "wants": ["hangout"],
        "free_time": {"source": "student"},
        "consent": {"matching": matching, "publication": {"tier": tier} if tier else None},
    }


def _uid(conn, email):
    return db.one(conn, "SELECT id FROM users WHERE email=?", (email,))["id"]


# ---------------------------------------------------------------- P1 #1: second deletion

def test_second_account_deletion_succeeds_with_unique_tombstones(client):
    a = util.mk_user("a1@x", "Ava")
    b = util.mk_user("b1@x", "Ben")
    conn = db.connect()
    try:
        assert scheduling.delete_account(conn, a) == {"ok": True}
        assert scheduling.delete_account(conn, b) == {"ok": True}
        emails = db.q(conn, "SELECT email FROM users WHERE id IN (?,?) ORDER BY id", (a, b))
        assert emails[0]["email"] != emails[1]["email"], "tombstones must be unique"
        assert all("deleted-" in e["email"] for e in emails)
        assert all("@" in e["email"] for e in emails)
    finally:
        conn.close()


# ------------------------------------------------------- P1 #2: publication gate on matching

def test_publication_off_means_no_candidates_and_no_inbox(client):
    a = util.mk_user("a2@x", "Ava")
    b = util.mk_user("b2@x", "Ben")
    util.grant(a, "matching")
    util.grant(b, "matching")  # NO publication grants
    util.windows(a, [TZW])
    util.windows(b, [TZW])
    util.tags(a, ["climbing"])
    util.tags(b, ["climbing"])
    conn = db.connect()
    try:
        assert matching.generate_for_user(conn, a) == []
        assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates")["n"] == 0
        assert db.one(conn, "SELECT COUNT(*) AS n FROM inbox_items")["n"] == 0
        # granting publication revives generation
        util.pub_card(a)
        util.pub_card(b)
        assert matching.generate_for_user(conn, a) != []
        assert db.one(conn, "SELECT COUNT(*) AS n FROM inbox_items")["n"] == 2
    finally:
        conn.close()


# ------------------------------------------- P1 #3: settings withdrawal kills matched plans

def test_settings_withdrawal_voids_matched_plan_and_notifies(client):
    ava = util.mk_user("ava3@x", "Ava")
    ben = util.mk_user("ben3@x", "Ben")
    cid, pid = util.mk_candidate(ava, ben)
    conn = db.connect()
    try:
        confirm(conn, ava, cid, 1)
        confirm(conn, ben, cid, 1)
        assert db.one(conn, "SELECT state FROM candidates WHERE id=?", (cid,))["state"] == "matched"
    finally:
        conn.close()

    # webui settings path: matching=false must run the full cascade
    util.direct_session(client, ben)
    r = client.post("/api/settings/grants", json={"matching": False})
    assert r.status_code == 200, r.text

    conn = db.connect()
    try:
        cand = db.one(conn, "SELECT * FROM candidates WHERE id=?", (cid,))
        assert cand["state"] == "voided"
        assert cand["closed_at"] is not None
        assert db.one(conn, "SELECT state FROM proposals WHERE id=?", (pid,))["state"] == "voided"
        assert db.one(conn, "SELECT 1 FROM availability WHERE user_id=?", (ben,)) is None
        # both sides were told the plan died — including the withdrawing student
        msgs = db.q(conn, "SELECT user_id, payload_json FROM inbox_items")
        recipients = {m["user_id"] for m in msgs if "withdrawn" in m["payload_json"]}
        assert recipients == {ava, ben}
    finally:
        conn.close()


def test_leave_matching_function_covers_matched_not_just_open(client):
    a = util.mk_user("a4@x", "AA")
    b = util.mk_user("b4@x", "BB")
    cid, _ = util.mk_candidate(a, b)
    conn = db.connect()
    try:
        confirm(conn, a, cid, 1)
        confirm(conn, b, cid, 1)  # matched
        leave_matching(conn, b)
        assert db.one(conn, "SELECT state FROM candidates WHERE id=?", (cid,))["state"] == "voided"
    finally:
        conn.close()


# ---------------------------------------- P1 #4: consent.matching=false stores nothing

def test_state_matching_false_stores_nothing_and_runs_cascade(client):
    a = util.mk_user("a5@x", "AA")
    b = util.mk_user("b5@x", "BB")
    util.grant(a, "matching")
    util.windows(a, [TZW])
    util.tags(a, ["climbing"])
    token, _ = util.mint_pairing(a)
    util.windows(b, [TZW])
    util.tags(b, ["climbing"])
    util.grant(b, "matching")
    util.pub_card(a)
    util.pub_card(b)

    # a real candidate first, from b's side
    conn = db.connect()
    try:
        matching.generate_for_user(conn, b)
        assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates")["n"] == 1
    finally:
        conn.close()

    r = client.post("/api/state", json=_payload([TZW], matching=False), headers=util.auth_header(token))
    assert r.status_code == 201, r.text
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT 1 FROM availability WHERE user_id=?", (a,)) is None
        assert db.one(conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (a,))["n"] == 0
        assert db.one(conn, "SELECT state FROM candidates")["state"] == "voided"
        rows = db.q(
            conn, "SELECT * FROM consent_log WHERE user_id_hash=? AND action='withdraw:matching'",
            (db.user_hash(a),),
        )
        assert len(rows) == 1, "one refusal must write exactly ONE withdrawal audit row"
        grant = db.one(conn, "SELECT withdrawn_at FROM grants WHERE user_id=? AND kind='matching'", (a,))
        assert grant is None or grant["withdrawn_at"] is not None
    finally:
        conn.close()


# ------------------------- P1 #5: replacing availability invalidates uncovered plans

def test_replacement_voids_uncovered_plan_and_notifies_both(client):
    a = util.mk_user("a6@x", "AA")
    b = util.mk_user("b6@x", "BB")
    util.windows(a, [("2026-10-29T18:00:00", "2026-10-29T23:00:00")])
    util.windows(b, [("2026-10-29T19:00:00", "2026-10-29T23:00:00")])
    util.grant(a, "matching")
    util.grant(b, "matching")
    util.pub_card(a)
    util.pub_card(b)
    token, _ = util.mint_pairing(a)
    conn = db.connect()
    try:
        matching.generate_for_user(conn, b)
        prop = db.one(conn, "SELECT * FROM proposals")
        assert prop["window_start"].startswith("2026-10-30T02")  # 19:00 LA = 02:00Z next day
    finally:
        conn.close()

    # shrink to two windows that TOGETHER cover 19-23 but neither ALONE covers it
    r = client.post(
        "/api/state",
        json=_payload([("2026-10-29T19:00:00", "2026-10-29T21:00:00"),
                       ("2026-10-29T21:00:00", "2026-10-29T23:00:00")]),
        headers=util.auth_header(token),
    )
    assert r.status_code == 201, r.text

    conn = db.connect()
    try:
        # the old 19:00-23:00 plan died: no single window covers it
        dead = db.one(conn, "SELECT * FROM candidates WHERE state='voided'")
        assert dead is not None and dead["closed_at"] is not None
        revocations = db.q(conn, "SELECT user_id FROM inbox_items WHERE payload_json LIKE '%withdrawn%'")
        assert {row["user_id"] for row in revocations} == {a, b}
        # a fresh candidate exists over the still-shared time (b is 19-23)
        fresh = db.one(conn, "SELECT c.* FROM candidates c WHERE c.state='open'")
        assert fresh is not None
    finally:
        conn.close()


def test_replacement_keeps_covered_live_plan(client):
    a = util.mk_user("a7@x", "AA")
    b = util.mk_user("b7@x", "BB")
    util.windows(a, [TZW])
    util.windows(b, [TZW])
    util.grant(a, "matching")
    util.grant(b, "matching")
    util.pub_card(a)
    util.pub_card(b)
    token, _ = util.mint_pairing(a)
    conn = db.connect()
    try:
        matching.generate_for_user(conn, b)
    finally:
        conn.close()
    # repost a window that still contains the proposal window
    r = client.post(
        "/api/state",
        json=_payload([("2026-10-29T18:00:00", "2026-10-29T22:00:00")]),
        headers=util.auth_header(token),
    )
    assert r.status_code == 201
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE state='voided'")["n"] == 0
        assert db.one(conn, "SELECT COUNT(*) AS n FROM candidates WHERE state='open'")["n"] >= 1
    finally:
        conn.close()


# ------------------------------------- P1 #6: counter honors matching authorization

def test_counter_after_withdrawal_is_not_found(client):
    a = util.mk_user("a8@x", "AA")
    b = util.mk_user("b8@x", "BB")
    util.windows(a, [TZW])
    util.windows(b, [TZW])
    util.grant(a, "matching")
    util.grant(b, "matching")
    util.pub_card(a)
    util.pub_card(b)
    conn = db.connect()
    try:
        matching.generate_for_user(conn, a)
        cid = db.one(conn, "SELECT id FROM candidates")["id"]
        leave_matching(conn, b)
        try:
            counter(conn, a, cid, "2026-10-29T19:30:00", "2026-10-29T20:30:00", "America/Los_Angeles")
            raise AssertionError("counter must refuse after matching withdrawal")
        except SchedulingError as exc:
            assert exc.code == "not_found"
        assert db.one(conn, "SELECT COUNT(*) AS n FROM proposals WHERE version=2")["n"] == 0
    finally:
        conn.close()


def test_counter_after_block_is_not_found(client):
    a = util.mk_user("a9@x", "AA")
    b = util.mk_user("b9@x", "BB")
    cid, _ = util.mk_candidate(a, b)
    conn = db.connect()
    try:
        confirm(conn, a, cid, 1)
        # block writes rows both directions with a future expiry
        conn.execute(
            "INSERT OR REPLACE INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
            " VALUES (?,?,?,?)",
            (db.user_hash(a), db.user_hash(b), db.utcnow(), db.utcnow(days=30)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
            " VALUES (?,?,?,?)",
            (db.user_hash(b), db.user_hash(a), db.utcnow(), db.utcnow(days=30)),
        )
        try:
            counter(conn, a, cid, "2026-10-29T19:30:00", "2026-10-29T20:30:00", "America/Los_Angeles")
            raise AssertionError("counter must refuse after a block")
        except SchedulingError as exc:
            assert exc.code == "not_found"
    finally:
        conn.close()


# --------------------------------- P1 #7/#8: retention epoch + hidden candidates

def test_terminal_retention_uses_closed_at_and_includes_hidden(client):
    a = util.mk_user("a10@x", "AA")
    b = util.mk_user("b10@x", "BB")
    old = util.mk_candidate(a, b)[0]
    fresh_closed = util.mk_candidate(a, b)[0]
    hidden_old = util.mk_candidate(a, b)[0]
    kept_matched = util.mk_candidate(a, b)[0]
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute("UPDATE candidates SET state='closed', closed_at=? WHERE id=?", (db.utcnow(days=-31), old))
            conn.execute("UPDATE candidates SET state='closed', closed_at=? WHERE id=?", (db.utcnow(days=-1), fresh_closed))
            conn.execute("UPDATE candidates SET state='hidden', closed_at=? WHERE id=?", (db.utcnow(days=-31), hidden_old))
            # matched with a future window: terminal delete must never touch it
            conn.execute("UPDATE candidates SET state='matched' WHERE id=?", (kept_matched,))
        counts = sweep.run_sweep(conn)
        assert counts["terminal_candidates_deleted"] == 2  # old + hidden_old
        assert db.one(conn, "SELECT state FROM candidates WHERE id=?", (fresh_closed,))["state"] == "closed"
        assert db.one(conn, "SELECT state FROM candidates WHERE id=?", (kept_matched,))["state"] == "matched"
    finally:
        conn.close()


# ---------------------- P2: expired open candidate no longer blocks regeneration

def test_expired_open_candidate_does_not_suppress_fresh_matching(client):
    a = util.mk_user("a11@x", "AA")
    b = util.mk_user("b11@x", "BB")
    util.windows(a, [TZW])
    util.windows(b, [TZW])
    util.grant(a, "matching")
    util.grant(b, "matching")
    util.pub_card(a)
    util.pub_card(b)
    util.tags(a, ["climbing"])
    util.tags(b, ["climbing"])
    conn = db.connect()
    try:
        assert matching.generate_for_user(conn, a) != []
        stale = db.one(conn, "SELECT id, expires_at FROM candidates")
        with db.immediate(conn):
            conn.execute("UPDATE candidates SET expires_at=? WHERE id=?", (db.utcnow(days=-1), stale["id"]))
        # fresh windows, same overlap → a NEW candidate must appear despite the stale row
        util.windows(a, [TZW])
        util.windows(b, [TZW])
        new_ids = matching.generate_for_user(conn, b)
        assert new_ids, "expired open candidate must not suppress regeneration"
    finally:
        conn.close()
