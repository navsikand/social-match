"""Public events a student says they could go to (Track 2 pairing signals).

Cubbie reads the student's inbox and calendar on the phone and sends only the PUBLIC part of an
event: a link to its public page, when it happens, and one category from a closed list. No
title, no sender, no email text, no location, no RSVP details. Two students whose links
canonicalise to the same string are going to / considering the same event, which is the
strongest signal the matcher gets.

The link is the one free-form string here, so it gets the same hard validation as activity
links (https only, real domain, no credentials, no fragment) and one more rule: everything
after `?` is dropped. Invitation emails often carry a personal RSVP token in the query string,
and the server has no business storing that. A path can still carry a token; the Skill tells
Cubbie to send the public page, never a personal invite link, and the student approves the list
before it leaves the phone. That residual risk is listed in docs/privacy.md.
"""
from urllib.parse import urlsplit

from server import offers

EVENT_CATEGORIES: tuple[str, ...] = (
    "career",
    "club",
    "talk",
    "music",
    "sports",
    "arts",
    "food",
    "party",
    "volunteer",
    "other",
)
MAX_EVENTS = 10
MAX_EVENT_HOURS = 24
EVENT_HORIZON_DAYS = 30


def canonical_url(value: object) -> str:
    """Validate like any activity link, then reduce to `https://host/path` (no query).

    Raises ValueError with a short reason. Two links to the same page compare equal even if
    one has a trailing slash, an upper-case host, an explicit :443, or tracking parameters.
    """
    cleaned = offers.validate_url(value)
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    return f"https://{host}{path}"
