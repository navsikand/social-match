"""P1d tests: register/login/logout, settings grants upsert, settings page (CONTRACT §11/§13).

Only webui-owned endpoints are exercised here; card/swipe/counter/revoke/dashboard/matches/
delete-account/pair endpoints belong to sibling phases.
"""
from server import db

import util

_TEST_PW = "correct horse battery staple"


def _payload(**over):
    body = {
        "email": "ava@example.com",
        "password": _TEST_PW,
        "display_name": "Ava",
        "avatar_id": 3,
        "adult_attested": True,
        "registration_code": "testiva",  # conftest REGISTRATION_CODE
    }
    body.update(over)
    return body


def _register(client, **over):
    r = client.post("/api/register", json=_payload(**over))
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def _error(resp):
    # Integrator update: app-level handler now flattens every HTTPException body to the
    # CONTRACT S2 shape {"error": ..., "detail": ...}, so read the top-level "error".
    body = resp.json()
    return body.get("error") if isinstance(body, dict) else None


def _grant_rows(uid, kind="matching"):
    conn = db.connect()
    try:
        return db.q(
            conn,
            "SELECT * FROM grants WHERE user_id=? AND kind=? ORDER BY version",
            (uid, kind),
        )
    finally:
        conn.close()


# ----------------------------------------------------------------- register/login


def test_register_sets_session_cookie(client):
    r = client.post("/api/register", json=_payload())
    assert r.status_code == 201
    assert "smsession=" in r.headers.get("set-cookie", "")
    assert client.cookies.get("smsession")


def test_register_wrong_invite_code(client):
    r = client.post("/api/register", json=_payload(registration_code="nope"))
    assert r.status_code == 403
    assert _error(r) == "invite_required"
    assert "smsession=" not in r.headers.get("set-cookie", "")


def test_register_requires_adult_attestation(client):
    r = client.post("/api/register", json=_payload(adult_attested=False))
    assert r.status_code == 403
    assert _error(r) == "attest_required"


def test_register_duplicate_email(client):
    _register(client)
    r = client.post("/api/register", json=_payload(display_name="Ava Two"))
    assert r.status_code == 409


def test_login_and_wrong_password(client):
    uid = _register(client)
    client.cookies.clear()

    bad = client.post("/api/login", json={"email": "ava@example.com", "password": "wrong"})
    assert bad.status_code == 401
    assert client.cookies.get("smsession") is None

    ok = client.post("/api/login", json={"email": "ava@example.com", "password": _TEST_PW})
    assert ok.status_code == 200
    assert ok.json()["user_id"] == uid
    assert "smsession=" in ok.headers.get("set-cookie", "")


def test_logout_clears_cookie(client):
    _register(client)
    assert client.cookies.get("smsession")
    r = client.post("/api/logout")
    assert r.status_code == 200
    assert "smsession=" in r.headers.get("set-cookie", "")
    assert client.cookies.get("smsession") is None

    after = client.post("/api/settings/grants", json={"matching": True})
    assert after.status_code == 401
    assert _error(after) == "session_invalid"


# ------------------------------------------------------------------ settings grants


def test_settings_grants_requires_session(client):
    r = client.post("/api/settings/grants", json={"matching": True})
    assert r.status_code == 401
    assert _error(r) == "session_invalid"


def test_matching_grant_upsert_withdraws_and_bumps_version(client):
    uid = _register(client)

    first = client.post("/api/settings/grants", json={"matching": True})
    assert first.status_code == 200
    rows = _grant_rows(uid, "matching")
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["withdrawn_at"] is None
    assert first.json()["grants"]["matching"]["version"] == 1

    second = client.post("/api/settings/grants", json={"matching": True})
    assert second.status_code == 200
    rows = _grant_rows(uid, "matching")
    assert [r["version"] for r in rows] == [1, 2]
    assert rows[0]["withdrawn_at"] is not None  # old row withdrawn
    assert rows[1]["withdrawn_at"] is None  # version 2 live
    assert second.json()["grants"]["matching"]["version"] == 2

    conn = db.connect()
    try:
        actions = [
            r["action"]
            for r in db.q(
                conn,
                "SELECT action FROM consent_log WHERE user_id_hash=? ORDER BY id",
                (db.user_hash(uid),),
            )
        ]
    finally:
        conn.close()
    assert "grant:matching" in actions
    assert "withdraw:matching" in actions


def test_publication_off_withdraws_live_grant(client):
    uid = _register(client)

    first = client.post("/api/settings/grants", json={"publication": {"tier": "card"}})
    assert first.status_code == 200
    assert first.json()["grants"]["publication"]["tier"] == "card"

    off = client.post("/api/settings/grants", json={"publication": {"tier": "off"}})
    assert off.status_code == 200
    assert off.json()["grants"]["publication"] is None

    rows = _grant_rows(uid, "publication")
    assert len(rows) == 1
    assert rows[0]["withdrawn_at"] is not None

    conn = db.connect()
    try:
        actions = [
            r["action"]
            for r in db.q(
                conn,
                "SELECT action FROM consent_log WHERE user_id_hash=? ORDER BY id",
                (db.user_hash(uid),),
            )
        ]
    finally:
        conn.close()
    assert "grant:publication" in actions
    assert "withdraw:publication" in actions


def test_settings_grants_rejects_bad_tier(client):
    _register(client)
    r = client.post("/api/settings/grants", json={"publication": {"tier": "everything"}})
    assert r.status_code == 422


# ----------------------------------------------------------------------- pages


def test_settings_page_renders_when_signed_in(client):
    uid = util.mk_user("ben@example.com", "Ben")
    util.direct_session(client, uid)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Sharing settings" in r.text


def test_settings_page_reflects_live_grant(client):
    uid = util.mk_user("cara@example.com", "Cara")
    util.grant(uid, "matching")
    util.grant(uid, "publication", "card+name")
    util.direct_session(client, uid)
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'data-current="card+name"' in r.text
    assert "checked" in r.text


def test_dashboard_redirects_without_session(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_public_pages_render(client):
    for path, needle in (
        ("/register", "Create your account"),
        ("/login", "Sign in"),
        ("/privacy", "Privacy"),
        ("/terms", "Terms"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert needle in r.text
