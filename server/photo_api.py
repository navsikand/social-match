"""Photo storage, publication and serving (CONTRACT-photos.md §2).

Three surfaces, one rule: *the DB authorizes, the file merely stores*. Revocation happens by
writing `photo_published_at = NULL` (and by bumping the revision), so a failed unlink can never
leave a servable photo behind.

`card_photo_url` is the read seam `scheduling.card()` calls; it is side-effect free and returns
None for every refusal. `GET /media/photo/{id}` answers 404 for every refusal as well, so the
route cannot be used to probe whether somebody has a photo.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from server import config, db, matching, photos, sessions, wording

router = APIRouter(tags=["photos"])

_CANDIDATE_STATES = ("open", "matched")  # everything else is not a live card
_RATE_LIMIT = 5  # uploads ...
_RATE_WINDOW_MINUTES = 10  # ... per this many minutes, per account
_MEDIA_TYPES = {"jpg": "image/jpeg", "png": "image/png"}
_FILE_EXTS = ("jpg", "png")
_PHOTO_SCOPE = "photo"


class _AccountGone(Exception):
    """Raised inside the publish transaction to roll back when the account vanished."""


class _RateLimited(Exception):
    """Raised inside the publish transaction when the authoritative count says stop.

    The pre-stream check is only a fast path: N concurrent requests can all read "4 recent
    uploads" before any of them writes, so the count that decides is taken under the write
    lock (gate review P1)."""


# ------------------------------------------------------------------ filesystem helpers


def _upload_dir() -> str:
    return config.upload_dir()


def _ensure_upload_dir() -> str:
    directory = _upload_dir()
    os.makedirs(directory, mode=0o750, exist_ok=True)
    return directory


def _stored_name(photo_path: str) -> str:
    """Defensive: only ever a bare filename, never a path that could escape UPLOAD_DIR."""
    name = os.path.basename(photo_path or "")
    return name if name == photo_path else ""


def _photo_file(photo_path: str) -> str | None:
    """Absolute path of the stored file when it exists and has a servable extension."""
    name = _stored_name(photo_path)
    if not name:
        return None
    if name.rsplit(".", 1)[-1].lower() not in _FILE_EXTS:
        return None
    path = os.path.join(_upload_dir(), name)
    return path if os.path.isfile(path) else None


def _write_atomic(name: str, data: bytes) -> str:
    """Temp file + os.replace, mode 0640. Same directory, so replace is atomic."""
    directory = _ensure_upload_dir()
    final = os.path.join(directory, name)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return final


# Request-local file cleanup is deliberately gone (gate review P1): a request only knows its
# OWN filename, so with two overlapping uploads the older request deleted the newer file, and
# a withdrawal could delete a photo published moments later. Deleting bytes is now the
# sweeper's job — see sweep.purge_orphan_photos: unreferenced AND past a grace window and
# reference-checked against the DB, in the same write transaction as publication.


# ------------------------------------------------------------------ authorization reads


def _published_row(conn, target_uid: int):
    """Common gate (applies to everyone, self included): live account + published + path."""
    row = db.one(conn, "SELECT * FROM users WHERE id=?", (target_uid,))
    if row is None or row["deleted_at"] is not None:
        return None
    if not row["photo_published_at"] or not row["photo_path"]:
        return None
    return row


def _availability_live(conn, uid: int, now: str):
    # Local copy of scheduling.availability_live: scheduling imports this module, so importing
    # it back would be a cycle. Same one-line read, same semantics.
    return db.one(conn, "SELECT * FROM availability WHERE user_id=? AND expires_at > ?", (uid, now))


def _viewer_may_see(conn, viewer_uid: int, about_uid: int) -> bool:
    """The non-self gate: exactly `card()`'s authorization, re-read now."""
    now = db.utcnow()
    candidates = db.q(
        conn,
        "SELECT * FROM candidates WHERE ((user_a=? AND user_b=?) OR (user_a=? AND user_b=?))"
        " AND state IN (?,?) AND expires_at > ? ORDER BY id DESC",
        (viewer_uid, about_uid, about_uid, viewer_uid, *_CANDIDATE_STATES, now),
    )
    for cand in candidates:
        prop = db.one(
            conn,
            "SELECT id FROM proposals WHERE candidate_id=? AND state != 'voided'"
            " ORDER BY version DESC LIMIT 1",
            (cand["id"],),
        )
        if prop is None:
            continue
        if not wording.matching_live(conn, cand["user_a"], cand["user_b"]):
            continue
        if wording.live_grant(conn, about_uid, "publication", "card") is None:
            continue
        if (
            _availability_live(conn, cand["user_a"], now) is None
            or _availability_live(conn, cand["user_b"], now) is None
        ):
            continue
        if wording.blocked_pair(conn, cand["user_a"], cand["user_b"]):
            continue
        return True
    return False


def card_photo_url(conn, viewer_uid: int, about_uid: int) -> str | None:
    """'/media/photo/{about_uid}' when the non-self gate passes, else None. Side-effect free."""
    if viewer_uid == about_uid:
        return None
    row = _published_row(conn, about_uid)
    if row is None or _photo_file(row["photo_path"]) is None:
        return None
    if not _viewer_may_see(conn, viewer_uid, about_uid):
        return None
    return f"/media/photo/{about_uid}"


# ------------------------------------------------------------------ POST /api/me/photo


def _error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


@router.post("/api/me/photo")
async def publish_photo(
    request: Request,
    user=Depends(sessions.require_session),
    conn=Depends(db.get_conn),
) -> JSONResponse:
    uid = user["id"]

    # (a) Early reject. Not authoritative — the account may be deleted mid-stream (step e).
    if user["deleted_at"] is not None:
        return _error(401, "session_invalid", "account missing or deleted")

    # (b) Streamed cap. Content-Length is never trusted (missing or lying under chunked).
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > photos.MAX_UPLOAD_BYTES:
            return _error(
                413, "too_large", f"upload exceeds {photos.MAX_UPLOAD_BYTES} bytes"
            )

    # (c) Per-account rate limit, counted in consent_log (no extra table).
    since = db.utcnow(days=-_RATE_WINDOW_MINUTES / 1440)
    recent = db.one(
        conn,
        "SELECT COUNT(*) AS n FROM consent_log WHERE user_id_hash=? AND action='photo:published'"
        " AND created_at > ?",
        (db.user_hash(uid), since),
    )
    if recent is not None and recent["n"] >= _RATE_LIMIT:
        return _error(429, "rate_limited", f"at most {_RATE_LIMIT} uploads per {_RATE_WINDOW_MINUTES} min")

    # (d) Decode + re-encode off the event loop.
    try:
        data, ext = await run_in_threadpool(photos.normalise, bytes(body))
    except photos.PhotoRejected as exc:
        status = 413 if exc.code in ("too_large", "too_big") else 415
        return _error(status, exc.code, exc.detail)

    # (e) Authoritative check + publish, one serialized transaction.
    now = db.utcnow()
    try:
        with db.immediate(conn):
            fresh = db.one(conn, "SELECT * FROM users WHERE id=?", (uid,))
            if fresh is None or fresh["deleted_at"] is not None:
                raise _AccountGone
            # Gate review P1: the pre-stream check is only a fast path. Concurrent uploads
            # could all observe "4 recent publications" and all publish, so the AUTHORITATIVE
            # count happens here, inside the same write transaction as the publication.
            recent = db.one(
                conn,
                "SELECT COUNT(*) AS n FROM consent_log WHERE user_id_hash=?"
                " AND action='photo:published' AND created_at > ?",
                (db.user_hash(uid), db.utcnow(days=-_RATE_WINDOW_MINUTES / 1440)),
            )
            if recent is not None and recent["n"] >= _RATE_LIMIT:
                raise _RateLimited
            rev = (fresh["photo_revision"] or 0) + 1
            name = f"{uid}-{rev}.{ext}"
            _write_atomic(name, data)
            conn.execute(
                "UPDATE users SET photo_path=?, photo_revision=?, photo_published_at=? WHERE id=?",
                (name, rev, now, uid),
            )
            conn.execute(
                "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (db.user_hash(uid), "photo:published", _PHOTO_SCOPE,
                 json.dumps({"revision": rev}), now),
            )
    except _RateLimited:
        return _error(429, "rate_limited", f"at most {_RATE_LIMIT} uploads per {_RATE_WINDOW_MINUTES} min")
    except _AccountGone:
        return _error(401, "session_invalid", "account missing or deleted")

    # Bytes of the previous revision are reclaimed by sweep.purge_orphan_photos after the
    # grace window; this request only ever knew its own filename, which is why per-request
    # cleanup was a bug (it could delete a newer upload).

    # A photo is mandatory before anyone is matchable, so publishing it may be the moment this
    # student first qualifies. Regenerate for every available user, in its own short transaction.
    with contextlib.suppress(Exception):
        with db.immediate(conn):
            matching.refresh_all(conn)

    return JSONResponse(
        status_code=200,
        content={"ok": True, "photo_url": f"/media/photo/{uid}", "revision": rev},
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------------ DELETE /api/me/photo


@router.delete("/api/me/photo")
def withdraw_photo(
    user=Depends(sessions.require_session),
    conn=Depends(db.get_conn),
) -> JSONResponse:
    uid = user["id"]
    now = db.utcnow()
    # Revocation lives in the DB: commit it before touching the filesystem.
    with db.immediate(conn):
        # Both fields in one statement: photo_published_at withdraws the AUTHORIZATION,
        # photo_path drops the REFERENCE so the sweeper may reclaim the bytes after grace.
        # Keeping photo_path (the first cut) meant a withdrawn photo was "referenced" forever
        # and never reclaimed — a retention bug the gate review caught.
        conn.execute(
            "UPDATE users SET photo_published_at=NULL, photo_path=NULL WHERE id=?", (uid,)
        )
        conn.execute(
            "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (db.user_hash(uid), "photo:withdrawn", _PHOTO_SCOPE, "{}", now),
        )
    # No unlink here: the sweeper reclaims unreferenced bytes after the grace window.
    return JSONResponse(status_code=200, content={"ok": True}, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ GET /media/photo/{id}


@router.get("/media/photo/{user_id}")
def serve_photo(
    user_id: int,
    user=Depends(sessions.require_session),
    conn=Depends(db.get_conn),
):
    viewer_uid = user["id"]

    row = _published_row(conn, user_id)
    path = _photo_file(row["photo_path"]) if row is not None else None
    if path is None:
        raise db.http_error(404, "not_found", "")

    if viewer_uid != user_id and not _viewer_may_see(conn, viewer_uid, user_id):
        raise db.http_error(404, "not_found", "")

    media_type = _MEDIA_TYPES.get(path.rsplit(".", 1)[-1].lower())
    if media_type is None:
        raise db.http_error(404, "not_found", "")

    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError:
        raise db.http_error(404, "not_found", "") from None

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
