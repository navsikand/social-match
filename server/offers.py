"""Activity-offer data helpers shared by scheduling.py (card, confirm) and activity.py (the agent
endpoints). No imports from scheduling/activity so neither can create an import cycle.

Option B (privacy-first): an offer is a tag from the closed vocabulary plus an https link.
There is no free-text title or place name anywhere. The link is the only free-form string, so
it is validated hard: https only, ASCII, capped length, a real DNS host, no credentials, no
fragment, no IP literal. The server never fetches it; the receiving agent opens it itself.
"""
import ipaddress
import re
from urllib.parse import urlsplit

from server import db

MAX_ROUNDS = 3
MAX_URL_LEN = 300
REASONS = ("too_expensive", "too_far", "not_my_vibe", "wrong_time")
REASON_WORDS = {
    "too_expensive": "too expensive",
    "too_far": "too far",
    "not_my_vibe": "not their vibe",
    "wrong_time": "the wrong time",
}
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def validate_url(value: object) -> str:
    """Return the cleaned URL or raise ValueError with a short reason."""
    if not isinstance(value, str) or not value:
        raise ValueError("activity_url must be a non-empty string")
    if len(value) > MAX_URL_LEN:
        raise ValueError(f"activity_url is longer than {MAX_URL_LEN} characters")
    if not value.isascii() or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("activity_url must be plain ASCII with no spaces or control characters")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError("activity_url must start with https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("activity_url must not contain credentials")
    if parts.fragment:
        raise ValueError("activity_url must not contain a #fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("activity_url has an invalid port") from exc
    if port not in (None, 443):
        raise ValueError("activity_url must use the default https port")
    host = (parts.hostname or "").lower()
    if not _HOST_RE.match(host):
        raise ValueError("activity_url needs a real domain name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("activity_url must use a domain name, not an IP address")
    return value


def url_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def human_tag(tag: str) -> str:
    return tag.replace("_", " ")


def latest_offer(conn, proposal_id: int):
    return db.one(
        conn,
        "SELECT * FROM activity_offers WHERE proposal_id=? ORDER BY round DESC LIMIT 1",
        (proposal_id,),
    )


def accepted_offer(conn, proposal_id: int):
    return db.one(
        conn,
        "SELECT * FROM activity_offers WHERE proposal_id=? AND state='accepted' LIMIT 1",
        (proposal_id,),
    )


def card_view(conn, uid: int, proposal_id: int) -> dict | None:
    """The activity block a card shows `uid`, or None when no agent has proposed anything."""
    offer = latest_offer(conn, proposal_id)
    if offer is None:
        return None
    return {
        "offer_id": offer["id"],
        "round": offer["round"],
        "max_rounds": MAX_ROUNDS,
        "state": offer["state"],
        "tag": offer["activity_tag"],
        "url": offer["activity_url"],
        "host": url_host(offer["activity_url"]),
        "proposed_by_you": offer["proposer_id"] == uid,
        "reason": offer["reason"],
        "reason_text": REASON_WORDS.get(offer["reason"]) if offer["reason"] else None,
    }
