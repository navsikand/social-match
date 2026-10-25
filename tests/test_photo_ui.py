"""Photo UI tests (CONTRACT-photos §4, lane B-ui).

Covers the "Your photo" panel on /settings and the photo render path on /m/{id}.
Pure server-rendered-source assertions: these pin the copy, the upload contract
(blob body, Content-Type: image/jpeg, no FormData) and the fact that the older
grant controls survived the edit.
"""
import util


def _settings_html(client, email: str = "photo-settings@example.com") -> str:
    uid = util.mk_user(email, "Photo Person")
    util.direct_session(client, uid)
    r = client.get("/settings")
    assert r.status_code == 200, r.text
    return r.text


def test_settings_page_renders_photo_panel(client):
    html = _settings_html(client)
    assert "<h2>Publish this photo on my card</h2>" in html
    assert "before either person confirms" in html
    assert "Not to the public, not to anyone you haven't been matched with." in html
    assert "You can remove it any time." in html


def test_settings_photo_panel_controls(client):
    html = _settings_html(client, "photo-controls@example.com")
    assert 'accept="image/jpeg,image/png"' in html
    # preview element shown before publishing
    assert 'id="photo-preview"' in html
    # both actions, driven by the plain-text status line helper
    assert 'id="photo-publish"' in html
    assert 'id="photo-remove"' in html
    assert 'id="photo-status"' in html
    assert 'show(photoStatus' in html


def test_settings_photo_upload_contract_in_source(client):
    html = _settings_html(client, "photo-contract@example.com")
    # raw blob body, JPEG content type, no FormData anywhere near the photo panel
    assert "/api/me/photo" in html
    assert '"Content-Type": "image/jpeg"' in html
    assert "FormData" not in html
    # canvas downscale + JPEG re-encode at 0.85, capped at 1600 px
    assert "1600" in html
    assert '}, "image/jpeg", PHOTO_QUALITY);' in html
    assert "PHOTO_QUALITY = 0.85" in html
    assert "canvas.toBlob" in html or ".toBlob(" in html
    # withdraw path
    assert 'method: "DELETE"' in html


def test_settings_photo_panel_does_not_touch_grants(client):
    html = _settings_html(client, "photo-grants@example.com")
    # The only grants POST in the page is the pre-existing "Save sharing settings" one.
    assert html.count("/api/settings/grants") == 1
    assert 'id="photo-file"' in html.split("/api/settings/grants")[0]


def test_settings_keeps_existing_grant_controls(client):
    uid = util.mk_user("photo-old-ui@example.com", "Old UI")
    util.grant(uid, "matching")
    util.grant(uid, "publication", "card")
    util.direct_session(client, uid)
    r = client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert 'type="checkbox" id="matching"' in html
    assert 'checked' in html
    assert '<select id="publication"' in html
    assert 'data-current="card"' in html
    assert "Save sharing settings" in html
    assert 'id="delete-account"' in html


def test_card_page_references_photo_render_path(client):
    a = util.mk_user("photo-a@example.com", "Aya")
    b = util.mk_user("photo-b@example.com", "Bo")
    candidate_id, _proposal_id = util.mk_candidate(a, b)
    util.direct_session(client, b)
    r = client.get(f"/m/{candidate_id}")
    assert r.status_code == 200, r.text
    html = r.text
    # client-side render path reads about.photo_url
    assert "photo_url" in html
    assert 'class="card-photo"' in html
    assert 'alt="Their photo"' in html
    assert "onerror" in html
    # disclosure line under the photo / avatar
    assert "Shown only to people who receive this card." in html


def test_card_page_without_photo_keeps_avatar_glyph(client):
    a = util.mk_user("photo-c@example.com", "Cy")
    b = util.mk_user("photo-d@example.com", "Dee")
    candidate_id, _proposal_id = util.mk_candidate(a, b)
    util.direct_session(client, a)
    r = client.get(f"/m/{candidate_id}")
    assert r.status_code == 200
    assert 'id="avatar" class="avatar"' in r.text
    assert "AVATARS" in r.text
