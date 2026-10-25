"""Pairing endpoints (CONTRACT §4). Owner: P1a.

Flow: `start` (anonymous) → `approve` (session) → `finish` (anonymous) → bearer token.
Codes are 8 Crockford chars displayed `XXXX-XXXX`, stored only as sha256 of the dash-stripped
uppercase string. `finish` is a single atomic UPDATE (… RETURNING) — never read-then-write.
"""
import datetime
import secrets
import sqlite3

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel

from server import auth, config, db, sessions

router = APIRouter(prefix="/api/pair", tags=["pairing"])
pairings_router = APIRouter(prefix="/api", tags=["pairing"])

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
CODE_LEN = 8

_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


# ---------------------------------------------------------------- helpers


def normalize_code(raw: str) -> str:
    """Uppercase, drop dashes/whitespace: 'ab12-cd34' → 'AB12CD34'."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def code_hash(raw: str) -> str:
    return db.hash_token(normalize_code(raw))


def display_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def plus_seconds(iso: str, seconds: int) -> str:
    """db.utcnow() supports days only; rotate grace is seconds-grained."""
    base = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    return (base + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _code_failure(conn, ch_hash: str, now: str):
    """Diagnose why the atomic UPDATE matched nothing → 404 or 410 (used beats expired)."""
    row = db.one(conn, "SELECT used_at, expires_at FROM pairing_codes WHERE code_hash=?", (ch_hash,))
    if row is None:
        return db.http_error(404, "code_invalid", "unknown pairing code")
    if row["used_at"] is not None:
        return db.http_error(410, "code_used", "that code was already used")
    if row["expires_at"] <= now:
        return db.http_error(410, "code_expired", "that code expired — start a new pairing")
    # Row exists, unused, unexpired → it has not been approved yet.
    return db.http_error(404, "code_invalid", "that code was not approved yet")


def _revoke_and_forget(conn, agent_id: str) -> None:
    """Shared by /pair/forget and DELETE /api/pairings/{agent_id}: revoke this pairing and
    delete availability this agent wrote (windows cascade). Other pairings/grants untouched."""
    now = db.utcnow()
    conn.execute(
        "UPDATE pairings SET revoked_at=? WHERE agent_id=? AND revoked_at IS NULL", (now, agent_id)
    )
    conn.execute("DELETE FROM availability WHERE written_by_agent_id=?", (agent_id,))


# ---------------------------------------------------------------- models


class StartIn(BaseModel):
    label: str | None = None


class CodeIn(BaseModel):
    code: str


# ---------------------------------------------------------------- routes


@router.post("/start", status_code=201)
def pair_start(payload: StartIn | None = Body(default=None), conn=Depends(db.get_conn)):
    _ = (payload.label if payload else None)  # label is advisory; agents name themselves on rotate-less flow
    ttl = config.code_ttl_seconds()
    code = new_code()
    with db.immediate(conn):
        conn.execute(
            "INSERT INTO pairing_codes (code_hash, user_id, kind, expires_at, used_at)"
            " VALUES (?, NULL, 'link', ?, NULL)",
            (code_hash(code), plus_seconds(db.utcnow(), ttl)),
        )
    shown = display_code(code)
    return {
        "code": shown,
        "url": f"{config.base_url()}/m/pair/{shown}",
        "expires_in": ttl,
    }


@router.post("/approve")
def pair_approve(
    payload: CodeIn,
    conn=Depends(db.get_conn),
    user=Depends(sessions.require_session),
):
    ch = code_hash(payload.code)
    with db.immediate(conn):
        now = db.utcnow()
        cur = conn.execute(
            "UPDATE pairing_codes SET user_id=?"
            " WHERE code_hash=? AND used_at IS NULL AND expires_at > ?",
            (user["id"], ch, now),
        )
        if cur.rowcount != 1:
            raise _code_failure(conn, ch, now)
    return {"ok": True}


@router.post("/finish")
def pair_finish(payload: CodeIn, conn=Depends(db.get_conn)):
    ch = code_hash(payload.code)
    token = secrets.token_urlsafe(24)
    agent_id = "ag_" + secrets.token_urlsafe(6)
    with db.immediate(conn):
        now = db.utcnow()
        user_id = None
        if _HAS_RETURNING:
            row = conn.execute(
                "UPDATE pairing_codes SET used_at=?"
                " WHERE code_hash=? AND used_at IS NULL AND expires_at > ? AND user_id IS NOT NULL"
                " RETURNING user_id",
                (now, ch, now),
            ).fetchone()
            if row is not None:
                user_id = row["user_id"]
        else:
            cur = conn.execute(
                "UPDATE pairing_codes SET used_at=?"
                " WHERE code_hash=? AND used_at IS NULL AND expires_at > ? AND user_id IS NOT NULL",
                (now, ch, now),
            )
            if cur.rowcount == 1:
                user_id = db.one(
                    conn, "SELECT user_id FROM pairing_codes WHERE code_hash=?", (ch,)
                )["user_id"]
        if user_id is None:
            raise _code_failure(conn, ch, now)
        conn.execute(
            "INSERT INTO pairings (user_id, agent_id, token_hash, prev_token_hash,"
            " prev_rotate_expires, label, created_at, last_seen_at)"
            " VALUES (?,?,?,NULL,NULL,?,?,?)",
            (user_id, agent_id, db.hash_token(token), "paired agent", now, now),
        )
    return {"token": token, "agent_id": agent_id}


@router.post("/rotate")
def pair_rotate(conn=Depends(db.get_conn), ident: auth.Identity = auth.identity_dep):
    new_token = secrets.token_urlsafe(24)
    with db.immediate(conn):
        now = db.utcnow()
        grace_expires = plus_seconds(now, config.rotate_grace_seconds())
        row = db.one(
            conn,
            "SELECT id, token_hash FROM pairings WHERE agent_id=? AND user_id=?",
            (ident.agent_id, ident.user_id),
        )
        if row is None:  # pragma: no cover - identity implies the row exists
            raise db.http_error(401, "token_invalid", "pairing vanished")
        conn.execute(
            "UPDATE pairings SET token_hash=?, prev_token_hash=?, prev_rotate_expires=?,"
            " last_seen_at=? WHERE id=?",
            (db.hash_token(new_token), row["token_hash"], grace_expires, now, row["id"]),
        )
    return {"token": new_token, "agent_id": ident.agent_id, "grace_expires_at": grace_expires}


@router.post("/forget")
def pair_forget(conn=Depends(db.get_conn), ident: auth.Identity = auth.identity_dep):
    with db.immediate(conn):
        _revoke_and_forget(conn, ident.agent_id)
    return {"ok": True}


@pairings_router.get("/pairings")
def list_pairings(conn=Depends(db.get_conn), user=Depends(sessions.require_session)):
    rows = db.q(
        conn,
        "SELECT agent_id, label, created_at, last_seen_at, revoked_at FROM pairings"
        " WHERE user_id=? ORDER BY id",
        (user["id"],),
    )
    return {"pairings": [dict(r) for r in rows]}


@pairings_router.delete("/pairings/{agent_id}")
def delete_pairing(
    agent_id: str, conn=Depends(db.get_conn), user=Depends(sessions.require_session)
):
    with db.immediate(conn):
        row = db.one(
            conn,
            "SELECT agent_id FROM pairings WHERE agent_id=? AND user_id=?",
            (agent_id, user["id"]),
        )
        if row is None:
            raise db.http_error(404, "not_found", "no such pairing for this user")
        _revoke_and_forget(conn, agent_id)
    return {"ok": True}


# `/api/pairings` shares no prefix with `/api/pair/*`, and the frozen `app.py` only includes
# `pairing.router` — so graft the pre-built absolute-path routes onto it directly (nesting
# would yield `/api/pair/api/pairings`).
router.routes.extend(pairings_router.routes)
