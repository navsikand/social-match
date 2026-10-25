"""Lane F: the user-facing chat surface (CONTRACT-chat.md §5).

What this file covers: the two page routes' session gate, the chat page's unconfigured banner
and its wiring to chat.py's POST body field, the settings delegation toggle round-trip (DB
truth, not just the echo) and the demo-box registration seeding. No provider calls, no network.
"""
from __future__ import annotations

import pytest

from server import config, db
from tests import util

FIXED_NOW = "2026-10-24T12:00:00Z"
FAKE_AI_URL = "https://ai.example.test/v1/chat/completions"
_REGISTER = {
    "email": "fi@example.com",
    "password": "correct horse battery staple",
    "display_name": "Fi",
    "avatar_id": 2,
    "adult_attested": True,
    "registration_code": "testiva",  # conftest REGISTRATION_CODE
}


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    yield
    db._FIXED_NOW = None


def _rows(sql: str, params: tuple = ()):
    conn = db.connect()
    try:
        return db.q(conn, sql, params)
    finally:
        conn.close()


def _grant_rows(uid: int, kind: str = "delegation"):
    return _rows(
        "SELECT * FROM grants WHERE user_id=? AND kind=? ORDER BY version", (uid, kind)
    )


def _consent_actions(uid: int) -> list[str]:
    return [
        row["action"]
        for row in _rows(
            "SELECT action FROM consent_log WHERE user_id_hash=? ORDER BY id",
            (db.user_hash(uid),),
        )
    ]


def _register(client, **over) -> int:
    body = dict(_REGISTER)
    body.update(over)
    res = client.post("/api/register", json=body)
    assert res.status_code == 201, res.text
    return res.json()["user_id"]


# ------------------------------------------------------------------- chat page + banner


def test_chat_page_shows_banner_when_ai_unconfigured(client, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: None)
    uid = util.mk_user("ui-unconfigured@example.com", "Una")
    util.direct_session(client, uid)

    res = client.get("/chat")
    assert res.status_code == 200
    assert "The assistant is not configured" in res.text
    assert "AI_API_URL" in res.text
    # The composer is hidden entirely when chat is off.
    assert 'id="composer"' not in res.text
    assert 'id="composer-text"' not in res.text


def test_chat_page_hides_banner_and_wires_content_field_when_configured(client, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: FAKE_AI_URL)
    monkeypatch.setattr(config, "ai_model", lambda: "fake-model-1")
    uid = util.mk_user("ui-configured@example.com", "Con")
    util.direct_session(client, uid)

    res = client.get("/chat")
    assert res.status_code == 200
    assert "The assistant is not configured" not in res.text
    assert 'id="composer"' in res.text
    assert "fake-model-1" in res.text

    # chat.py's POST handler reads {"content": ...}; the composer must send exactly that.
    assert 'JSON.stringify({content: text})' in res.text
    assert 'fetch("/api/chat"' in res.text
    # Model/action/error text is rendered as text nodes only, never as markup.
    assert "innerHTML" not in res.text
    assert "createTextNode" in res.text


def test_chat_page_redirects_anonymous(client):
    res = client.get("/chat", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=/chat"


# --------------------------------------------------------------------- schedule gate
# Only the session gate is lane F's: signed-in rendering belongs to the integration gate
# that runs once lane C's schedule.html is installed.


def test_schedule_page_redirects_anonymous(client):
    res = client.get("/schedule", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=/schedule"


# ------------------------------------------------------------- delegation grants


def test_settings_delegation_toggle_round_trip(client):
    uid = _register(client)

    on = client.post("/api/settings/grants", json={"delegation": True})
    assert on.status_code == 200, on.text
    assert on.json()["grants"]["delegation"]["version"] == 1
    live = _grant_rows(uid)
    assert len(live) == 1
    assert live[0]["version"] == 1
    assert live[0]["withdrawn_at"] is None
    assert live[0]["tier"] is None
    assert "grant:delegation" in _consent_actions(uid)

    off = client.post("/api/settings/grants", json={"delegation": False})
    assert off.status_code == 200, off.text
    assert off.json()["grants"]["delegation"] is None
    rows = _grant_rows(uid)
    assert len(rows) == 1  # withdrawal only, no new version
    assert rows[0]["withdrawn_at"] is not None
    actions = _consent_actions(uid)
    assert "withdraw:delegation" in actions

    # Re-granting after a withdrawal creates a fresh live row (the existing helper keeps
    # versioning relative to the live row only, so this restarts at 1).
    again = client.post("/api/settings/grants", json={"delegation": True})
    assert again.status_code == 200, again.text
    rows = _grant_rows(uid)
    assert [row["version"] for row in rows] == [1, 1]
    assert rows[0]["withdrawn_at"] is not None
    assert rows[1]["withdrawn_at"] is None


def test_settings_page_renders_delegation_toggle(client):
    uid = util.mk_user("ui-settings@example.com", "Set")
    util.grant(uid, "delegation")
    util.direct_session(client, uid)

    res = client.get("/settings")
    assert res.status_code == 200
    assert "Your assistant" in res.text
    assert "Let my assistant accept, counter or pass offers for me" in res.text
    assert 'id="delegation"' in res.text
    assert 'checked' in res.text.split('id="delegation"')[1].split(">")[0]


# ------------------------------------------------------------ registration seeding


def test_registration_seeds_delegation_grant_when_default_on(client, monkeypatch):
    monkeypatch.setattr(config, "delegation_default_on", lambda: True)
    uid = _register(client, email="seed-on@example.com")

    rows = _grant_rows(uid)
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["withdrawn_at"] is None
    assert rows[0]["granted_at"] == FIXED_NOW
    assert rows[0]["expires_at"] > rows[0]["granted_at"]
    assert "grant:delegation" in _consent_actions(uid)


def test_registration_does_not_seed_delegation_by_default(client, monkeypatch):
    monkeypatch.setattr(config, "delegation_default_on", lambda: False)
    uid = _register(client, email="seed-off@example.com")

    assert _grant_rows(uid) == []
    assert "grant:delegation" not in _consent_actions(uid)
    # Nothing else was seeded either: registration is otherwise byte-identical.
    other = _rows("SELECT kind, tier FROM grants WHERE user_id=?", (uid,))
    assert other == []


def test_registration_seeded_grant_is_echoed_on_chat_page(client, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: None)
    monkeypatch.setattr(config, "delegation_default_on", lambda: True)
    _register(client, email="seed-echo@example.com")

    res = client.get("/chat")
    assert res.status_code == 200
    assert "delegation" in res.text  # chip rail JS names the signal it renders
    # The chip payload is fetched from the API; the DB truth is what the UI renders.
    history = client.get("/api/chat/history")
    assert history.status_code == 200
    assert history.json()["state"]["delegation"] is True


# ------------------------------------------------------- negotiations panel (finding 6-UI)
# UI tests cannot run JS, so the assertions pin the exact template markers the script relies on:
# ids, endpoint URLs, request-body field names and the closed vocabularies. Drift in any of them
# is a broken control, so a text assertion is the honest test.

import re  # noqa: E402

from server import offers, schema  # noqa: E402


def _chat_html(client) -> str:
    res = client.get("/chat")
    assert res.status_code == 200
    return res.text


def _template_block(html: str, template_id: str) -> str:
    """The rendered contents of <template id="...">…</template>."""
    marker = f'id="{template_id}"'
    at = html.index(marker)
    start = html.index(">", at) + 1
    end = html.index("</template>", start)
    return html[start:end]


def test_chat_page_negotiations_panel_and_offer_respond_controls(client, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: FAKE_AI_URL)
    uid = util.mk_user("ui-offers@example.com", "Off")
    util.direct_session(client, uid)
    html = _chat_html(client)

    # The panel and its parts exist on the page itself (no JS needed to see the surface).
    assert 'id="negotiations"' in html
    assert 'id="negotiations-list"' in html
    assert 'id="delegation-note"' in html
    assert 'id="negotiations-empty"' in html
    assert "Negotiations" in html

    # The manual endpoint chat.py serves, plus the activity view it renders from.
    assert '"/api/chat/offer-respond"' in html
    assert '"/api/agent/activity/"' in html          # FIXES-chat.md 6-UI endpoint
    assert '"/api/card/"' in html                    # session-authenticated fallback
    assert '"/api/dashboard"' in html                # open candidates

    # Exactly the request bodies /api/chat/offer-respond's _parse_offer_body expects.
    assert 'const body = {candidate_id: item.candidate_id, offer_id: item.activity.offer_id};' in html
    assert 'respondOffer(item, {action: "accept"})' in html
    assert 'proposal_version: item.proposal_version' in html
    assert 'tag: tag, url: url' in html
    assert 'respondOffer(item, {action: "pass", reason: reason})' in html
    # accept carries nothing else; pass carries only a reason (enforced by the endpoint).
    assert "activity_tag" not in html


def test_chat_page_counter_tags_are_the_schema_vocabulary(client):
    uid = util.mk_user("ui-tags@example.com", "Tag")
    util.direct_session(client, uid)
    block = _template_block(_chat_html(client), "tag-options")
    pairs = re.findall(r'<option value="([a-z_]+)">([^<]+)</option>', block)
    assert [value for value, _label in pairs] == list(schema.TAG_VOCAB)
    # Human words, not raw identifiers.
    for value, label in pairs:
        assert label == value.replace("_", " ")


def test_chat_page_pass_reasons_are_the_offers_vocabulary(client):
    uid = util.mk_user("ui-reasons@example.com", "Rea")
    util.direct_session(client, uid)
    block = _template_block(_chat_html(client), "reason-options")
    pairs = re.findall(r'<option value="([a-z_]+)">([^<]+)</option>', block)
    assert [value for value, _label in pairs] == list(offers.REASONS)
    assert dict(pairs) == {code: offers.REASON_WORDS[code] for code in offers.REASONS}


def test_chat_page_delegation_note_reflects_the_grant(client):
    on = util.mk_user("ui-note-on@example.com", "On")
    util.grant(on, "delegation")
    util.direct_session(client, on)
    html = _chat_html(client)
    assert "Your assistant can accept, counter or pass offers for you" in html
    assert "Your assistant will not answer offers for you" not in html

    off = util.mk_user("ui-note-off@example.com", "Off")
    util.direct_session(client, off)
    html = _chat_html(client)
    assert "Your assistant will not answer offers for you" in html
    assert "Your assistant can accept, counter or pass offers for you" not in html


# --------------------------------------------------- sticky status line (finding 14)

def test_chat_status_line_error_is_sticky_across_done(client):
    uid = util.mk_user("ui-sticky@example.com", "Sti")
    util.direct_session(client, uid)
    html = _chat_html(client)

    # The flag exists and starts clear.
    assert "let statusSticky = false;" in html
    # error/conflict events RAISE it (notifySticky), normal messages clear it (notify).
    assert "function notifySticky(text, cls) {" in html
    sticky_body = html.split("function notifySticky(text, cls) {", 1)[1].split("}", 1)[0]
    assert "statusSticky = true;" in sticky_body
    plain_body = html.split("function notify(text, cls) {", 1)[1].split("}", 1)[0]
    assert "statusSticky = false;" in plain_body
    assert "notifySticky(errorLine(event.code, event.detail));" in html
    assert 'notifySticky("Nothing was applied: "' in html

    # The end-of-stream hide is GUARDED by the flag: done cannot wipe a sticky error.
    assert "} else if (!statusSticky) {" in html
    assert html.count("status.hidden = true;") == 1

    # A NEW SEND is what clears it — before the "Thinking…" line, not on stream end.
    send = html.split("async function sendTurn(text) {", 1)[1]
    assert send.index("statusSticky = false;") < send.index('notify("Thinking…", "info");')


# ------------------------------------------- pending sections / ICS routing (finding 13-UI)

def test_chat_pending_renders_sections_and_routes_ics_confirm(client):
    uid = util.mk_user("ui-pending@example.com", "Pen")
    util.direct_session(client, uid)
    html = _chat_html(client)

    # Grouped windows/events display, tolerant of BOTH the flat payload and F13-core's sections.
    assert "function pendingSections(pending) {" in html
    assert "items.windows" in html and "items.events" in html
    assert 'const WINDOWS_LABEL = "Free windows";' in html
    assert 'const EVENTS_LABEL = "Events";' in html
    assert 'heading.textContent = section.label + " (" + section.items.length + ")"' in html
    # Riders ride as muted lines, only from data actually present.
    assert "function pendingRiders(pending) {" in html
    assert '"also updating: "' in html
    assert 'set_interests: "interests"' in html

    # ICS previews confirm through the schedule endpoint with the row id + the ticked indices.
    assert '"/api/schedule/ics/confirm"' in html
    assert "body: {id: id, pick: cancel ? [] : pickedIndices()}" in html
    assert 'box.type = "checkbox";' in html
    assert 'box.dataset.pick = String(shown);' in html
    assert '"/api/chat/confirm", body: cancel ? {id: id, cancel: true} : {id: id}' in html
    # The preview kind decides the route; it is read back off the rendered box.
    assert "pendingBox.dataset.pendingKind = kind;" in html
