"""Direct-SQL seed helpers so every phase can test its own seam without HTTP or siblings.
FROZEN (CONTRACT §13). Times are local wall strings + tz, converted to UTC on insert."""
import datetime
import json
import secrets
from zoneinfo import ZoneInfo

from server import config, db, sessions


def conn():
    return db.connect()


def to_utc(local_s: str, tz: str) -> str:
    """'2026-10-29T19:00:00' + 'America/Los_Angeles' → '...Z' UTC iso string."""
    dt = datetime.datetime.strptime(local_s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mk_user(email: str, name: str, avatar_id: int = 1, photo: bool = True) -> int:
    """Seed a user. A photo is MANDATORY for matching (owner's call), so the default includes
    one; pass photo=False to test the unmatchable state."""
    c = conn()
    try:
        c.execute(
            "INSERT INTO users (email, password_hash, display_name, preset_avatar_id,"
            " adult_attested_at, created_at, photo_path)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                email, None, name, avatar_id, db.utcnow(), db.utcnow(),
                f"{email.split('@')[0]}.jpg" if photo else None,
            ),
        )
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        c.close()


def grant(uid: int, kind: str = "matching", tier: str | None = None) -> None:
    c = conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO grants (user_id, kind, tier, version, fields_json, granted_at, expires_at)"
                " VALUES (?,?,?,1,?,?,?)",
                (
                    uid,
                    kind,
                    tier,
                    json.dumps({"audience": "matched_users_only"}),
                    db.utcnow(),
                    db.utcnow(days=config.grant_ttl_days()),
                ),
            )
    finally:
        c.close()


def pub_name(uid: int) -> None:
    grant(uid, "publication", "card+name")


def pub_card(uid: int) -> None:
    grant(uid, "publication", "card")


def windows(uid: int, pairs: list, tz: str = "America/Los_Angeles",
            source: str = "student", agent_id: str | None = None,
            expires_days: float | None = None) -> None:
    """pairs = [(local_start, local_end), ...]. Replaces existing windows for the user."""
    exp_days = config.state_ttl_days() if expires_days is None else expires_days
    c = conn()
    try:
        with db.immediate(c):
            c.execute("DELETE FROM availability_windows WHERE user_id=?", (uid,))
            c.execute(
                "INSERT OR REPLACE INTO availability (user_id, written_by_agent_id, payload_hash,"
                " observed_at, expires_at, source) VALUES (?,?,?,?,?,?)",
                (uid, agent_id, secrets.token_hex(8), db.utcnow(),
                 db.utcnow(days=exp_days), source),
            )
            for s, e in pairs:
                c.execute(
                    "INSERT INTO availability_windows (user_id, starts_at, ends_at, tz)"
                    " VALUES (?,?,?,?)",
                    (uid, to_utc(s, tz), to_utc(e, tz), tz),
                )
    finally:
        c.close()


def tags(uid: int, taglist: list) -> None:
    c = conn()
    try:
        with db.immediate(c):
            c.execute("DELETE FROM hobby_tags WHERE user_id=?", (uid,))
            for t in taglist:
                c.execute("INSERT INTO hobby_tags (user_id, tag) VALUES (?,?)", (uid, t))
    finally:
        c.close()



def wants(uid: int, wantlist: list) -> None:
    c = conn()
    try:
        with db.immediate(c):
            c.execute("DELETE FROM user_wants WHERE user_id=?", (uid,))
            for w in wantlist:
                c.execute("INSERT INTO user_wants (user_id, want) VALUES (?,?)", (uid, w))
    finally:
        c.close()

def mk_candidate(a: int, b: int, window: tuple = ("2026-10-29T19:00:00", "2026-10-29T21:00:00"),
                 tz: str = "America/Los_Angeles", score: int = 8, reasons: dict | None = None,
                 tags_list: tuple = ("climbing",), grant_names: bool = True):
    """Full pair setup: matching + card grants, tags, windows on both sides, open candidate +
    proposal v1. Returns (candidate_id, proposal_id)."""
    ua, ub = min(a, b), max(a, b)
    if grant_names:
        grant(ua, "matching")
        grant(ub, "matching")
        pub_card(ua)
        pub_card(ub)
    tags(ua, list(tags_list))
    tags(ub, list(tags_list))
    windows(ua, [window], tz=tz)
    windows(ub, [window], tz=tz)
    c = conn()
    try:
        exp = db.utcnow(days=config.grant_ttl_days())
        with db.immediate(c):
            r = json.dumps(reasons or {"shared_tags": ["climbing"],
                                       "overlap_hours": 2, "both_want_event": False})
            c.execute(
                "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at,"
                " expires_at, state) VALUES (?,?,?,?,?,?,'open')",
                (ua, ub, score, r, db.utcnow(), exp),
            )
            cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "INSERT INTO proposals (candidate_id, window_start, window_end, version,"
                " state, expires_at) VALUES (?,?,?,1,'proposed',?)",
                (cid, to_utc(window[0], tz), to_utc(window[1], tz), exp),
            )
            pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        return cid, pid
    finally:
        c.close()


def mint_pairing(uid: int, label: str = "test-agent"):
    """Direct pairings row + fresh token, bypassing the HTTP pairing flow."""
    token = secrets.token_urlsafe(24)
    agent_id = "ag_" + secrets.token_urlsafe(6)
    c = conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO pairings (user_id, agent_id, token_hash, prev_token_hash,"
                " prev_rotate_expires, label, created_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, agent_id, db.hash_token(token), None, None, label,
                 db.utcnow(), db.utcnow()),
            )
    finally:
        c.close()
    return token, agent_id


def auth_header(token: str):
    return {"Authorization": f"Bearer {token}"}


def direct_session(client, uid: int):
    """Set a signed session cookie without the web login flow."""
    client.cookies.set("smsession", sessions.sign_session(uid))
    return client
