"""Cards, swipes, proposals, concurrency. P2b owns this file (CONTRACT §8).

Design note: every transactional behaviour is a plain module function taking `(conn, uid, ...)`
so the race tests can call it on their own `db.connect()` connections from real threads and a
`threading.Barrier` — exactly the interleaving production sees. The FastAPI endpoints below are
thin: session auth + one function call + error shape.

`right` is a *version-bound confirmation*, never a stored preference: the proposal version the
client swiped on must still be the current non-void proposal at commit time. Check order in the
confirm transaction is fixed by CONTRACT §8 and any deviation is called out inline.
"""
from __future__ import annotations

import datetime
import json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import config, db, sessions, wording

router = APIRouter(prefix="/api", tags=["scheduling"])

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
DIRECTIONS = ("right", "left", "block", "report")
CONFIRMED_STATES = ("a_confirmed", "both_confirmed", "closed")
OPEN_CANDIDATE_STATES = ("open",)


class SchedulingError(Exception):
    """Domain error raised by the plain functions; endpoints translate it to the §2 JSON shape."""

    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------ small helpers


def _to_utc(local_s: str, tz: str) -> str:
    """'2026-10-29T19:00' in tz → UTC string (CONTRACT §2)."""
    try:
        dt = datetime.datetime.fromisoformat(str(local_s).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return dt.astimezone(datetime.UTC).strftime(_UTC_FMT)
    except (ValueError, TypeError, KeyError) as exc:
        raise SchedulingError(422, "payload_forbidden", f"bad window/tz: {exc}") from exc
    except Exception as exc:  # ZoneInfoNotFoundError is not a ValueError on every version
        raise SchedulingError(422, "payload_forbidden", f"bad tz: {exc}") from exc


def candidate_row(conn, uid: int, candidate_id: int):
    """The candidate, but only if `uid` is one of its two users (else 404 — no probing)."""
    row = db.one(conn, "SELECT * FROM candidates WHERE id=?", (candidate_id,))
    if row is None or uid not in (row["user_a"], row["user_b"]):
        raise SchedulingError(404, "not_found", "no such candidate")
    return row


def counterpart(cand, uid: int) -> int:
    return cand["user_b"] if uid == cand["user_a"] else cand["user_a"]


def current_proposal(conn, candidate_id: int):
    """Newest non-void proposal — the only one a swipe may bind to."""
    return db.one(
        conn,
        "SELECT * FROM proposals WHERE candidate_id=? AND state != 'voided'"
        " ORDER BY version DESC LIMIT 1",
        (candidate_id,),
    )


def latest_proposal(conn, candidate_id: int):
    return db.one(
        conn,
        "SELECT * FROM proposals WHERE candidate_id=? ORDER BY version DESC LIMIT 1",
        (candidate_id,),
    )


def display_tz(conn, cand) -> str:
    """Display tz rule (CONTRACT §8): user_a's earliest window row tz, else user_b's."""
    for uid in (cand["user_a"], cand["user_b"]):
        row = db.one(
            conn,
            "SELECT tz FROM availability_windows WHERE user_id=? ORDER BY starts_at LIMIT 1",
            (uid,),
        )
        if row is not None:
            return row["tz"]
    return "UTC"


def availability_live(conn, uid: int, now: str):
    return db.one(conn, "SELECT * FROM availability WHERE user_id=? AND expires_at > ?", (uid, now))


def covering_window(conn, uid: int, start: str, end: str, now: str):
    """Read-time expiry on both the availability row and the containing window."""
    if availability_live(conn, uid, now) is None:
        return None
    return db.one(
        conn,
        "SELECT * FROM availability_windows WHERE user_id=? AND starts_at <= ? AND ends_at >= ?"
        " ORDER BY starts_at LIMIT 1",
        (uid, start, end),
    )


def _active_candidate(cand, now: str, *, allow_matched: bool = False):
    """404 unless the candidate may still be acted on."""
    if cand["expires_at"] <= now:
        raise SchedulingError(404, "expired", "this match expired")
    allowed = ("open", "matched") if allow_matched else OPEN_CANDIDATE_STATES
    if cand["state"] not in allowed:
        raise SchedulingError(404, "not_found", "this card is no longer available")


def _assert_not_swiped(conn, uid: int, candidate_id: int):
    row = db.one(conn, "SELECT direction FROM swipes WHERE user_id=? AND candidate_id=?", (uid, candidate_id))
    if row is not None:
        raise SchedulingError(409, "already_swiped", "you already answered this card")


def _record_swipe(conn, uid: int, candidate_id: int, direction: str, now: str) -> None:
    conn.execute(
        "INSERT INTO swipes (user_id, candidate_id, direction, created_at) VALUES (?,?,?,?)",
        (uid, candidate_id, direction, now),
    )


def _user_alive(conn, uid: int) -> bool:
    return db.one(conn, "SELECT 1 FROM users WHERE id=? AND deleted_at IS NULL", (uid,)) is not None


def _push(conn, user_id: int, payload: dict) -> None:
    db.push_inbox(conn, user_id, payload["kind"], payload)


def _log_consent(conn, uid: int, action: str, scope: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (db.user_hash(uid), action, scope, json.dumps(detail), db.utcnow()),
    )


# ------------------------------------------------------------------ dashboard


def dashboard(conn, uid: int) -> dict:
    now = db.utcnow()
    rows = db.q(
        conn,
        "SELECT * FROM candidates WHERE (user_a=? OR user_b=?) AND state='open' AND expires_at > ?"
        " ORDER BY id DESC",
        (uid, uid, now),
    )
    return {
        "candidates": [
            {
                "candidate_id": row["id"],
                "card_url": wording.card_url(row["id"]),
                "state": row["state"],
            }
            for row in rows
        ]
    }


# ------------------------------------------------------------------ card


def card(conn, uid: int, candidate_id: int) -> dict:
    """Serve-time authorization (§7): never cached, every grant re-read now."""
    now = db.utcnow()
    cand = candidate_row(conn, uid, candidate_id)
    about_uid = counterpart(cand, uid)
    prop = current_proposal(conn, candidate_id)
    if cand["expires_at"] <= now or prop is None:
        raise SchedulingError(404, "expired", "this match expired")
    if cand["state"] not in ("open", "matched"):
        raise SchedulingError(404, "not_found", "this card is no longer available")
    if wording.blocked_pair(conn, cand["user_a"], cand["user_b"]):
        raise SchedulingError(404, "not_found", "this card is no longer available")
    if not wording.matching_live(conn, cand["user_a"], cand["user_b"]):
        raise SchedulingError(404, "not_found", "this card is no longer available")
    if wording.live_grant(conn, about_uid, "publication", "card") is None:
        raise SchedulingError(404, "not_found", "this card is no longer available")
    if availability_live(conn, cand["user_a"], now) is None or availability_live(
        conn, cand["user_b"], now
    ) is None:
        raise SchedulingError(404, "expired", "availability expired")

    tz = display_tz(conn, cand)
    about = db.one(conn, "SELECT * FROM users WHERE id=?", (about_uid,))
    tags = [r["tag"] for r in db.q(conn, "SELECT tag FROM hobby_tags WHERE user_id=? ORDER BY tag", (about_uid,))]
    return {
        "candidate_id": cand["id"],
        "proposal_id": prop["id"],
        "proposal_version": prop["version"],
        "about": {
            "pseudonym": wording.pseudonym_for(conn, about_uid),
            "display_name": about["display_name"] if wording.name_visible(conn, about_uid) else None,
            "preset_avatar_id": about["preset_avatar_id"],
            "hobby_tags": tags,
        },
        "window": {
            "start_utc": prop["window_start"],
            "end_utc": prop["window_end"],
            "tz": tz,
            "display": wording.fmt_window(prop["window_start"], prop["window_end"], tz),
        },
        "reasons": _loads(cand["reasons_json"]),
        "card_url": wording.card_url(cand["id"]),
        "state": cand["state"],
    }


def _loads(raw):
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


# ------------------------------------------------------------------ confirm (right)


def confirm(conn, uid: int, candidate_id: int, proposal_version: int) -> dict:
    """Version-bound confirmation. ONE `immediate` txn, check order fixed by CONTRACT §8."""
    with db.immediate(conn):
        now = db.utcnow()
        cand = candidate_row(conn, uid, candidate_id)
        other = counterpart(cand, uid)

        # Idempotency window: a duplicate confirmation on an already-matched pair is a 200, so
        # 'matched' is allowed ONLY when this user's confirmation row already exists.
        prop = current_proposal(conn, candidate_id)
        already_confirmed = (
            prop is not None
            and db.one(
                conn,
                "SELECT 1 FROM proposal_confirmations WHERE proposal_id=? AND user_id=?",
                (prop["id"], uid),
            )
            is not None
        )
        _active_candidate(cand, now, allow_matched=already_confirmed)
        if prop is None:
            raise SchedulingError(404, "not_found", "no open proposal")

        # (1) revoke intent since the proposal came into existence → 409 intent_present.
        # NOTE: proposals has no created_at column (schema is frozen), so the proposal's epoch is
        # the candidate's created_at — for sub-30m retention windows this never contradicts the
        # intent-before-contend semantics of §8 and it is conservative (blocks more, never less).
        if db.one(
            conn,
            "SELECT 1 FROM revoke_intents WHERE created_at >= ? AND user_id IN (?,?) LIMIT 1",
            (cand["created_at"], cand["user_a"], cand["user_b"]),
        ):
            raise SchedulingError(409, "intent_present", "one side withdrew this match")

        # (2) both matching grants live.
        if not wording.matching_live(conn, cand["user_a"], cand["user_b"]):
            raise SchedulingError(404, "not_found", "not matched any more")

        # (3) counterpart publication grant (card serving). Evaluated for the record but
        # intentionally NOT blocking: a withdrawn card grant suppresses future cards, it does not
        # invalidate a confirmation that was already formed. (§8)
        _ = wording.live_grant(conn, other, "publication", "card")

        # (4) not blocked either direction.
        if wording.blocked_pair(conn, cand["user_a"], cand["user_b"]):
            raise SchedulingError(404, "not_found", "this card is no longer available")

        # (5) version binding.
        if prop["version"] != proposal_version:
            raise SchedulingError(409, "stale_version", "this card changed — reload it")

        # (6) both availabilities live and still covering the whole proposal window (read-time).
        if availability_live(conn, cand["user_a"], now) is None or availability_live(
            conn, cand["user_b"], now
        ) is None:
            raise SchedulingError(404, "expired", "availability expired")
        if covering_window(conn, cand["user_a"], prop["window_start"], prop["window_end"], now) is None or (
            covering_window(conn, cand["user_b"], prop["window_start"], prop["window_end"], now) is None
        ):
            raise SchedulingError(409, "slot_conflict", "that window no longer fits your availability")

        # (7) no overlap with another proposal this user already committed to.
        clash = db.one(
            conn,
            "SELECT p.id FROM proposal_confirmations pc JOIN proposals p ON p.id = pc.proposal_id"
            " WHERE pc.user_id=? AND pc.proposal_id != ? AND p.state IN (?,?,?)"
            " AND p.window_start < ? AND p.window_end > ? LIMIT 1",
            (uid, prop["id"], *CONFIRMED_STATES, prop["window_end"], prop["window_start"]),
        )
        if clash is not None:
            raise SchedulingError(409, "slot_conflict", "you already have a plan in that window")

        # (8) idempotent confirmation + the swipe row.
        existing_swipe = db.one(
            conn, "SELECT direction FROM swipes WHERE user_id=? AND candidate_id=?", (uid, candidate_id)
        )
        if existing_swipe is not None and existing_swipe["direction"] != "right":
            raise SchedulingError(409, "already_swiped", "you already answered this card")
        if existing_swipe is None:
            _record_swipe(conn, uid, candidate_id, "right", now)
        cur = conn.execute(
            "INSERT OR IGNORE INTO proposal_confirmations (proposal_id, user_id, confirmed_at)"
            " VALUES (?,?,?)",
            (prop["id"], uid, now),
        )
        inserted = cur.rowcount > 0
        total = db.one(
            conn, "SELECT COUNT(*) AS n FROM proposal_confirmations WHERE proposal_id=?", (prop["id"],)
        )["n"]

        tz = display_tz(conn, cand)
        start, end = prop["window_start"], prop["window_end"]

        if inserted and total == 1:
            conn.execute(
                "UPDATE proposals SET state='a_confirmed' WHERE id=? AND state='proposed'", (prop["id"],)
            )
            _push(conn, other, wording.confirmation_text(conn, uid, start, end, tz, cand["id"]))
        elif inserted and total >= 2:
            conn.execute(
                "UPDATE proposals SET state='both_confirmed' WHERE id=? AND state IN ('proposed','a_confirmed')",
                (prop["id"],),
            )
            conn.execute("UPDATE candidates SET state='matched' WHERE id=?", (cand["id"],))
            # `about` is the OTHER user per recipient (§9).
            _push(conn, uid, wording.match_text(conn, other, start, end, tz))
            _push(conn, other, wording.match_text(conn, uid, start, end, tz))

        state = db.one(conn, "SELECT state FROM proposals WHERE id=?", (prop["id"],))["state"]
        return {"state": state, "proposal_id": prop["id"], "proposal_version": prop["version"]}


# ------------------------------------------------------------------ swipe dispatch


def swipe(conn, uid: int, candidate_id: int, proposal_version: int, direction: str) -> dict:
    if direction not in DIRECTIONS:
        raise SchedulingError(422, "payload_forbidden", "direction must be right|left|block|report")
    if direction == "right":
        return confirm(conn, uid, candidate_id, proposal_version)
    if direction == "left":
        return _decline(conn, uid, candidate_id)
    if direction == "block":
        return _block(conn, uid, candidate_id)
    return _report(conn, uid, candidate_id)


def _decline(conn, uid: int, candidate_id: int) -> dict:
    with db.immediate(conn):
        now = db.utcnow()
        cand = candidate_row(conn, uid, candidate_id)
        _active_candidate(cand, now)
        _assert_not_swiped(conn, uid, candidate_id)
        _record_swipe(conn, uid, candidate_id, "left", now)
        conn.execute(
            "UPDATE candidates SET state='declined', closed_at=? WHERE id=? AND state='open'",
            (now, cand["id"]),
        )
        return {"state": "declined"}


def _block(conn, uid: int, candidate_id: int) -> dict:
    with db.immediate(conn):
        now = db.utcnow()
        cand = candidate_row(conn, uid, candidate_id)
        other = counterpart(cand, uid)
        _active_candidate(cand, now)
        _assert_not_swiped(conn, uid, candidate_id)
        expires = db.utcnow(days=config.blocks_retention_months() * 30)
        uh, oh = db.user_hash(uid), db.user_hash(other)
        for a_hash, b_hash in ((uh, oh), (oh, uh)):
            conn.execute(
                "INSERT OR REPLACE INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (a_hash, b_hash, now, expires),
            )
        _record_swipe(conn, uid, candidate_id, "block", now)
        conn.execute(
            "UPDATE candidates SET state='voided', closed_at=? WHERE id=?", (now, cand["id"])
        )
        prop = latest_proposal(conn, cand["id"])
        if prop is not None and _user_alive(conn, other):
            tz = display_tz(conn, cand)
            _push(conn, other, wording.revocation_text(prop["window_start"], prop["window_end"], tz))
        return {"state": "voided"}


def _report(conn, uid: int, candidate_id: int) -> dict:
    with db.immediate(conn):
        now = db.utcnow()
        cand = candidate_row(conn, uid, candidate_id)
        _active_candidate(cand, now)
        _assert_not_swiped(conn, uid, candidate_id)
        _record_swipe(conn, uid, candidate_id, "report", now)
        # report ≠ block: no blocks row (moderation may act later), no inbox.
        conn.execute(
            "INSERT INTO reports (reporter_id, candidate_id, created_at, state) VALUES (?,?,?,'open')",
            (uid, cand["id"], now),
        )
        conn.execute(
            "UPDATE candidates SET state='hidden', closed_at=? WHERE id=?", (now, cand["id"])
        )
        return {"state": "hidden"}


# ------------------------------------------------------------------ counter


def counter(conn, uid: int, candidate_id: int, window_start: str, window_end: str, tz: str) -> dict:
    start = _to_utc(window_start, tz)
    end = _to_utc(window_end, tz)
    if start >= end:
        raise SchedulingError(422, "payload_forbidden", "window must start before it ends")

    with db.immediate(conn):
        now = db.utcnow()
        cand = candidate_row(conn, uid, candidate_id)
        other = counterpart(cand, uid)
        _active_candidate(cand, now)

        # Reviewer P1 #6: a counter is a fresh notification about the countering student —
        # matching grants on both sides, the pair unblocked, and the countering student's
        # own card grant must be live (a withdrawn card means no new cards at all).
        if not wording.matching_live(conn, cand["user_a"], cand["user_b"]):
            raise SchedulingError(404, "not_found", "no longer available")
        if wording.blocked_pair(conn, cand["user_a"], cand["user_b"]):
            raise SchedulingError(404, "not_found", "no longer available")
        if wording.live_grant(conn, uid, "publication", "card") is None:
            raise SchedulingError(404, "not_found", "no longer available")
        old = current_proposal(conn, cand["id"])
        if old is None:
            raise SchedulingError(404, "not_found", "no open proposal")

        for who in (cand["user_a"], cand["user_b"]):
            if covering_window(conn, who, start, end, now) is None:
                raise SchedulingError(422, "payload_forbidden", "that window is not inside free time")

        conn.execute(
            "INSERT INTO proposals (candidate_id, window_start, window_end, version, state, expires_at)"
            " VALUES (?,?,?,?,'proposed',?)",
            (cand["id"], start, end, old["version"] + 1, cand["expires_at"]),
        )
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Confirmations live per proposal_id, so they simply do not carry to the new version.
        conn.execute(
            "UPDATE proposals SET state='voided', superseded_by=? WHERE id=?", (new_id, old["id"])
        )
        if _user_alive(conn, other):
            _push(conn, other, wording.counter_text(conn, uid, start, end, tz, cand["id"]))
        return {"state": "proposed", "proposal_id": new_id, "proposal_version": old["version"] + 1}


# ------------------------------------------------------------------ revoke


def revoke(conn, uid: int, candidate_id: int) -> dict:
    """Intent FIRST (autocommit, outside any txn) so a concurrent confirm always sees it."""
    cand = candidate_row(conn, uid, candidate_id)
    conn.execute(
        "INSERT INTO revoke_intents (user_id, created_at) VALUES (?,?)", (uid, db.utcnow())
    )
    with db.immediate(conn):
        cand = candidate_row(conn, uid, candidate_id)
        other = counterpart(cand, uid)
        prop = latest_proposal(conn, cand["id"])
        conn.execute(
            "UPDATE proposals SET state='voided' WHERE candidate_id=?"
            " AND state IN ('proposed','a_confirmed','both_confirmed')",
            (cand["id"],),
        )
        conn.execute(
            "UPDATE candidates SET state='voided', closed_at=? WHERE id=?",
            (db.utcnow(), cand["id"]),
        )
        if prop is not None:
            tz = display_tz(conn, cand)
            msg = wording.revocation_text(prop["window_start"], prop["window_end"], tz)
            for who in (uid, other):
                if _user_alive(conn, who):
                    _push(conn, who, msg)
        return {"state": "voided"}


# ------------------------------------------------------------------ leave matching


def _leave_cascade(conn, uid: int, source: str) -> dict:
    """Shared withdrawal cascade (reviewer P1 #3): withdrawing matching must also void OPEN
    and MATCHED plans, drop availability, and notify living counterparts. Runs inside the
    caller's transaction; logs one withdraw:matching row with the given source."""
    now = db.utcnow()
    conn.execute(
        "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='matching' AND withdrawn_at IS NULL",
        (now, uid),
    )
    conn.execute("DELETE FROM availability WHERE user_id=?", (uid,))  # windows cascade
    rows = db.q(
        conn,
        "SELECT * FROM candidates WHERE (user_a=? OR user_b=?) AND state IN ('open','matched')",
        (uid, uid),
    )
    for cand in rows:
        other = counterpart(cand, uid)
        prop = current_proposal(conn, cand["id"])
        conn.execute(
            "UPDATE proposals SET state='voided' WHERE candidate_id=?"
            " AND state IN ('proposed','a_confirmed','both_confirmed')",
            (cand["id"],),
        )
        conn.execute(
            "UPDATE candidates SET state='voided', closed_at=? WHERE id=?", (now, cand["id"])
        )
        if prop is not None and _user_alive(conn, other):
            tz = display_tz(conn, cand)
            _push(conn, other, wording.revocation_text(prop["window_start"], prop["window_end"], tz))
        if prop is not None and cand["state"] == "matched" and _user_alive(conn, uid):
            # A confirmed plan dies for BOTH sides — the withdrawing student is told too.
            tz = display_tz(conn, cand)
            _push(conn, uid, wording.revocation_text(prop["window_start"], prop["window_end"], tz))
    _log_consent(conn, uid, "withdraw:matching", "matching", {"source": source})
    return {"ok": True}


def leave_matching(conn, uid: int) -> dict:
    with db.immediate(conn):
        return _leave_cascade(conn, uid, "leave_matching")


# ------------------------------------------------------------------ delete account


def delete_account(conn, uid: int) -> dict:
    """One txn. Strip identifiers, drop derived rows, keep blocks + reports + hashed consent."""
    with db.immediate(conn):
        now = db.utcnow()
        purge_after = db.utcnow(days=config.audit_residue_days())
        rows = db.q(conn, "SELECT id FROM candidates WHERE user_a=? OR user_b=?", (uid, uid))
        conn.execute(
            # Reviewer P1 #1: a non-identifying UNIQUE tombstone, so the SECOND account
            # deletion doesn't collide on users.email='' and roll back its own erasure.
            "UPDATE users SET email=?, display_name='deleted user', password_hash='', deleted_at=?"
            " WHERE id=?",
            (f"deleted-{uid}@invalid", now, uid),
        )
        conn.execute("DELETE FROM availability WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM hobby_tags WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM interest_raw WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM user_wants WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM swipes WHERE user_id=?", (uid,))
        conn.execute(
            "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND withdrawn_at IS NULL", (now, uid)
        )
        conn.execute(
            "UPDATE pairings SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, uid)
        )
        for cand in rows:
            conn.execute(
                "UPDATE proposals SET state='voided' WHERE candidate_id=?"
                " AND state IN ('proposed','a_confirmed','both_confirmed')",
                (cand["id"],),
            )
            conn.execute("DELETE FROM proposal_confirmations WHERE proposal_id IN"
                         " (SELECT id FROM proposals WHERE candidate_id=?)", (cand["id"],))
            conn.execute(
                "UPDATE candidates SET state='voided', closed_at=? WHERE id=?", (now, cand["id"])
            )
        conn.execute("DELETE FROM inbox_items WHERE user_id=?", (uid,))
        conn.execute(
            "UPDATE consent_log SET detail_json='{}', purge_after=? WHERE user_id_hash=?",
            (purge_after, db.user_hash(uid)),
        )
        return {"ok": True}


# ------------------------------------------------------------------ endpoints

class SwipeBody(BaseModel):
    candidate_id: int
    proposal_version: int = 0
    direction: str


class CounterBody(BaseModel):
    candidate_id: int
    window_start: str
    window_end: str
    tz: str


class CandidateBody(BaseModel):
    candidate_id: int


class DeleteBody(BaseModel):
    confirm: bool = False


def _handle(fn, *args) -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=fn(*args))
    except SchedulingError as exc:
        return JSONResponse(
            status_code=exc.status, content={"error": exc.code, "detail": exc.detail}
        )


@router.get("/dashboard")
def api_dashboard(user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(dashboard, conn, user["id"])


@router.get("/card/{candidate_id}")
def api_card(candidate_id: int, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(card, conn, user["id"], candidate_id)


@router.post("/swipe")
def api_swipe(body: SwipeBody, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(swipe, conn, user["id"], body.candidate_id, body.proposal_version, body.direction)


@router.post("/counter")
def api_counter(body: CounterBody, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(
        counter, conn, user["id"], body.candidate_id, body.window_start, body.window_end, body.tz
    )


@router.post("/revoke")
def api_revoke(body: CandidateBody, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(revoke, conn, user["id"], body.candidate_id)


@router.post("/leave-matching")
def api_leave(user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    return _handle(leave_matching, conn, user["id"])


@router.post("/delete-account")
def api_delete(body: DeleteBody, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    if not body.confirm:
        return JSONResponse(status_code=422, content={"error": "payload_forbidden", "detail": "confirm required"})
    return _handle(delete_account, conn, user["id"])


def swipe_directions() -> list[str]:
    return list(DIRECTIONS)
