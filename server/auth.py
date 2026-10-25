"""Bearer auth (CONTRACT §4).

`get_identity` is the one exported FastAPI dependency every bearer endpoint uses. It resolves
`Authorization: Bearer <token>` (sha256-stored in `pairings.token_hash`) into an `Identity`.

Rules:
- exact current token hash wins;
- the *previous* hash is accepted only while `prev_rotate_expires > now` (rotate grace);
- unknown token → 401 `token_invalid`;
- matched pairing with `revoked_at` set → 401 `token_revoked`.

Revocation is checked AFTER a row match so an unlinked agent gets the distinct `token_revoked`
body instead of looking like a garbage token.
"""
from collections import namedtuple

from fastapi import Depends, Request

from server import db

Identity = namedtuple("Identity", ["user_id", "agent_id"])


def parse_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def _lookup(conn, token_hash: str, now: str):
    """Current token first, then rotate grace (previous hash, unexpired)."""
    row = db.one(
        conn,
        "SELECT id, user_id, agent_id, revoked_at FROM pairings WHERE token_hash=?",
        (token_hash,),
    )
    if row is not None:
        return row
    return db.one(
        conn,
        "SELECT id, user_id, agent_id, revoked_at FROM pairings"
        " WHERE prev_token_hash=? AND prev_rotate_expires IS NOT NULL AND prev_rotate_expires > ?",
        (token_hash, now),
    )


def get_identity(request: Request, conn=Depends(db.get_conn)) -> Identity:
    """FastAPI dependency: bearer token → Identity(user_id, agent_id) or 401."""
    token = parse_bearer(request)
    if not token:
        raise db.http_error(401, "token_invalid", "missing bearer token")
    row = _lookup(conn, db.hash_token(token), db.utcnow())
    if row is None:
        raise db.http_error(401, "token_invalid", "unknown token")
    if row["revoked_at"] is not None:
        raise db.http_error(401, "token_revoked", "this agent was unlinked")
    return Identity(user_id=row["user_id"], agent_id=row["agent_id"])


identity_dep = Depends(get_identity)
