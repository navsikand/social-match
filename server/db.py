"""SQLite spine: connections, UTC time, hashing, inbox push, immediate transactions."""
import contextlib
import datetime
import hashlib
import json
import os
import sqlite3

from fastapi import Depends, HTTPException

from server import config

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "schema.sql")

_FIXED_NOW = None  # tests may freeze time: db._FIXED_NOW = "2026-10-24T12:00:00Z"


def utcnow(days: float = 0) -> str:
    """UTC timestamp string 'YYYY-MM-DDTHH:MM:SSZ' — lexicographically ordered."""
    if _FIXED_NOW is not None and days == 0:
        return _FIXED_NOW
    if _FIXED_NOW is not None:
        base = datetime.datetime.strptime(_FIXED_NOW, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.UTC
        )
        return (base + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        path or config.db_path(), timeout=30, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Column additions applied to databases that already exist (the deployed one does).
# Literal statements, no interpolation: the names are constants, not input.
_PHOTO_MIGRATIONS = (
    ("photo_path", "ALTER TABLE users ADD COLUMN photo_path TEXT"),
    ("photo_revision", "ALTER TABLE users ADD COLUMN photo_revision INTEGER NOT NULL DEFAULT 0"),
    ("photo_published_at", "ALTER TABLE users ADD COLUMN photo_published_at TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent, serialized column additions.

    Two processes can start at once, so the PRAGMA read and the ALTER run under a single
    BEGIN IMMEDIATE: one wins the write lock, the other sees the column already there.
    """
    with immediate(conn):
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, statement in _PHOTO_MIGRATIONS:
            if name not in existing:
                conn.execute(statement)


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        _migrate(conn)
    finally:
        conn.close()


@contextlib.contextmanager
def immediate(conn: sqlite3.Connection):
    """Serialized write transaction. SQLite's write lock is the serialization point."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def q(conn, sql: str, args: tuple = ()):
    return conn.execute(sql, args).fetchall()


def one(conn, sql: str, args: tuple = ()):
    return conn.execute(sql, args).fetchone()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_hash(user_id: int) -> str:
    return hashlib.sha256(f"{config.block_salt()}:{user_id}".encode()).hexdigest()


def push_inbox(conn, user_id: int, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO inbox_items (user_id, kind, payload_json, created_at) VALUES (?,?,?,?)",
        (user_id, kind, json.dumps(payload), utcnow()),
    )


def get_conn():
    """FastAPI dependency: one fresh connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


conn_dep = Depends(get_conn)


def http_error(status: int, code: str, detail: str = ""):
    return HTTPException(status_code=status, detail={"error": code, "detail": detail})
