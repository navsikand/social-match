"""GET /api/inbox + since-cursor. P2c owns this file (CONTRACT §9).

The inbox is an append-only LOG of events, not a live view of the world. Items are never edited,
retracted or filtered by expiry: if a candidate was pushed and later voided by the sweep (§10), the
original item stays in the log verbatim — a reader that wants current state calls /api/card (or
dashboard), not the inbox. Consequence: a client must not treat an inbox item as proof that the
thing it describes is still true.

Cursor semantics: `since` is the max `seq` the client has already seen. `seq` is globally
monotonic (INTEGER PRIMARY KEY AUTOINCREMENT), per-user filtering by `seq > since` is therefore
monotonic and gap-free for each user. Every read also bumps the calling pairing's `last_seen_at`.
"""
import json

from fastapi import APIRouter, Depends, Query

from server import db
from server.auth import Identity, get_identity

router = APIRouter(prefix="/api", tags=["inbox"])


@router.get("/inbox")
def get_inbox(
    since: int = Query(0, ge=0),
    identity: Identity = Depends(get_identity),
    conn=Depends(db.get_conn),
):
    now = db.utcnow()
    with db.immediate(conn):
        conn.execute(
            "UPDATE pairings SET last_seen_at=? WHERE agent_id=? AND user_id=?",
            (now, identity.agent_id, identity.user_id),
        )
        rows = db.q(
            conn,
            "SELECT seq, kind, payload_json, created_at FROM inbox_items"
            " WHERE user_id=? AND seq>? ORDER BY seq ASC",
            (identity.user_id, since),
        )
    return {
        "items": [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ],
        "now": now,
    }
