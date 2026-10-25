"""Endpoint tests for server/photo_api.py (CONTRACT-photos.md §2, PLAN §B10).

Two things are under test here and nothing else: that publication can be *revoked* (the DB is the
switch, the file is only storage) and that the serving route refuses — always with 404 — the
moment any live-card condition lapses.

Time is injected via `db._FIXED_NOW` rather than slept through, and `UPLOAD_DIR` is pointed at
`tmp_path` *before* the app is imported, so nothing touches the repo's real uploads directory.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]
from PIL import Image

from server import db
from tests import util

FIXED_NOW = "2026-10-24T12:00:00Z"
WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")
TZ = "America/Los_Angeles"
MB = 1024 * 1024


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Everything in this module runs on one injected clock: no sleeps, no flakes."""
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    return FIXED_NOW


@pytest.fixture(autouse=True)
def clean_state(frozen_clock):
    yield
    db._FIXED_NOW = None


@pytest.fixture()
def uploads(db_env, monkeypatch, tmp_path):
    """UPLOAD_DIR before the app is created: config reads env at call time, but be explicit."""
    directory = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(directory))
    db.init_db()
    return directory


@pytest.fixture()
def client(uploads):
    from server.app import app

    with TestClient(app) as c:
        yield c


# --------------------------------------------------------------------------- helpers


def _png(color="red", size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(color="blue", size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _sign(client, uid: int):
    return util.direct_session(client, uid)


def _row(uid: int):
    c = db.connect()
    try:
        return db.one(c, "SELECT * FROM users WHERE id=?", (uid,))
    finally:
        c.close()


def _publish(client, uid: int, data: bytes | None = None, content_type="image/png"):
    _sign(client, uid)
    return client.post(
        "/api/me/photo",
        content=_png() if data is None else data,
        headers={"Content-Type": content_type},
    )


def _files(uploads: Path) -> list[str]:
    return sorted(os.listdir(uploads)) if uploads.exists() else []


def _pair(with_photo_for: int = 2) -> tuple[int, int, int, int]:
    """Two users with a live open candidate; returns (uid_a, uid_b, candidate_id, proposal_id)."""
    a = util.mk_user("a@example.com", "Ava")
    b = util.mk_user("b@example.com", "Ben")
    cid, pid = util.mk_candidate(a, b, window=WINDOW, tz=TZ)
    return a, b, cid, pid


# --------------------------------------------------------------------------- publish


def test_publish_png_sets_columns_and_writes_file(client, uploads):
    uid = util.mk_user("solo@example.com", "Solo")

    r = _publish(client, uid)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["photo_url"] == f"/media/photo/{uid}"
    assert body["revision"] == 1

    row = _row(uid)
    assert row["photo_revision"] == 1
    assert row["photo_path"] == f"{uid}-1.png"
    assert row["photo_published_at"] == FIXED_NOW
    assert (uploads / f"{uid}-1.png").is_file()


def test_publish_jpeg_sets_columns(client, uploads):
    uid = util.mk_user("solo2@example.com", "Solo")

    r = _publish(client, uid, _jpeg(), "image/jpeg")

    assert r.status_code == 200, r.text
    row = _row(uid)
    assert row["photo_path"] == f"{uid}-1.jpg"
    assert row["photo_revision"] == 1
    assert row["photo_published_at"] is not None
    assert (uploads / f"{uid}-1.jpg").is_file()


def test_publish_rejects_oversize_declared_body(client, uploads):
    uid = util.mk_user("big@example.com", "Big", photo=False)  # nothing stored before the upload
    _sign(client, uid)

    r = client.post(
        "/api/me/photo",
        content=b"\x00" * (6 * MB),
        headers={"Content-Type": "image/jpeg"},
    )

    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    assert _files(uploads) == []
    assert _row(uid)["photo_path"] is None


def test_publish_rejects_oversize_chunked_without_content_length(client, uploads):
    """The cap is enforced mid-stream: no Content-Length is ever consulted."""
    uid = util.mk_user("big2@example.com", "Big")
    _sign(client, uid)

    def chunks():
        for _ in range(6):
            yield b"\x00" * MB

    r = client.post(
        "/api/me/photo",
        content=chunks(),
        headers={"Content-Type": "image/jpeg"},
    )

    assert r.status_code == 413, r.text
    assert r.json()["error"] == "too_large"
    assert _files(uploads) == []


def test_publish_rejects_text_plain(client, uploads):
    uid = util.mk_user("txt@example.com", "Txt")
    _sign(client, uid)

    r = client.post(
        "/api/me/photo",
        content=b"this is definitely not a photo",
        headers={"Content-Type": "text/plain"},
    )

    assert r.status_code == 415
    assert r.json()["error"] in ("unsupported", "corrupt")
    assert _files(uploads) == []


def test_publish_rate_limited_on_sixth_upload(client, uploads, monkeypatch):
    uid = util.mk_user("spam@example.com", "Spam")
    _sign(client, uid)

    for i in range(5):
        r = client.post("/api/me/photo", content=_png(), headers={"Content-Type": "image/png"})
        assert r.status_code == 200, (i, r.text)

    sixth = client.post("/api/me/photo", content=_png(), headers={"Content-Type": "image/png"})
    assert sixth.status_code == 429
    assert sixth.json()["error"] == "rate_limited"

    # Exactly at the window edge the counter is still full; past it, uploads resume.
    monkeypatch.setattr(db, "_FIXED_NOW", "2026-10-24T12:09:59Z")
    still_limited = client.post("/api/me/photo", content=_png(), headers={"Content-Type": "image/png"})
    assert still_limited.status_code == 429

    monkeypatch.setattr(db, "_FIXED_NOW", "2026-10-24T12:10:01Z")
    after = client.post("/api/me/photo", content=_png(), headers={"Content-Type": "image/png"})
    assert after.status_code == 200, after.text


def test_publish_requires_session_and_writes_nothing(client, uploads):
    r = client.post("/api/me/photo", content=_png(), headers={"Content-Type": "image/png"})

    assert r.status_code == 401
    assert _files(uploads) == []
    # "writes nothing" must mean the DATABASE too, not just the uploads directory: no
    # publication row, no revision bump, no photo pointer.
    conn = db.connect()
    try:
        pubs = db.one(
            conn, "SELECT COUNT(*) AS n FROM consent_log WHERE action='photo:published'"
        )["n"]
        user = db.one(conn, "SELECT photo_path, photo_revision FROM users ORDER BY id LIMIT 1")
    finally:
        conn.close()
    assert pubs == 0, "an unauthenticated upload must not write a publication row"
    assert user is None or (user["photo_path"] is None and user["photo_revision"] == 0)


# --------------------------------------------------------------------------- serving


def test_serve_self_with_hardening_headers(client, uploads):
    uid = util.mk_user("me@example.com", "Me")
    assert _publish(client, uid).status_code == 200

    r = client.get(f"/media/photo/{uid}")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"] == "inline"
    assert r.content.startswith(b"\x89PNG")


def test_serve_counterpart_with_live_card_then_after_matching_withdrawn(client, uploads):
    a, b, _cid, _pid = _pair()
    assert _publish(client, b).status_code == 200

    _sign(client, a)
    ok = client.get(f"/media/photo/{b}")
    assert ok.status_code == 200, ok.text
    assert ok.headers["cache-control"] == "no-store"

    # B withdraws the matching grant: no live card, no photo.
    c = db.connect()
    try:
        with db.immediate(c):
            c.execute(
                "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='matching'",
                (db.utcnow(), b),
            )
    finally:
        c.close()

    _sign(client, a)
    assert client.get(f"/media/photo/{b}").status_code == 404


def test_serve_404_after_own_photo_deleted_even_for_self(client, uploads):
    uid = util.mk_user("gone@example.com", "Gone")
    assert _publish(client, uid).status_code == 200
    _sign(client, uid)
    assert client.get(f"/media/photo/{uid}").status_code == 200

    d = client.delete("/api/me/photo")

    assert d.status_code == 200
    assert d.json() == {"ok": True}
    assert _row(uid)["photo_published_at"] is None
    _sign(client, uid)
    assert client.get(f"/media/photo/{uid}").status_code == 404


def test_serve_404_after_block(client, uploads):
    a, b, _cid, _pid = _pair()
    assert _publish(client, b).status_code == 200
    _sign(client, a)
    assert client.get(f"/media/photo/{b}").status_code == 200

    c = db.connect()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (
                    db.user_hash(a),
                    db.user_hash(b),
                    db.utcnow(),
                    db.utcnow(days=30),
                ),
            )
    finally:
        c.close()

    _sign(client, a)
    assert client.get(f"/media/photo/{b}").status_code == 404


def test_serve_404_for_declined_candidate(client, uploads):
    a, b, cid, _pid = _pair()
    assert _publish(client, b).status_code == 200

    c = db.connect()
    try:
        with db.immediate(c):
            c.execute("UPDATE candidates SET state='declined' WHERE id=?", (cid,))
    finally:
        c.close()

    _sign(client, a)
    assert client.get(f"/media/photo/{b}").status_code == 404


def test_serve_anonymous_is_401(client, uploads):
    uid = util.mk_user("anon@example.com", "Anon")
    assert _publish(client, uid).status_code == 200

    client.cookies.clear()
    assert client.get(f"/media/photo/{uid}").status_code == 401


def test_serve_404_when_no_photo_published(client, uploads):
    a, b, _cid, _pid = _pair()
    _sign(client, a)
    assert client.get(f"/media/photo/{b}").status_code == 404


# --------------------------------------------------------------------------- replacement


def test_replacement_bumps_revision_removes_old_file_and_serves_new_bytes(client, uploads):
    a, b, _cid, _pid = _pair()
    assert _publish(client, b, _png("red"), "image/png").status_code == 200
    first = (uploads / f"{b}-1.png").read_bytes()

    r = _publish(client, b, _jpeg("green"), "image/jpeg")
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2

    row = _row(b)
    assert row["photo_path"] == f"{b}-2.jpg"
    # Gate review P1: per-request cleanup was removed because a request only knows its OWN
    # filename (a stale request could delete a newer upload). The old revision is now simply
    # unreferenced, and the sweeper reclaims it once it is past the grace window.
    assert (uploads / f"{b}-1.png").exists(), "old bytes stay until the sweeper runs"
    from server import sweep as sweep_mod

    stale = os.path.getmtime(uploads / f"{b}-1.png") - 2 * sweep_mod.PHOTO_GRACE_SECONDS
    os.utime(uploads / f"{b}-1.png", (stale, stale))
    conn = db.connect()
    try:
        counts = sweep_mod.run_sweep(conn)
    finally:
        conn.close()
    assert counts["photos_purged"] == 1
    assert not (uploads / f"{b}-1.png").exists(), "sweeper reclaims the unreferenced revision"
    assert _files(uploads) == [f"{b}-2.jpg"], "the LIVE revision must survive the sweep"

    _sign(client, a)
    served = client.get(f"/media/photo/{b}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/jpeg"
    assert served.content != first
    assert served.content.startswith(b"\xff\xd8")  # JPEG magic: the new image, not the old one
    assert served.headers["cache-control"] == "no-store"


def test_card_photo_url_gates_match_serving(client, uploads):
    """scheduling.card()'s seam must agree with the route: same gate, same refusals."""
    from server import photo_api

    a, b, cid, _pid = _pair()
    assert photo_api.card_photo_url(db.connect(), a, b) is None  # no photo yet

    assert _publish(client, b).status_code == 200
    c = db.connect()
    try:
        assert photo_api.card_photo_url(c, a, b) == f"/media/photo/{b}"
        assert photo_api.card_photo_url(c, b, b) is None  # self is never a card photo
        assert photo_api.card_photo_url(c, b, a) is None  # A published nothing
        c.execute("UPDATE candidates SET state='hidden' WHERE id=?", (cid,))
        assert photo_api.card_photo_url(c, a, b) is None
    finally:
        c.close()
