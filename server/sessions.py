"""Signed session cookies: 'smsession' = URLSafeSerializer({'uid': id, 't': issued-at})."""
from fastapi import Depends, Request  # pyright: ignore[reportMissingImports]
from itsdangerous import BadSignature, URLSafeSerializer  # pyright: ignore[reportMissingImports]
from server import config, db


def sign_session(user_id: int) -> str:
    s = URLSafeSerializer(config.session_secret(), salt="smsess")
    return s.dumps({"uid": user_id, "t": db.utcnow()})


def _read_token(token: str | None) -> int | None:
    if not token:
        return None
    s = URLSafeSerializer(config.session_secret(), salt="smsess")
    try:
        data = s.loads(token)
        uid = data.get("uid")
        return int(uid) if isinstance(uid, int) else None
    except (BadSignature, Exception):
        return None


def session_user_id(request: Request) -> int | None:
    return _read_token(request.cookies.get("smsession"))


def require_session(request: Request, conn=Depends(db.get_conn)):
    """Dependency: live, non-deleted user row, else 401 session_invalid."""
    uid = session_user_id(request)
    if uid is None:
        raise db.http_error(401, "session_invalid", "sign in first")
    row = db.one(conn, "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (uid,))
    if row is None:
        raise db.http_error(401, "session_invalid", "account missing or deleted")
    return row


session_dep = Depends(require_session)
