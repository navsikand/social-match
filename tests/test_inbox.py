"""P2c: GET /api/inbox cursor + last_seen bump, wording asymmetry, and the sweep (§9/§10/§13)."""
import json
import re

import pytest

from server import db, sweep, wording
from server.wording import candidate_text, confirmation_text
from tests import util
from tests.util import auth_header, mint_pairing, mk_user, pub_card, pub_name

TZ = "America/Los_Angeles"
WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")
START_UTC = util.to_utc(WINDOW[0], TZ)
END_UTC = util.to_utc(WINDOW[1], TZ)


# ------------------------------------------------------------------ helpers


def _err_code(resp) -> str:
    body = resp.json()
    return body["error"] if "error" in body else body["detail"]["error"]


def _push(uid: int, kind: str, payload: dict) -> None:
    c = util.conn()
    try:
        db.push_inbox(c, uid, kind, payload)
    finally:
        c.close()


def _pairing_row(agent_id: str):
    c = util.conn()
    try:
        return db.one(c, "SELECT * FROM pairings WHERE agent_id=?", (agent_id,))
    finally:
        c.close()


def _inbox(client, token: str, since: int | None = None):
    url = "/api/inbox" if since is None else f"/api/inbox?since={since}"
    return client.get(url, headers=auth_header(token))


# ------------------------------------------------------------------ cursor


def test_cursor_since_is_monotonic_and_per_user(client):
    uid = mk_user("ava@example.com", "Ava")
    other = mk_user("ben@example.com", "Ben")
    token, _agent = mint_pairing(uid)

    for i in range(3):
        _push(uid, "candidate", {"kind": "candidate", "message": f"m{i}", "card_url": "/m/1"})
    # Another user's items interleave in the global seq space: must never leak.
    _push(other, "candidate", {"kind": "candidate", "message": "theirs"})

    r = _inbox(client, token)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "now"}
    assert [i["payload"]["message"] for i in body["items"]] == ["m0", "m1", "m2"]
    seqs = [i["seq"] for i in body["items"]]
    assert seqs == sorted(seqs), "ascending by seq"
    assert all({"seq", "kind", "payload", "created_at"} <= set(i) for i in body["items"])
    assert all(i["kind"] == "candidate" for i in body["items"])
    assert all(i["payload"]["message"] != "theirs" for i in body["items"])

    mid = seqs[1]
    r = _inbox(client, token, since=mid)
    assert [i["seq"] for i in r.json()["items"]] == [seqs[2]], "strictly greater than the cursor"

    r = _inbox(client, token, since=seqs[-1])
    assert r.json()["items"] == [], "cursor at max → nothing new"

    r = _inbox(client, token, since=0)
    assert [i["seq"] for i in r.json()["items"]] == seqs


def test_inbox_requires_valid_bearer(client):
    assert _err_code(_inbox(client, "nope-not-a-token")) == "token_invalid"


def test_inbox_bumps_last_seen_to_response_now(client):
    uid = mk_user("ava@example.com", "Ava")
    token, agent_id = mint_pairing(uid)
    c = util.conn()
    try:
        c.execute("UPDATE pairings SET last_seen_at=? WHERE agent_id=?", ("2000-01-01T00:00:00Z", agent_id))
    finally:
        c.close()

    body = _inbox(client, token).json()
    assert _pairing_row(agent_id)["last_seen_at"] == body["now"]
    assert body["now"] != "2000-01-01T00:00:00Z"


def test_inbox_bump_touches_only_the_calling_pairing(client):
    uid = mk_user("ava@example.com", "Ava")
    token_a, agent_a = mint_pairing(uid, "laptop")
    _token_b, agent_b = mint_pairing(uid, "phone")
    _inbox(client, token_a)
    assert _pairing_row(agent_a)["last_seen_at"] is not None
    assert _pairing_row(agent_b)["last_seen_at"] == _pairing_row(agent_a)["created_at"]


# ------------------------------------------------------------------ wording asymmetry (end-to-end)


def test_candidate_label_asymmetry_end_to_end(client):
    """A published card+name, B published card only; a third user's inbox shows both — and the
    asymmetry is decided at compose time by wording.label (CONTRACT §7/§9)."""
    a = mk_user("ava@example.com", "Ava")
    b = mk_user("ben@example.com", "Ben")
    reader = mk_user("cy@example.com", "Cy")

    pub_name(a)
    pub_card(b)
    util.windows(a, [WINDOW], tz=TZ)
    util.windows(b, [WINDOW], tz=TZ)
    util.tags(a, ["climbing"])
    util.tags(b, ["live_music"])

    c = util.conn()
    try:
        pseudo_b = wording.pseudonym_for(c, b)
        text_a = candidate_text(c, a, START_UTC, END_UTC, TZ, 7)
        text_b = candidate_text(c, b, START_UTC, END_UTC, TZ, 7)
        for payload in (text_a, text_b):
            db.push_inbox(c, reader, payload["kind"], payload)
    finally:
        c.close()

    token, _agent = mint_pairing(reader)
    items = _inbox(client, token).json()["items"]
    assert len(items) == 2
    about_a, about_b = items[0]["payload"], items[1]["payload"]

    # A granted card+name → display name in the message and as `about`.
    assert about_a["about"] == "Ava"
    assert "Ava is up for " in about_a["message"]
    assert about_a["card_url"].endswith("/m/7")
    assert about_a["card_url"] in about_a["message"]

    # B granted card only → the pseudonym, tag-<hash>, never the display name.
    assert re.fullmatch(r"live_music-[0-9a-f]{4}", pseudo_b), pseudo_b
    assert about_b["about"] == pseudo_b
    assert f"{pseudo_b} is up for " in about_b["message"]
    assert "Ben" not in about_b["message"]

    # Neither message carries the other party's display name.
    assert "Ben" not in about_a["message"]
    assert "Ava" not in about_b["message"]


def test_confirmation_text_and_no_premature_youre_on(client):
    a = mk_user("ava@example.com", "Ava")
    b = mk_user("ben@example.com", "Ben")
    pub_name(a)
    pub_card(b)
    util.windows(a, [WINDOW], tz=TZ)
    util.windows(b, [WINDOW], tz=TZ)
    util.tags(a, ["climbing"])
    util.tags(b, ["live_music"])

    c = util.conn()
    try:
        samples = [
            candidate_text(c, a, START_UTC, END_UTC, TZ, 3),
            candidate_text(c, b, START_UTC, END_UTC, TZ, 3),
            confirmation_text(c, a, START_UTC, END_UTC, TZ, 3),
            confirmation_text(c, b, START_UTC, END_UTC, TZ, 3),
            wording.counter_text(c, a, START_UTC, END_UTC, TZ, 3),
            wording.counter_text(c, b, START_UTC, END_UTC, TZ, 3),
            wording.match_text(c, a, START_UTC, END_UTC, TZ),
            wording.revocation_text(START_UTC, END_UTC, TZ),
        ]
        conf_a = confirmation_text(c, a, START_UTC, END_UTC, TZ, 3)
        conf_b = confirmation_text(c, b, START_UTC, END_UTC, TZ, 3)
        db.push_inbox(c, a, "confirmation", conf_a)
    finally:
        c.close()

    # CONTRACT §9: confirmation asks for the reader's side, it never declares a match.
    for conf in (conf_a, conf_b):
        assert "confirm your side" in conf["message"]
        assert conf["kind"] == "confirmation"
        assert conf["card_url"] and conf["card_url"] in conf["message"]
        assert "confirmed " in conf["message"]

    for sample in samples:
        msg = sample["message"]
        assert msg, sample
        assert "you're on" not in msg.lower(), f"premature match wording: {msg}"
        assert "you’re on" not in msg.lower(), f"premature match wording: {msg}"

    # It survives the round trip through the inbox JSON.
    token, _agent = mint_pairing(a)
    item = _inbox(client, token).json()["items"][0]
    assert item["payload"]["kind"] == "confirmation"
    assert "confirm your side" in item["payload"]["message"]
    assert "you're on" not in item["payload"]["message"].lower()
    assert json.loads(json.dumps(item))["payload"]["card_url"] == item["payload"]["card_url"]


def test_inbox_is_a_log_not_a_live_view(client):
    """Voiding a candidate does not retract the item that announced it (documented in inbox.py)."""
    uid = mk_user("ava@example.com", "Ava")
    token, _agent = mint_pairing(uid)
    cid, _pid = util.mk_candidate(uid, mk_user("ben@example.com", "Ben"))
    c = util.conn()
    try:
        text = candidate_text(c, uid, START_UTC, END_UTC, TZ, cid)
        db.push_inbox(c, uid, "candidate", text)
        c.execute("UPDATE candidates SET state='voided' WHERE id=?", (cid,))
    finally:
        c.close()
    items = _inbox(client, token).json()["items"]
    assert [i["payload"]["candidate_id"] for i in items] == [cid]
    assert items[0]["payload"]["message"] == text["message"]


# ------------------------------------------------------------------ sweep (§10)


@pytest.fixture()
def seeded(db_env):
    db.init_db()
    return db_env


def _ids():
    return {
        "avail_expired": mk_user("gone@example.com", "Gone"),
        "cand_expired": mk_user("cand@example.com", "Cand"),
        "prop_expired": mk_user("prop@example.com", "Prop"),
        "closed": mk_user("closed@example.com", "Closed"),
    }


def test_run_sweep_expiry_and_purge(seeded):
    ids = _ids()
    past = db.utcnow(days=-1)
    future = db.utcnow(days=30)
    # a window that has demonstrably already happened (real DB times, not the fixture window)
    done_start, done_end = db.utcnow(days=-3), db.utcnow(days=-2)
    # fresh availability that must survive (own connection → seed before opening our txn)
    util.windows(ids["cand_expired"], [WINDOW], tz=TZ)
    c = util.conn()
    try:
        with db.immediate(c):
            # 1. expired availability (+ its windows must cascade away)
            c.execute(
                "INSERT INTO availability (user_id, written_by_agent_id, payload_hash, observed_at,"
                " expires_at, source) VALUES (?,?,?,?,?,'student')",
                (ids["avail_expired"], None, "h", past, past),
            )
            c.execute(
                "INSERT INTO availability_windows (user_id, starts_at, ends_at, tz) VALUES (?,?,?,?)",
                (ids["avail_expired"], START_UTC, END_UTC, TZ),
            )

            # 2. open candidate past expires_at → voided
            c.execute(
                "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at,"
                " expires_at, state) VALUES (?,?,8,'{}',?,?,'open')",
                (ids["cand_expired"], ids["prop_expired"], past, past),
            )
            cid_open = c.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 3. proposal past expires_at → voided (candidate itself still open/fresh)
            c.execute(
                "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at,"
                " expires_at, state) VALUES (?,?,8,'{}',?,?,'open')",
                (ids["prop_expired"], ids["closed"], future, future),
            )
            cid_prop = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO proposals (candidate_id, window_start, window_end, version, state,"
                " expires_at) VALUES (?,?,?,1,'proposed',?)",
                (cid_prop, START_UTC, END_UTC, past),
            )
            pid_expired = c.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 4. both_confirmed whose window has passed → closed, and its candidate → closed
            c.execute(
                "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at,"
                " expires_at, state) VALUES (?,?,8,'{}',?,?,'matched')",
                (ids["closed"], ids["prop_expired"], future, future),
            )
            cid_matched = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO proposals (candidate_id, window_start, window_end, version, state,"
                " expires_at) VALUES (?,?,?,1,'both_confirmed',?)",
                (cid_matched, done_start, done_end, future),
            )
            pid_closed = c.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 5. expired block row vs fresh one
            c.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES ('dead','beef',?,?)",
                (past, past),
            )
            c.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES ('live','beef',?,?)",
                (db.utcnow(), future),
            )

            # 6. consent_log past purge_after vs keep-forever (NULL) vs future purge
            c.execute(
                "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at,"
                " purge_after) VALUES ('h1','delete','account','{}',?,?)",
                (past, past),
            )
            c.execute(
                "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at,"
                " purge_after) VALUES ('h2','publish','grants','{}',?,NULL)",
                (past,),
            )
            c.execute(
                "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at,"
                " purge_after) VALUES ('h3','delete','account','{}',?,?)",
                (past, future),
            )

        counts = sweep.run_sweep(c)

        # availability gone, windows cascaded
        assert db.one(c, "SELECT 1 FROM availability WHERE user_id=?", (ids["avail_expired"],)) is None
        assert db.q(c, "SELECT 1 FROM availability_windows WHERE user_id=?", (ids["avail_expired"],)) == []
        # fresh availability + windows untouched
        assert db.one(c, "SELECT 1 FROM availability WHERE user_id=?", (ids["cand_expired"],)) is not None
        assert len(db.q(c, "SELECT 1 FROM availability_windows WHERE user_id=?", (ids["cand_expired"],))) == 1

        assert db.one(c, "SELECT state FROM candidates WHERE id=?", (cid_open,))["state"] == "voided"
        assert db.one(c, "SELECT state FROM proposals WHERE id=?", (pid_expired,))["state"] == "voided"
        assert db.one(c, "SELECT state FROM candidates WHERE id=?", (cid_prop,))["state"] == "open"
        assert db.one(c, "SELECT state FROM proposals WHERE id=?", (pid_closed,))["state"] == "closed"
        assert db.one(c, "SELECT state FROM candidates WHERE id=?", (cid_matched,))["state"] == "closed"

        assert db.q(c, "SELECT 1 FROM blocks WHERE user_hash='dead'") == []
        assert len(db.q(c, "SELECT 1 FROM blocks WHERE user_hash='live'")) == 1

        assert [r["user_id_hash"] for r in db.q(c, "SELECT user_id_hash FROM consent_log ORDER BY id")] == [
            "h2",
            "h3",
        ]

        assert counts["availability_deleted"] >= 1
        assert counts["candidates_voided"] == 1
        assert counts["proposals_voided"] == 1
        assert counts["proposals_closed"] == 1
        assert counts["candidates_closed"] == 1
        assert counts["blocks_deleted"] == 1
        assert counts["consent_purged"] == 1
    finally:
        c.close()


def test_run_sweep_is_idempotent(seeded):
    uid = mk_user("ava@example.com", "Ava")
    other = mk_user("ben@example.com", "Ben")
    past = db.utcnow(days=-1)
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at,"
                " expires_at, state) VALUES (?,?,8,'{}',?,?,'open')",
                (uid, other, past, past),
            )
        first = sweep.run_sweep(c)
        assert first["candidates_voided"] == 1
        second = sweep.run_sweep(c)
        assert all(v == 0 for v in second.values()), second
    finally:
        c.close()
