"""P1a pairing + bearer auth tests (CONTRACT §4, §13)."""
import hashlib
import re

from fastapi import Request

from server import auth, db, pairing
from tests.util import auth_header, direct_session, grant, mint_pairing, mk_user, windows

CODE_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$")
WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")


# ------------------------------------------------------------------ helpers


def _err_code(resp) -> str:
    """CONTRACT §2 wants {"error": ...}; the frozen db.http_error nests it under "detail"."""
    body = resp.json()
    if "error" in body:
        return body["error"]
    return body["detail"]["error"]


def _sha(raw: str) -> str:
    return hashlib.sha256(pairing.normalize_code(raw).encode()).hexdigest()


def _ident(token: str):
    """Resolve a bearer token through the real dependency without needing a bearer endpoint."""
    req = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    conn = db.connect()
    try:
        return auth.get_identity(req, conn)
    finally:
        conn.close()


def _start(client, label=None):
    body = {} if label is None else {"label": label}
    r = client.post("/api/pair/start", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _pair(client, uid):
    """Full happy-path pairing: start → approve → finish. Returns (token, agent_id)."""
    code = _start(client)["code"]
    direct_session(client, uid)
    assert client.post("/api/pair/approve", json={"code": code}).status_code == 200
    r = client.post("/api/pair/finish", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["token"], body["agent_id"]


def _availability_agents() -> list[str]:
    conn = db.connect()
    try:
        return [r["written_by_agent_id"] for r in db.q(conn, "SELECT written_by_agent_id FROM availability")]
    finally:
        conn.close()


def _window_count(uid: int) -> int:
    conn = db.connect()
    try:
        return db.one(
            conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,)
        )["n"]
    finally:
        conn.close()


def _pairing_row(agent_id: str):
    conn = db.connect()
    try:
        return db.one(conn, "SELECT * FROM pairings WHERE agent_id=?", (agent_id,))
    finally:
        conn.close()


# ------------------------------------------------------------------ code basics


def test_code_alphabet_format_and_storage(client, db_env):
    assert len(pairing.ALPHABET) == 32
    assert not set("ILOU") & set(pairing.ALPHABET)

    body = _start(client, label="phone")
    assert set(body) == {"code", "url", "expires_in"}
    assert CODE_RE.match(body["code"]), body["code"]
    assert body["expires_in"] == 300
    assert body["url"].endswith("/m/pair/" + body["code"])

    conn = db.connect()
    try:
        row = db.one(
            conn,
            "SELECT kind, user_id, used_at FROM pairing_codes WHERE code_hash=?",
            (_sha(body["code"]),),
        )
    finally:
        conn.close()
    assert row is not None, "code must be stored as sha256 of the dash-stripped uppercase string"
    assert row["kind"] == "link"
    assert row["user_id"] is None
    assert row["used_at"] is None


# ------------------------------------------------------------------ happy path


def test_start_approve_finish_happy_path(client):
    uid = mk_user("ava@example.com", "Ava")

    started = _start(client)
    code = started["code"]

    direct_session(client, uid)
    r = client.post("/api/pair/approve", json={"code": code.lower()})  # lowercase must normalize
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    r = client.post("/api/pair/finish", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"token", "agent_id"}
    token, agent_id = body["token"], body["agent_id"]
    assert len(token) == 32
    assert agent_id.startswith("ag_")

    row = _pairing_row(agent_id)
    assert row["user_id"] == uid
    assert row["token_hash"] == db.hash_token(token)
    assert row["revoked_at"] is None

    # the minted token actually authenticates
    assert _ident(token) == (uid, agent_id)

    # and rotates over HTTP
    r = client.post("/api/pair/rotate", headers=auth_header(token))
    assert r.status_code == 200, r.text
    assert r.json()["agent_id"] == agent_id


def test_finish_before_approve_is_not_found(client):
    code = _start(client)["code"]
    r = client.post("/api/pair/finish", json={"code": code})
    assert r.status_code == 404
    assert _err_code(r) == "code_invalid"


def test_unknown_code_approve_and_finish_404(client):
    uid = mk_user("ava@example.com", "Ava")
    direct_session(client, uid)
    r = client.post("/api/pair/approve", json={"code": "ZZZZ-ZZZZ"})
    assert r.status_code == 404
    assert _err_code(r) == "code_invalid"
    r = client.post("/api/pair/finish", json={"code": "ZZZZ-ZZZZ"})
    assert r.status_code == 404
    assert _err_code(r) == "code_invalid"


# ------------------------------------------------------------------ single use


def test_second_finish_returns_410_code_used(client):
    uid = mk_user("ava@example.com", "Ava")
    code = _start(client)["code"]
    direct_session(client, uid)
    assert client.post("/api/pair/approve", json={"code": code}).status_code == 200
    first = client.post("/api/pair/finish", json={"code": code})
    assert first.status_code == 200

    second = client.post("/api/pair/finish", json={"code": code})
    assert second.status_code == 410
    assert _err_code(second) == "code_used"

    third = client.post("/api/pair/finish", json={"code": code.replace("-", "")})
    assert third.status_code == 410
    assert _err_code(third) == "code_used"


# ------------------------------------------------------------------ expiry


def test_expired_code_after_ttl(client):
    uid = mk_user("ava@example.com", "Ava")
    saved, db._FIXED_NOW = db._FIXED_NOW, "2026-10-24T12:00:00Z"
    try:
        code = _start(client)["code"]
        conn = db.connect()
        try:
            row = db.one(
                conn, "SELECT expires_at FROM pairing_codes WHERE code_hash=?", (_sha(code),)
            )
        finally:
            conn.close()
        assert row["expires_at"] == "2026-10-24T12:05:00Z"

        db._FIXED_NOW = "2026-10-24T12:06:00Z"
        direct_session(client, uid)
        r = client.post("/api/pair/approve", json={"code": code})
        assert r.status_code == 410
        assert _err_code(r) == "code_expired"

        r = client.post("/api/pair/finish", json={"code": code})
        assert r.status_code == 410
        assert _err_code(r) == "code_expired"
    finally:
        db._FIXED_NOW = saved


# ------------------------------------------------------------------ approve auth


def test_approve_without_session_is_401_session_invalid(client):
    code = _start(client)["code"]
    r = client.post("/api/pair/approve", json={"code": code})
    assert r.status_code == 401
    assert _err_code(r) == "session_invalid"


# ------------------------------------------------------------------ rotate grace


def test_rotate_grace_accepts_previous_token(client):
    uid = mk_user("ava@example.com", "Ava")
    token0, agent_id = mint_pairing(uid, "laptop")

    r = client.post("/api/pair/rotate", headers=auth_header(token0))
    assert r.status_code == 200, r.text
    body = r.json()
    token1 = body["token"]
    assert token1 != token0
    assert body["agent_id"] == agent_id
    assert body["grace_expires_at"] > db.utcnow()

    row = _pairing_row(agent_id)
    assert row["token_hash"] == db.hash_token(token1)
    assert row["prev_token_hash"] == db.hash_token(token0)

    # previous token still resolves inside the grace window (dependency level)
    assert _ident(token0) == (uid, agent_id)
    assert _ident(token1) == (uid, agent_id)


def test_previous_token_invalid_after_grace(client):
    uid = mk_user("ava@example.com", "Ava")
    token0, agent_id = mint_pairing(uid, "laptop")
    assert client.post("/api/pair/rotate", headers=auth_header(token0)).status_code == 200

    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE pairings SET prev_rotate_expires='2026-01-01T00:00:00Z' WHERE agent_id=?",
                (agent_id,),
            )
    finally:
        conn.close()

    r = client.post("/api/pair/rotate", headers=auth_header(token0))
    assert r.status_code == 401
    assert _err_code(r) == "token_invalid"


# ------------------------------------------------------------------ forget


def test_forget_revokes_and_removes_only_this_agents_availability(client):
    ava = mk_user("ava@example.com", "Ava")
    ben = mk_user("ben@example.com", "Ben")
    token_a, agent_a = mint_pairing(ava, "laptop")
    token_b, agent_b = mint_pairing(ben, "phone")
    windows(ava, [WINDOW], agent_id=agent_a)
    windows(ben, [WINDOW], agent_id=agent_b)
    grant(ava, "matching")

    assert sorted(_availability_agents()) == sorted([agent_a, agent_b])
    assert _window_count(ava) == 1 and _window_count(ben) == 1

    r = client.post("/api/pair/forget", headers=auth_header(token_a))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    assert _availability_agents() == [agent_b], "only the forgotten agent's availability is deleted"
    assert _window_count(ava) == 0, "windows cascade with the availability row"
    assert _window_count(ben) == 1
    assert _pairing_row(agent_a)["revoked_at"] is not None
    assert _pairing_row(agent_b)["revoked_at"] is None

    conn = db.connect()
    try:
        g = db.one(conn, "SELECT withdrawn_at FROM grants WHERE user_id=? AND kind='matching'", (ava,))
    finally:
        conn.close()
    assert g is not None and g["withdrawn_at"] is None, "forget must not touch grants"

    assert _err_code(client.post("/api/pair/forget", headers=auth_header(token_a))) == "token_revoked"


# ------------------------------------------------------------------ 401 bodies


def test_unknown_and_revoked_tokens_have_distinct_401_bodies(client):
    unknown = client.post("/api/pair/forget", headers=auth_header("not-a-real-token"))
    assert unknown.status_code == 401
    assert _err_code(unknown) == "token_invalid"

    uid = mk_user("ava@example.com", "Ava")
    token, _agent = mint_pairing(uid)
    assert client.post("/api/pair/forget", headers=auth_header(token)).status_code == 200
    revoked = client.post("/api/pair/forget", headers=auth_header(token))
    assert revoked.status_code == 401
    assert _err_code(revoked) == "token_revoked"

    assert _err_code(unknown) != _err_code(revoked)
    assert unknown.json() != revoked.json()

    # a revoked pairing's previous (grace) token is also revoked, never "invalid"
    token2, agent2 = mint_pairing(uid)
    assert client.post("/api/pair/rotate", headers=auth_header(token2)).status_code == 200
    client.post("/api/pair/forget", headers=auth_header(token2))
    r = client.post("/api/pair/forget", headers=auth_header(token2))
    assert _err_code(r) == "token_revoked"
    assert _pairing_row(agent2)["revoked_at"] is not None


def test_missing_bearer_header_is_token_invalid(client):
    r = client.post("/api/pair/rotate")
    assert r.status_code == 401
    assert _err_code(r) == "token_invalid"


# ------------------------------------------------------------------ pairings list/delete


def test_list_and_delete_pairings_session(client):
    uid = mk_user("ava@example.com", "Ava")
    _token_a, agent_a = mint_pairing(uid, "laptop")
    _token_b, agent_b = mint_pairing(uid, "phone")
    windows(uid, [WINDOW], agent_id=agent_b)

    direct_session(client, uid)
    r = client.get("/api/pairings")
    assert r.status_code == 200
    listed = r.json()["pairings"]
    assert {p["agent_id"] for p in listed} == {agent_a, agent_b}
    assert set(listed[0]) == {"agent_id", "label", "created_at", "last_seen_at", "revoked_at"}
    assert {p["label"] for p in listed} == {"laptop", "phone"}

    other = mk_user("ben@example.com", "Ben")
    direct_session(client, other)
    r = client.delete(f"/api/pairings/{agent_a}")
    assert r.status_code == 404, "cannot delete another user's pairing"

    direct_session(client, uid)
    r = client.delete(f"/api/pairings/{agent_b}")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert _pairing_row(agent_b)["revoked_at"] is not None
    assert _pairing_row(agent_a)["revoked_at"] is None
    assert _availability_agents() == [], "this agent's availability (and windows) is gone"
    assert _window_count(uid) == 0

    r = client.get("/api/pairings")
    assert r.status_code == 200
    assert {p["agent_id"] for p in r.json()["pairings"]} == {agent_a, agent_b}


def test_pairings_require_session(client):
    assert _err_code(client.get("/api/pairings")) == "session_invalid"
    assert _err_code(client.delete("/api/pairings/ag_nope")) == "session_invalid"
