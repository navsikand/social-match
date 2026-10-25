"""Settings / auth pages UI fixes (lane A): A4, B2, C4, D2, D3, D4, D6, D7, D8.

Source-level assertions for the client behaviour (this stack has no browser test harness) plus
real HTTP checks for every rule that the server also enforces.
"""
import util
from server import db


def _settings(client, email: str = "settings-ui@example.com") -> tuple[int, str]:
    uid = util.mk_user(email, "Settings Person")
    util.direct_session(client, uid)
    r = client.get("/settings")
    assert r.status_code == 200, r.text
    return uid, r.text


def _q(sql: str, args: tuple = ()):
    conn = db.connect()
    try:
        return db.q(conn, sql, args)
    finally:
        conn.close()


# --------------------------------------------------------------------- A4 matching off


def test_matching_off_states_consequence_and_confirms(client):
    _, html = _settings(client)
    # The consequence is stated inside the control itself...
    assert "Turning this off deletes your saved free-time windows and" in html
    assert "cancels open and confirmed plans." in html
    # ...and matching:false is only posted after an explicit confirm().
    confirm_at = html.index("window.confirm(")
    assert "Turn matching off?" in html
    post_at = html.index("postJSON(GRANTS_URL, payload)")
    assert confirm_at < post_at
    assert "if (payload.matching === false)" in html


def test_matching_off_cascade_is_unchanged_server_side(client):
    uid, _ = _settings(client)
    util.grant(uid, "matching")
    util.windows(uid, [("2026-10-29T19:00:00", "2026-10-29T21:00:00")])
    assert _q("SELECT id FROM availability_windows WHERE user_id=?", (uid,))

    r = client.post("/api/settings/grants", json={"matching": False})
    assert r.status_code == 200, r.text
    assert r.json()["grants"]["matching"] is None
    assert _q("SELECT id FROM availability_windows WHERE user_id=?", (uid,)) == []
    assert _q("SELECT user_id FROM availability WHERE user_id=?", (uid,)) == []


# ------------------------------------------------------------------- B2 double submit


def test_submit_buttons_disable_for_the_request(client):
    uid, html = _settings(client, "save-disable@example.com")
    assert 'const saveBtn = document.getElementById("save");' in html
    assert "if (saveBtn.disabled) return;" in html
    assert "saveBtn.disabled = true;" in html
    assert "saveBtn.disabled = false;" in html

    register = client.get("/register").text
    assert 'form.querySelector("button[type=submit]")' in register
    assert "if (submitBtn.disabled) return;" in register
    assert "if (!navigating) submitBtn.disabled = false;" in register

    login = client.get("/login").text
    assert 'form.querySelector("button[type=submit]")' in login
    assert "if (submitBtn.disabled) return;" in login


# -------------------------------------------------------------------- C4 apiError


def test_api_error_maps_known_codes_before_raw_detail(client):
    html = client.get("/login").text
    assert "API_ERROR_MESSAGES" in html
    for code, sentence in (
        ("invite_required", "That registration code is not valid."),
        ("attest_required", "You must confirm you are 18 or older."),
        ("email_taken", "An account with that email already exists."),
        ("payload_forbidden", "Some of the details were missing or not allowed."),
        ("session_invalid", "Your session expired — sign in again."),
        ("rate_limited", "Too many attempts just now"),
    ):
        assert code + ":" in html, code
        assert sentence in html, code
    # The top-level code is tested BEFORE the raw detail string.
    assert html.index("data && data.error") < html.index("data && data.detail")


# ------------------------------------------------------------------------ D7 password


def test_password_rule_client_and_server(client):
    register = client.get("/register").text
    assert "novalidate" not in register
    assert 'minlength="8"' in register
    assert 'required' in register

    r = client.post(
        "/api/register",
        json={
            "email": "short-pw@example.com",
            "password": "short",
            "display_name": "Short",
            "avatar_id": 1,
            "adult_attested": True,
            "registration_code": "testiva",
        },
    )
    assert r.status_code == 422, r.text
    assert r.json().get("error") == "payload_forbidden"
    assert "at least 8" in str(r.json().get("detail"))


# ------------------------------------------------------------------ D2 nav / D3 mobile


def test_nav_marks_current_page(client):
    _, settings = _settings(client, "nav-current@example.com")
    assert '<a href="/settings" aria-current="page">' in settings
    login = client.get("/login").text
    assert '<a href="/login" aria-current="page">' in login
    register = client.get("/register").text
    assert '<a href="/register" aria-current="page">' in register


def test_phone_header_and_register_order_css(client):
    css = client.get("/static/style.css").text
    block = css[css.index("@media (max-width: 560px)") :]
    assert "flex-direction: row" in block  # D2: one header row
    assert "overflow-x: auto" in block  # D2: nav scrolls sideways
    assert "min-height: 2.75rem" in block  # D2: 44 px nav/linklike targets
    assert "flex-direction: column-reverse" in block  # D3: form above the marketing
    assert 'a[aria-current="page"]' in css
    assert "min-height: 2.75rem" in css.split("button.linklike")[1].split("}")[0]


# ---------------------------------------------------------------------- D4 announcements


def test_notice_nodes_are_announced(client):
    uid, settings = _settings(client, "a11y@example.com")
    for node in ("photo-status", "delegation-status", "settings-status", "delete-status"):
        marker = f'id="{node}" class="notice" role="status" aria-live="polite'
        assert marker in settings, node
    assert 'aria-busy="false"' in settings
    # show() upgrades an error to role=alert and marks in-flight requests busy.
    base = client.get("/login").text
    assert 'el.setAttribute("role", isError ? "alert" : "status")' in base
    assert 'el.setAttribute("aria-busy", kind === "info" ? "true" : "false")' in base

    register = client.get("/register").text
    assert 'id="register-status" class="notice" role="status" aria-live="polite"' in register
    login = client.get("/login").text
    assert 'id="login-status" class="notice" role="status" aria-live="polite"' in login


# ------------------------------------------------------------------------- D6 photo state


def test_photo_panel_renders_published_state_on_load(client):
    uid, html = _settings(client, "photo-load@example.com")
    # Nothing published yet: no src, Remove disabled, input labelled.
    assert 'for="photo-file"' in html
    assert 'id="photo-preview"' in html
    assert 'data-published="false"' in html
    assert "src=\"/media/photo/" not in html

    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE users SET photo_published_at=? WHERE id=?", (db.utcnow(), uid)
            )
    finally:
        conn.close()

    html = client.get("/settings").text
    assert f'src="/media/photo/{uid}"' in html
    assert 'data-published="true"' in html
    assert "disabled>Remove</button>" not in html
    # Remove asks before deleting.
    assert "Remove your published photo?" in html
    assert "window.confirm(" in html.split("photoRemove.addEventListener")[1]


# ----------------------------------------------------------------------- D8 deletion


def test_delete_redirects_to_login_with_notice_and_clears_cookie(client):
    _, html = _settings(client, "delete-redirect@example.com")
    assert 'window.location.href = "/login?deleted=1";' in html
    assert 'fetch("/api/logout", {method: "POST"})' in html
    login = client.get("/login?deleted=1").text
    assert "Your account was deleted." in login
    # The notice is only shown on the deleted landing, not on a plain /login.
    plain = client.get("/login").text
    notice = plain.split('id="login-notice"')[1].split(">")[0]
    assert "hidden" in notice
