import tests.util as util

from server import db, wording


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_error_bodies_are_flat_contract_shape(client):
    """CONTRACT §2: {"error": ..., "detail": ...} at top level, everywhere — verified on a real 401."""
    r = client.get("/api/inbox")
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "token_invalid"
    assert "detail" in body


def test_blocked_pair_expired_block_does_not_suppress(client):
    """Regression for the precedence bug P2c found: an EXPIRED block must not block matching.
    Old SQL was (A AND B) OR (C AND D AND expires>) - expiry applied to one direction only."""
    a = util.mk_user("a@x", "Ava")
    b = util.mk_user("b@x", "Ben")
    conn = db.connect()
    try:
        now = db.utcnow()
        ah, bh = db.user_hash(a), db.user_hash(b)
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (ah, bh, db.utcnow(days=-1), db.utcnow(days=-1)),
            )
        assert wording.blocked_pair(conn, a, b) is False
        assert wording.blocked_pair(conn, b, a) is False
        with db.immediate(conn):
            conn.execute("DELETE FROM blocks")
            conn.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (ah, bh, now, db.utcnow(days=30)),
            )
        assert wording.blocked_pair(conn, a, b) is True  # reverse direction still blocked
        assert wording.blocked_pair(conn, b, a) is True
    finally:
        conn.close()
