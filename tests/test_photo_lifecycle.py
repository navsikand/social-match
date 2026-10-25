"""Lifecycle regressions from the implementation gate review (FIX/BLOCK, 4 x P1).

Each test names the finding it pins. These are the cases the happy-path suite could not see:
deletion leaving bytes behind, the sweeper eating a live photo, concurrent uploads dodging the
rate limit, and the card emitting the photo URL where the template does not look for it.
"""
from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]
from PIL import Image

from server import db, sweep
from tests import util  # noqa: F401  (seed helpers used indirectly by fixtures)

FIXED_NOW = "2026-10-24T12:00:00Z"
TZ = "America/Los_Angeles"
WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    yield
    db._FIXED_NOW = None


@pytest.fixture()
def uploads(db_env, monkeypatch, tmp_path):
    directory = tmp_path / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(directory))
    db.init_db()
    directory.mkdir(exist_ok=True)
    return directory


@pytest.fixture()
def client(uploads):
    from server.app import app

    with TestClient(app) as c:
        yield c


def _register(client, email="life@x.test", name="Life"):
    r = client.post(
        "/api/register",
        json={
            "email": email,
            "password": "pw-123456",
            "display_name": name,
            "avatar_id": 2,
            "adult_attested": True,
            "registration_code": "testiva",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def _jpeg_bytes(size=(64, 64)) -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def _publish(client, blob: bytes | None = None):
    return client.post(
        "/api/me/photo",
        content=blob if blob is not None else _jpeg_bytes(),
        headers={"Content-Type": "image/jpeg"},
    )


# ------------------------------------------------------- P1: deletion must unlink


def test_account_deletion_unlinks_the_photo_file(client, uploads):
    uid = _register(client)
    files = lambda: [p for p in uploads.iterdir() if p.suffix in (".jpg", ".png")]  # noqa: E731
    assert _publish(client).status_code == 200
    assert len(files()) == 1

    assert client.post("/api/delete-account", json={"confirm": True}).status_code == 200
    assert files() == [], "account deletion must remove the image bytes, not just the pointer"
    conn = db.connect()
    try:
        row = db.one(conn, "SELECT photo_path, photo_published_at FROM users WHERE id=?", (uid,))
    finally:
        conn.close()
    assert row["photo_path"] is None and row["photo_published_at"] is None


# --------------------------------- P1: sweeper must not eat a live photo


def test_sweeper_deletes_only_unreferenced_and_stale(client, uploads):
    _register(client)
    assert _publish(client).status_code == 200
    live = next(p for p in uploads.iterdir() if p.suffix == ".jpg")

    stale_orphan = uploads / "999-1.jpg"
    stale_orphan.write_bytes(_jpeg_bytes())
    fresh_orphan = uploads / "998-1.jpg"
    fresh_orphan.write_bytes(_jpeg_bytes())

    old = 2 * sweep.PHOTO_GRACE_SECONDS
    stale_at = os.path.getmtime(stale_orphan) - old
    os.utime(stale_orphan, (stale_at, stale_at))
    # live photo is old too: being REFERENCED must protect it regardless of age
    live_at = os.path.getmtime(live) - old
    os.utime(live, (live_at, live_at))

    conn = db.connect()
    try:
        counts = sweep.run_sweep(conn)
    finally:
        conn.close()

    assert counts["photos_purged"] == 1
    assert live.exists(), "a referenced photo must never be purged, however old it is"
    assert not stale_orphan.exists(), "unreferenced + past grace is exactly what gets purged"
    assert fresh_orphan.exists(), "unreferenced but INSIDE the grace window must survive"


# --------------------------------- P1: rate limit must hold under concurrency


def test_rate_limit_is_authoritative_inside_the_transaction(client, uploads):
    uid = _register(client)
    # Four recent publications already: one more is allowed, the one after that is not.
    conn = db.connect()
    try:
        with db.immediate(conn):
            for _ in range(4):
                conn.execute(
                    "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (db.user_hash(uid), "photo:published", "photo", "{}", db.utcnow()),
                )
    finally:
        conn.close()

    results: list[int] = []
    lock = threading.Lock()

    def attempt():
        from server.app import app

        with TestClient(app) as c:
            c.cookies.set("smsession", client.cookies.get("smsession"))
            code = _publish(c).status_code
        with lock:
            results.append(code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == [200, 429], f"exactly one upload may pass the 5-per-10min limit: {results}"
    conn = db.connect()
    try:
        n = db.one(
            conn,
            "SELECT COUNT(*) AS n FROM consent_log WHERE user_id_hash=? AND action='photo:published'",
            (db.user_hash(uid),),
        )["n"]
    finally:
        conn.close()
    assert n == 5


# ------------------------- P1: unlink failure must still revoke serving


def test_unlink_failure_still_refuses_to_serve(client, uploads, monkeypatch):
    _register(client)
    assert _publish(client).status_code == 200
    assert client.get("/media/photo/1").status_code == 200

    def boom(*a, **k):
        raise OSError("disk says no")

    # The unlink genuinely fails here: account deletion is the path that touches the bytes.
    monkeypatch.setattr(os, "unlink", boom)
    assert client.post("/api/delete-account", json={"confirm": True}).status_code == 200
    # Revocation is the DB, so serving stops even though the file survived the failed unlink.
    # Revocation is the DB, so the photo stops being served even though the bytes survived
    # the failed unlink. The account is gone, so the session is invalid: 401, never 200.
    assert client.get("/media/photo/1").status_code != 200
    assert any(p.suffix == ".jpg" for p in uploads.iterdir()), (
        "the injected unlink failure should leave the bytes behind — otherwise this test "
        "proves nothing about the DB being the switch"
    )


def test_withdrawn_photo_is_reclaimed_by_the_sweeper(client, uploads):
    """Gate review P1: withdrawal must ALSO drop the reference, or 'unreferenced' never
    becomes true and the bytes live forever behind a publication that was revoked."""
    _register(client)
    assert _publish(client).status_code == 200
    stored = next(p for p in uploads.iterdir() if p.suffix == ".jpg")
    assert client.delete("/api/me/photo").status_code == 200

    conn = db.connect()
    try:
        row = db.one(conn, "SELECT photo_path, photo_published_at FROM users WHERE id=1")
    finally:
        conn.close()
    assert row["photo_published_at"] is None
    assert row["photo_path"] is None, "a withdrawn photo must stop being 'referenced'"

    stale = os.path.getmtime(stored) - 2 * sweep.PHOTO_GRACE_SECONDS
    os.utime(stored, (stale, stale))
    conn = db.connect()
    try:
        counts = sweep.run_sweep(conn)
    finally:
        conn.close()
    assert counts["photos_purged"] == 1
    assert not stored.exists(), "withdrawal + grace must actually reclaim the bytes"


# ------------------- P1: the card must expose the URL where the template reads it


def test_card_exposes_photo_url_inside_about(client, uploads):
    ava = util.mk_user("c1@x.test", "Ava")
    ben = util.mk_user("c2@x.test", "Ben")
    util.pub_card(ben)
    cid, _ = util.mk_candidate(ava, ben)

    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE users SET photo_path=?, photo_revision=1, photo_published_at=? WHERE id=?",
                ("2-1.jpg", db.utcnow(), ben),
            )
        (uploads / "2-1.jpg").write_bytes(_jpeg_bytes())
    finally:
        conn.close()

    util.direct_session(client, ava)
    card = client.get(f"/api/card/{cid}")
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["about"]["photo_url"] == f"/media/photo/{ben}"
    assert "photo_url" not in body, "top-level photo_url was the bug: the template reads about.*"


# ------------------- P2: closed candidate must never carry an actionable link


def test_closed_candidate_with_future_window_has_no_card_link(client):
    ava = util.mk_user("c3@x.test", "Ava")
    ben = util.mk_user("c4@x.test", "Ben")
    util.pub_card(ava)
    util.pub_card(ben)
    cid, pid = util.mk_candidate(ava, ben)

    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute("UPDATE proposals SET state='both_confirmed' WHERE id=?", (pid,))
            conn.execute(
                "UPDATE candidates SET state='closed', closed_at=? WHERE id=?", (db.utcnow(), cid)
            )
    finally:
        conn.close()

    util.direct_session(client, ava)
    rows = client.get("/api/matches").json()["matches"]
    row = next(r for r in rows if r["candidate_id"] == cid)
    assert row["card_url"] is None, "closed candidates reject card(); a link would be a dead end"


# -------------------------------------------------- P2: migration is idempotent + safe


def test_migration_is_idempotent_and_serialized(db_env, tmp_path):
    db.init_db()
    db.init_db()  # repeated startup must be a no-op, not an error

    errors: list[BaseException] = []

    def boot():
        try:
            db.init_db()
        except BaseException as exc:  # noqa: BLE001 - we want the failure text
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent initializers must not collide: {errors}"
    conn = db.connect()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    finally:
        conn.close()
    assert cols.count("photo_path") == 1
    assert cols.count("photo_revision") == 1
    assert cols.count("photo_published_at") == 1
