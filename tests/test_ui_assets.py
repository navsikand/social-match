"""The web UI must not contact third parties: fonts are served from /static/fonts."""
import re

PAGES = ["/login", "/register"]


def test_pages_reference_no_external_hosts(client):
    for path in PAGES:
        html = client.get(path).text
        # No stylesheet, script, font or image may come from another host.
        external = re.findall(r'(?:href|src)="(https?://[^"]+)"', html)
        assert external == [], f"{path} loads from another host: {external}"
        assert "googleapis" not in html and "gstatic" not in html


def test_stylesheet_has_no_external_urls_and_fonts_are_served(client):
    css = client.get("/static/style.css").text
    assert "googleapis" not in css and "gstatic" not in css
    assert not re.search(r"url\(\s*[\"']?https?://", css)
    for font in re.findall(r'url\("(/static/fonts/[^"]+\.woff2)"\)', css):
        r = client.get(font)
        assert r.status_code == 200 and len(r.content) > 1000, font
        assert r.content[:4] == b"wOF2", f"{font} is not a woff2 file"


def test_brand_name_is_consistent(client):
    for path in PAGES:
        html = client.get(path).text
        assert "Social Match" in html
        assert "Social Match" in html
