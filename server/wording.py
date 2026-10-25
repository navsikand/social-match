"""Wording + grant-read seam. FROZEN (CONTRACT §7/§9): every message and card label goes through here,
so name-vs-pseudonym obeys the publication grants at the moment of use."""
import datetime
import hashlib
from zoneinfo import ZoneInfo

from server import config, db

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def live_grant(conn, user_id: int, kind: str, tier: str | None = None):
    """Newest non-withdrawn, unexpired grant row of this kind (tier match required when given)."""
    now = db.utcnow()
    if tier is None:
        return db.one(
            conn,
            "SELECT * FROM grants WHERE user_id=? AND kind=? AND withdrawn_at IS NULL"
            " AND expires_at > ? ORDER BY version DESC LIMIT 1",
            (user_id, kind, now),
        )
    # tier='card+name' subsumes 'card' at the card level; name check is exact.
    if tier == "card":
        return db.one(
            conn,
            "SELECT * FROM grants WHERE user_id=? AND kind='publication' AND tier IN ('card','card+name')"
            " AND withdrawn_at IS NULL AND expires_at > ? ORDER BY version DESC LIMIT 1",
            (user_id, now),
        )
    return db.one(
        conn,
        "SELECT * FROM grants WHERE user_id=? AND kind='publication' AND tier=?"
        " AND withdrawn_at IS NULL AND expires_at > ? ORDER BY version DESC LIMIT 1",
        (user_id, tier, now),
    )


def matching_live(conn, user_a: int, user_b: int) -> bool:
    return (
        live_grant(conn, user_a, "matching") is not None
        and live_grant(conn, user_b, "matching") is not None
    )


def blocked_pair(conn, user_a: int, user_b: int) -> bool:
    now = db.utcnow()
    ah, bh = db.user_hash(user_a), db.user_hash(user_b)
    return (
        db.one(
            conn,
            "SELECT 1 FROM blocks WHERE ((user_hash=? AND blocked_user_hash=?)"
            " OR (user_hash=? AND blocked_user_hash=?)) AND expires_at > ?",
            (ah, bh, bh, ah, now),
        )
        is not None
    )


def pseudonym_for(conn, user_id: int) -> str:
    tag_row = db.one(
        conn, "SELECT tag FROM hobby_tags WHERE user_id=? ORDER BY tag LIMIT 1", (user_id,)
    )
    tag = tag_row["tag"] if tag_row else "student"
    short = hashlib.sha256(f"pseudo:{user_id}".encode()).hexdigest()[:4]
    return f"{tag}-{short}"


def name_visible(conn, about_uid: int) -> bool:
    return live_grant(conn, about_uid, "publication", "card+name") is not None


def label(conn, about_uid: int) -> str:
    """Display name if it may be shown; otherwise the pseudonym. Checked at the moment of use."""
    if name_visible(conn, about_uid):
        row = db.one(conn, "SELECT display_name FROM users WHERE id=?", (about_uid,))
        if row is not None:
            return row["display_name"]
    return pseudonym_for(conn, about_uid)


def fmt_window(start_utc: str, end_utc: str, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = datetime.UTC
    s = datetime.datetime.strptime(start_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.UTC).astimezone(zone)
    e = datetime.datetime.strptime(end_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.UTC).astimezone(zone)
    return f"{_WEEKDAYS[s.weekday()]} {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"


def card_url(candidate_id: int) -> str:
    return f"{config.base_url()}/m/{candidate_id}"


# ---- exact notification texts (CONTRACT §9) -----------------------------------

def candidate_text(conn, about_uid: int, start_utc: str, end_utc: str, tz: str, cid: int) -> dict:
    w = fmt_window(start_utc, end_utc, tz)
    url = card_url(cid)
    return {
        "kind": "candidate",
        "candidate_id": cid,
        "about": label(conn, about_uid),
        "window": w,
        "card_url": url,
        "message": f"{label(conn, about_uid)} is up for {w}. See the card: {url}",
    }


def confirmation_text(conn, about_uid: int, start_utc: str, end_utc: str, tz: str, cid: int) -> dict:
    w = fmt_window(start_utc, end_utc, tz)
    url = card_url(cid)
    return {
        "kind": "confirmation",
        "candidate_id": cid,
        "about": label(conn, about_uid),
        "window": w,
        "card_url": url,
        "message": f"{label(conn, about_uid)} confirmed {w} — confirm your side: {url}",
    }


def match_text(conn, about_uid: int, start_utc: str, end_utc: str, tz: str) -> dict:
    w = fmt_window(start_utc, end_utc, tz)
    return {
        "kind": "match",
        "about": label(conn, about_uid),
        "window": w,
        "message": f"It's on: {w} with {label(conn, about_uid)}.",
    }


def revocation_text(start_utc: str, end_utc: str, tz: str) -> dict:
    w = fmt_window(start_utc, end_utc, tz)
    return {"kind": "revocation", "window": w, "message": f"The plan for {w} was withdrawn."}


def counter_text(conn, about_uid: int, start_utc: str, end_utc: str, tz: str, cid: int) -> dict:
    w = fmt_window(start_utc, end_utc, tz)
    url = card_url(cid)
    return {
        "kind": "counter",
        "candidate_id": cid,
        "about": label(conn, about_uid),
        "window": w,
        "card_url": url,
        "message": f"{label(conn, about_uid)} suggested {w} instead. See the card: {url}",
    }
