"""Grants-CHECK rebuild + chat-table schema (lane I).

A SQLite CHECK cannot be ALTERed, so `grants` gains kind='delegation' by table rebuild. These
tests exercise the rebuild hard: fresh DB, hand-built pre-existing DB, repeated init, and two
racing initializers. Every old row is dumped as a tuple before and after and must be identical.

Frozen clock, zero sleeps.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from server import db

FIXED_NOW = "2026-10-24T12:00:00Z"

# The grants table as deployed BEFORE the delegation grant. Copied verbatim from the old
# schema.sql on purpose: if the migration's idea of "old" drifts, this fixture goes red.
OLD_GRANTS_DDL = """
CREATE TABLE grants (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('matching','publication','reveal')),
  tier TEXT CHECK (kind != 'publication' OR tier IN ('card','card+name')),
  version INTEGER NOT NULL,
  fields_json TEXT,
  audience TEXT NOT NULL DEFAULT 'matched_users_only',
  granted_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  withdrawn_at TEXT
)
"""

OLD_GRANTS_COLUMNS = (
    "id",
    "user_id",
    "kind",
    "tier",
    "version",
    "fields_json",
    "audience",
    "granted_at",
    "expires_at",
    "withdrawn_at",
)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    yield
    db._FIXED_NOW = None


def _dump(conn) -> list[tuple]:
    """Every column of every grants row, order-stable. The whole point of the test."""
    rows = conn.execute(
        "SELECT id, user_id, kind, tier, version, fields_json, audience, granted_at,"
        " expires_at, withdrawn_at FROM grants ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in rows]


def _insert(conn, uid, kind, tier, version, granted_at, expires_at, withdrawn_at=None,
            fields_json: str | None = '{"audience": "matched_users_only"}',
            audience="matched_users_only"):
    conn.execute(
        "INSERT INTO grants (user_id, kind, tier, version, fields_json, audience, granted_at,"
        " expires_at, withdrawn_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, kind, tier, version, fields_json, audience, granted_at, expires_at, withdrawn_at),
    )


def _seed_old_rows(conn) -> None:
    """Every kind, plus a withdrawn and an expired variant, plus NULL/edge values."""
    with db.immediate(conn):
        _insert(conn, 1, "matching", None, 1, "2026-10-01T00:00:00Z", "2026-10-31T00:00:00Z")
        _insert(conn, 2, "publication", "card", 2, "2026-10-02T00:00:00Z", "2026-11-01T00:00:00Z")
        _insert(conn, 3, "publication", "card+name", 7, "2026-10-03T00:00:00Z",
                "2026-11-02T00:00:00Z", fields_json='{"audience": "matched_users_only"}')
        _insert(conn, 4, "reveal", None, 1, "2026-10-04T00:00:00Z", "2026-11-03T00:00:00Z")
        # withdrawn: consent revoked, row must survive untouched
        _insert(conn, 5, "matching", None, 3, "2026-10-05T00:00:00Z", "2026-11-04T00:00:00Z",
                withdrawn_at="2026-10-06T00:00:00Z")
        # expired: past expires_at, row must survive untouched
        _insert(conn, 6, "matching", None, 2, "2026-09-01T00:00:00Z", "2026-09-08T00:00:00Z")
        # NULL fields_json / non-default audience: the copy must be column-exact, not coerced
        _insert(conn, 7, "publication", "card", 1, "2026-10-07T00:00:00Z",
                "2026-11-06T00:00:00Z", fields_json=None, audience="public")


def _make_old_db(path: str) -> None:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(OLD_GRANTS_DDL)
        _seed_old_rows(conn)
    finally:
        conn.close()


def _grants_sql(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='grants'"
        ).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------- fresh DB


def test_fresh_db_accepts_delegation_grant(db_env):
    db.init_db()
    conn = db.connect(db_env)
    try:
        assert "delegation" in _grants_sql(db_env), "schema.sql must ship the extended CHECK"
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO grants (user_id, kind, tier, version, fields_json, granted_at,"
                " expires_at) VALUES (?,?,?,?,?,?,?)",
                (1, "delegation", None, 1, None, db.utcnow(), db.utcnow(days=30)),
            )
        assert db.one(conn, "SELECT COUNT(*) AS n FROM grants WHERE kind='delegation'")["n"] == 1
        # The tier CHECK is still enforced for publication rows after the rebuild.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO grants (user_id, kind, tier, version, granted_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (1, "publication", "bogus", 1, db.utcnow(), db.utcnow(days=30)),
            )
        # And the old kinds still reject nonsense.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO grants (user_id, kind, version, granted_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (1, "telepathy", 1, db.utcnow(), db.utcnow(days=30)),
            )
    finally:
        conn.close()


# ------------------------------------------- pre-existing (deployed) DB


def test_rebuild_preserves_every_row_and_extends_the_check(db_env):
    _make_old_db(db_env)
    assert "delegation" not in _grants_sql(db_env)

    conn = db.connect(db_env)
    try:
        before = _dump(conn)
    finally:
        conn.close()
    assert len(before) == 7

    db.init_db()  # runs schema.sql (no-op on grants) then the rebuild

    assert "delegation" in _grants_sql(db_env), "the CHECK must now admit delegation"
    conn = db.connect(db_env)
    try:
        after = _dump(conn)
        # column order and identity of the rebuilt table, exactly as before
        cols = tuple(
            r["name"] for r in conn.execute("PRAGMA table_info(grants)").fetchall()
        )
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO grants (user_id, kind, tier, version, fields_json, granted_at,"
                " expires_at) VALUES (?,?,?,?,?,?,?)",
                (99, "delegation", None, 1, None, db.utcnow(), db.utcnow(days=30)),
            )
        delegation_id = db.one(
            conn, "SELECT id FROM grants WHERE user_id=99 AND kind='delegation'"
        )["id"]
    finally:
        conn.close()

    assert cols == OLD_GRANTS_COLUMNS, f"column order changed: {cols}"
    assert after == before, (
        "every column of every pre-existing grants row must survive the rebuild byte-identical"
    )
    # Existing ids are preserved, so the new row lands after them.
    assert delegation_id > max(r[0] for r in before)


def test_rebuild_is_a_noop_the_second_time(db_env):
    _make_old_db(db_env)
    conn = db.connect(db_env)
    try:
        before = _dump(conn)
    finally:
        conn.close()

    db.init_db()
    db.init_db()
    db.init_db()

    assert _grants_sql(db_env).count("CREATE TABLE") == 1
    conn = db.connect(db_env)
    try:
        assert _dump(conn) == before
        assert db.one(conn, "SELECT COUNT(*) AS n FROM grants")["n"] == len(before)
    finally:
        conn.close()


def test_racing_initializers_preserve_rows(db_env):
    """Two startup paths, separate connections, one write lock: both finish, rows survive."""
    _make_old_db(db_env)
    conn = db.connect(db_env)
    try:
        before = _dump(conn)
    finally:
        conn.close()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def boot() -> None:
        try:
            barrier.wait(timeout=10)
            db.init_db()
        except BaseException as exc:  # noqa: BLE001 - we want the failure text
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"racing initializers must not collide: {errors}"
    assert all(not t.is_alive() for t in threads)
    conn = db.connect(db_env)
    try:
        assert _dump(conn) == before
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "grants_new" not in tables, "a half-finished rebuild must not leave a shadow table"
    assert "delegation" in _grants_sql(db_env)


# --------------------------------------------------- the chat tables


def test_chat_tables_exist_with_their_constraints(db_env):
    db.init_db()
    conn = db.connect(db_env)
    try:
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }
        assert {"chat_messages", "chat_turns", "chat_pending"} <= tables
        assert {"ix_chat_messages_user", "ix_chat_pending_user"} <= indexes

        now = db.utcnow()
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO chat_messages (user_id, role, content, created_at)"
                " VALUES (?,?,?,?)",
                (1, "user", "hi", now),
            )
            conn.execute(
                "INSERT INTO chat_turns (user_id, kind, ref, at) VALUES (?,?,?,?)",
                (1, "negotiation", "offer:12", now),
            )
            conn.execute(
                "INSERT INTO chat_pending (user_id, kind, payload_json, state_hash,"
                " created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (1, "windows", "{}", "abc", now, db.utcnow()),
            )
        # role is a closed set — provider prose never invents a role
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chat_messages (user_id, role, content, created_at)"
                " VALUES (?,?,?,?)",
                (1, "system", "not a transcript role", now),
            )
        # NOT NULL discipline the chat lane relies on
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO chat_pending (user_id, kind, payload_json, state_hash,"
                " created_at) VALUES (?,?,?,?,?)",
                (1, "events", "{}", "abc", now),
            )
    finally:
        conn.close()
