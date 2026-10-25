"""Deterministic matching engine (CONTRACT §6). P2a owns this file.

`generate_for_user` is called from INSIDE an already-open `db.immediate()` transaction
(`server/state.py`). It therefore uses plain `conn.execute()` only and NEVER issues
BEGIN/COMMIT: sqlite cannot nest write transactions, and a nested BEGIN would raise
there and abort the whole state write.

The payload's `wants` values (hangout|event) persist in `user_wants` (schema.sql):
`both_want_event` is true when BOTH users carry the `event` want — a real stored
signal, not a tag heuristic. The reasons key stays `both_want_event`, and the
score formula is 3*shared_tags + 2*overlap_hours + 1*both_want_event, plus 4 for each public
event link both students sent (user_events). `busy_load` is stored for the ranker but this
rule baseline deliberately ignores it, so a learned model can be compared against it.
"""
import collections
import datetime
import json

from server import db, logging as smlog, wording

log = smlog.get("match")

# A pair only becomes a candidate when an intersection segment is at least this long.
MIN_OVERLAP_SECONDS = 30 * 60
# Points for each public event link both students sent (more than a shared interest's 3).
SHARED_EVENT_WEIGHT = 4



def _ts(value: str) -> datetime.datetime:
    """UTC string 'YYYY-MM-DDTHH:MM:SSZ' → aware datetime (lexicographic compare is chronological)."""
    return datetime.datetime.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.UTC
    )


def _windows(conn, user_id: int):
    return conn.execute(
        "SELECT starts_at, ends_at, tz FROM availability_windows WHERE user_id=? ORDER BY starts_at",
        (user_id,),
    ).fetchall()


def _tags(conn, user_id: int) -> set[str]:
    return {
        row["tag"]
        for row in conn.execute("SELECT tag FROM hobby_tags WHERE user_id=?", (user_id,)).fetchall()
    }


def _wants(conn, user_id: int) -> set[str]:
    return {
        row["want"]
        for row in conn.execute("SELECT want FROM user_wants WHERE user_id=?", (user_id,)).fetchall()
    }


def _events(conn, user_id: int, now: str) -> dict[str, str]:
    """{canonical public link: category} for events that have not ended yet."""
    return {
        row["url"]: row["category"]
        for row in conn.execute(
            "SELECT url, category FROM user_events WHERE user_id=? AND ends_at > ?",
            (user_id, now),
        ).fetchall()
    }


def _shared_events(mine: dict[str, str], theirs: dict[str, str]) -> list[dict]:
    """Links both students sent. Only this intersection is ever shown; categories that
    disagree fall back to 'other' rather than letting one side's label win."""
    return [
        {"url": url, "category": mine[url] if mine[url] == theirs[url] else "other"}
        for url in sorted(mine.keys() & theirs.keys())
    ]


def refresh_events(conn, user_id: int) -> int:
    """Re-derive the event part of every OPEN card this user is on.

    A card is created the first time two students overlap, which may be before the second
    student sends their events. Without this, an event added later would never count. Only the
    `shared_events` reason and its points change; tags, overlap and the proposal are left
    exactly as they were. Runs inside the caller's transaction. Returns cards changed.
    """
    now = db.utcnow()
    mine = _events(conn, user_id, now)
    changed = 0
    rows = conn.execute(
        "SELECT id, user_a, user_b, score, reasons_json FROM candidates"
        " WHERE (user_a=? OR user_b=?) AND state='open' AND expires_at > ?",
        (user_id, user_id, now),
    ).fetchall()
    for row in rows:
        other = row["user_b"] if row["user_a"] == user_id else row["user_a"]
        try:
            reasons = json.loads(row["reasons_json"] or "{}")
        except ValueError:
            continue
        old = reasons.get("shared_events", [])
        new = _shared_events(mine, _events(conn, other, now))
        if new == old:
            continue
        if new:
            reasons["shared_events"] = new
        else:
            reasons.pop("shared_events", None)
        score = row["score"] + SHARED_EVENT_WEIGHT * (len(new) - len(old))
        conn.execute(
            "UPDATE candidates SET score=?, reasons_json=? WHERE id=?",
            (score, json.dumps(reasons), row["id"]),
        )
        changed += 1
    return changed


def _longest_overlap(mine, theirs):
    """Longest >=30min intersection across every pair of window rows.

    Returns (start_utc, end_utc, seconds) or None. Tie on duration → earliest start.
    """
    best = None
    for a in mine:
        for b in theirs:
            start = max(a["starts_at"], b["starts_at"])
            end = min(a["ends_at"], b["ends_at"])
            if end <= start:
                continue
            seconds = (_ts(end) - _ts(start)).total_seconds()
            if seconds < MIN_OVERLAP_SECONDS:
                continue
            if best is None or seconds > best[2] or (seconds == best[2] and start < best[0]):
                best = (start, end, seconds)
    return best


def _pair_has_candidate(conn, user_a: int, user_b: int) -> bool:
    """Any non-void/hidden candidate suppresses regeneration — but an OPEN candidate only
    while still live (read-time expiry): a stale open row is sweep material, not armour.
    Reviewer P2: expired-open suppression blocked fresh matching before the sweep ran."""
    row = conn.execute(
        "SELECT 1 FROM candidates WHERE user_a=? AND user_b=?"
        " AND state NOT IN ('voided','hidden')"
        " AND (state != 'open' OR expires_at > ?)"
        " LIMIT 1",
        (user_a, user_b, db.utcnow()),
    ).fetchone()
    return row is not None


def _pair_swiped(conn, user_a: int, user_b: int) -> bool:
    """Swipes attach to a candidate; a swipe for the pair is a swipe on any candidate of the pair."""
    row = conn.execute(
        "SELECT 1 FROM swipes s JOIN candidates c ON c.id = s.candidate_id"
        " WHERE c.user_a=? AND c.user_b=? LIMIT 1",
        (user_a, user_b),
    ).fetchone()
    return row is not None


def generate_for_user(conn, user_id: int) -> list[int]:
    """Generate candidates for this user against everyone else. Returns new candidate ids.

    EXTENSIVE LOGGING (see server/logging.py): every pass writes one `sm.match` summary line
    naming how many other users existed and how many were refused by each gate. That line is
    the answer to "why do I see no matches": silence used to be indistinguishable between
    "nobody else is available", "you never granted matching", and "no overlapping free time".
    Per-pair detail goes to DEBUG; content (windows, tags) is never logged.
    """
    now = db.utcnow()
    skips: collections.Counter[str] = collections.Counter()

    mine = _windows(conn, user_id)
    if not mine:
        log.info("user=%s pass=aborted reason=no_windows_of_my_own", user_id)
        return []

    my_avail = conn.execute(
        "SELECT expires_at FROM availability WHERE user_id=?", (user_id,)
    ).fetchone()
    if my_avail is None or my_avail["expires_at"] <= now:
        log.info(
            "user=%s pass=aborted reason=availability_expired_or_missing expires_at=%s now=%s",
            user_id,
            (my_avail["expires_at"] if my_avail else None),
            now,
        )
        return []

    # Display rule for our payloads: the REQUESTER's earliest window tz (first row by starts_at).
    display_tz = mine[0]["tz"]
    my_tags = _tags(conn, user_id)
    my_wants = _wants(conn, user_id)
    my_events = _events(conn, user_id, now)

    others = conn.execute(
        "SELECT user_id, expires_at FROM availability WHERE user_id != ? AND expires_at > ?"
        " ORDER BY user_id",
        (user_id, now),
    ).fetchall()

    def skip(reason: str, pair_other: int) -> None:
        skips[reason] += 1
        log.debug("user=%s other=%s skip=%s", user_id, pair_other, reason)

    new_ids: list[int] = []
    for other in others:
        other_id = other["user_id"]
        if other_id == user_id:
            continue
        user_a, user_b = (user_id, other_id) if user_id < other_id else (other_id, user_id)

        if _pair_has_candidate(conn, user_a, user_b):
            skip("pair_already_has_candidate", other_id)
            continue
        if _pair_swiped(conn, user_a, user_b):
            skip("pair_already_swiped", other_id)
            continue
        if wording.blocked_pair(conn, user_a, user_b):
            skip("pair_blocked", other_id)
            continue
        if not wording.matching_live(conn, user_a, user_b):
            # The single most common cause of "I see no matches": both students need a LIVE
            # matching grant. Chat writes are grant-preserving by design, so a student who has
            # only ever talked to the assistant has never granted matching.
            mine_live = wording.live_grant(conn, user_id, "matching") is not None
            theirs_live = wording.live_grant(conn, other_id, "matching") is not None
            skip(
                "matching_grant_missing_me" if not mine_live else "matching_grant_missing_them",
                other_id,
            )
            continue

        # FLOW §2 authorization table: a card — and its candidate notification — about X
        # requires X's live publication grant. Without a card grant there is no card at all;
        # a bare card grant demotes to pseudonym, it never blocks (reviewer P1 #2).
        if wording.live_grant(conn, user_id, "publication", "card") is None:
            skip("publication_card_missing_me", other_id)
            continue
        if wording.live_grant(conn, other_id, "publication", "card") is None:
            skip("publication_card_missing_them", other_id)
            continue

        their_windows = _windows(conn, other_id)
        if not their_windows:
            skip("other_has_no_windows", other_id)
            continue

        overlap = _longest_overlap(mine, their_windows)
        if overlap is None:
            skip("no_overlapping_free_time", other_id)
            continue
        seg_start, seg_end, seg_seconds = overlap

        their_tags = _tags(conn, other_id)
        their_wants = _wants(conn, other_id)
        shared = sorted(my_tags & their_tags)
        overlap_hours = int(round(seg_seconds / 3600))
        both_want_event = "event" in my_wants and "event" in their_wants
        # A link both students sent is the same public event: the strongest single signal.
        # Only the intersection is ever shown; either side's other events stay private.
        their_events = _events(conn, other_id, now)
        shared_events = _shared_events(my_events, their_events)
        score = (
            3 * len(shared)
            + 2 * overlap_hours
            + (1 if both_want_event else 0)
            + SHARED_EVENT_WEIGHT * len(shared_events)
        )

        expires_at = min(my_avail["expires_at"], other["expires_at"])
        reasons = {
            "shared_tags": shared,
            "overlap_hours": overlap_hours,
            "both_want_event": both_want_event,
        }
        if shared_events:
            reasons["shared_events"] = shared_events
        conn.execute(
            "INSERT INTO candidates (user_a, user_b, score, reasons_json, created_at, expires_at, state)"
            " VALUES (?,?,?,?,?,?,'open')",
            (user_a, user_b, score, json.dumps(reasons), now, expires_at),
        )
        candidate_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO proposals (candidate_id, window_start, window_end, version, state, expires_at)"
            " VALUES (?,?,?,1,'proposed',?)",
            (candidate_id, seg_start, seg_end, expires_at),
        )

        # Inbox to BOTH users; `about` is always the OTHER person, so labels obey THEIR grants.
        db.push_inbox(
            conn,
            user_id,
            "candidate",
            wording.candidate_text(conn, other_id, seg_start, seg_end, display_tz, candidate_id),
        )
        db.push_inbox(
            conn,
            other_id,
            "candidate",
            wording.candidate_text(conn, user_id, seg_start, seg_end, display_tz, candidate_id),
        )

        new_ids.append(candidate_id)
        log.info(
            "user=%s other=%s candidate=%s score=%s overlap_hours=%s shared_tags=%s",
            user_id, other_id, candidate_id, score, overlap_hours, len(shared),
        )

    # One line per pass, always. This is the line to grep when a student says "no matches".
    if skips:
        log.info(
            "user=%s pass=done others=%s new=%s skip=%s",
            user_id,
            len(others),
            len(new_ids),
            dict(skips.most_common()),
        )
    else:
        log.info(
            "user=%s pass=done others=%s new=%s skip=none",
            user_id,
            len(others),
            len(new_ids),
        )
    if not others:
        log.info(
            "user=%s nobody_else_is_available reason=no_other_user_has_a_live_availability_row",
            user_id,
        )
    return new_ids
