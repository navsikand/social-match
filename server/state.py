"""POST /api/state. P1b owns this file (CONTRACT §5).

One endpoint: accept the canonical allowlisted payload, or reject it with 422
`payload_forbidden`. The write path is a single `db.immediate` transaction so availability,
windows, tags, grants, consent rows and matching generation are all-or-nothing.

The write service itself lives in `server.statewrite` (shared with the chat lanes); this module
keeps only the HTTP shape: parse, validate, delegate, wrap the 201.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from server import db, statewrite
from server.auth import get_identity
from server.schema import StatePayload
from server.statewrite import canonical_hash  # re-exported for legacy callers

__all__ = ["canonical_hash", "post_state", "router"]

router = APIRouter(prefix="/api", tags=["state"])


def _forbidden(detail: str) -> JSONResponse:
    # Contract §2 wants the flat error shape, so we answer with a JSONResponse rather than
    # the nested detail wrapping of db.http_error.
    return JSONResponse(status_code=422, content={"error": "payload_forbidden", "detail": detail})


def _detail(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<body>"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid payload"


@router.post("/state", status_code=201)
async def post_state(
    request: Request,
    identity=Depends(get_identity),
    conn=Depends(db.get_conn),
):
    try:
        raw = await request.json()
    except Exception:
        return _forbidden("request body must be a JSON object")

    try:
        payload = StatePayload.model_validate(raw)
    except ValidationError as exc:
        return _forbidden(_detail(exc))

    with db.immediate(conn):
        out = statewrite.write_state(
            conn, identity.user_id, payload, mode="agent", agent_id=identity.agent_id
        )

    return JSONResponse(status_code=201, content=out)
