"""Autonomous negotiation worker (CONTRACT-chat.md §6, PLAN-chat-frontend.md §C2).

The hands-free half of the negotiation: a student's own assistant ANSWERS the offers the other
student's assistant left pending. The worker never talks to a provider itself — every turn goes
through `chat.run_turn` (and therefore `ai.stream_reply`), so the protocol, the persistence, the
transcript partition and the one-op rule are exactly the ones typed turns use.

Load-bearing rules (each has a test):
  * ONE singleton: a module-level `_STARTED` flag; a second `loop()` raises loudly at startup.
  * RESPONSES ONLY — no initial-offer scanner (PLAN §C2b): the scan looks at offers that already
    exist and are still pending, never at candidates without one.
  * The ADDRESSEE of a pending offer is the candidate's counterpart of `proposer_id`. Without a
    live `delegation` grant and a live account, the worker skips that user silently.
  * Budget: a real `chat_turns` row per attempt (`kind='negotiation'`, `ref='offer:<id>'`), so
    autonomy is not free credits, and the row also IS the attempt counter (cap 2 → one templated
    note telling the user to answer by hand, then the worker stops for that offer).
  * A tighter autonomy ceiling (12 turns / 10 min) short-circuits WITHOUT inserting a row, so a
    skip never eats the shared 20-turn budget.
  * The same per-user in-flight lock as typed turns, acquired NON-BLOCKING: a human typing wins.
  * Delegation is re-read as an advisory pre-check and verified again after the turn. The
    authoritative recheck belongs inside the apply transaction — see the seam note below.

SEAM (reported, not patched — chat.py is lane B's file): `chat._apply_negotiation_op` re-reads
`_user_alive` and `_live_grant(conn, uid, "delegation")`, but it runs BEFORE `activity.propose` /
`activity.respond`, which each open their own `db.immediate`. SQLite cannot nest BEGIN IMMEDIATE,
so that recheck is a pre-flight statement, not the in-transaction recheck PLAN §C2a asks for; the
window is "grant withdrawn between that SELECT and the core's BEGIN IMMEDIATE". The worker narrows
it (pre-check under the lock, post-drain verification) but cannot close it from this file.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json

from server import activity, chat, config, db, offers
from server.scheduling import counterpart

# One tick every 15 s (PLAN §C2) — the integrator mounts `loop()` beside `sweep.loop()`.
_TICK_SECONDS = 15
# PLAN §C2: "no autonomous turn once a user hits 12 turns in the last 10 min".
AUTONOMY_WINDOW_MINUTES = chat.RATE_WINDOW_MINUTES
AUTONOMY_CEILING = 12
# PLAN §C2: "after 2 failed attempts on the same offer the worker stops for that pair".
MAX_ATTEMPTS = 2

# Server-templated fact shown when the worker gives up: the manual Accept/Counter/Pass card is
# the fallback path. Pinned text — the dedupe check is content equality on exactly this string.
MANUAL_NOTE = "your assistant stopped retrying this offer; accept, counter or pass it from the chat"

_STARTED = False


def started() -> bool:
    """True once the singleton loop has been started (and not yet finished)."""
    return _STARTED


# --------------------------------------------------------------------- read-only helpers


def _live_delegation(conn, uid: int, now: str | None = None) -> bool:
    row = db.one(
        conn,
        "SELECT 1 FROM grants WHERE user_id=? AND kind='delegation' AND withdrawn_at IS NULL"
        " AND expires_at > ? LIMIT 1",
        (uid, now or db.utcnow()),
    )
    return row is not None


def _alive(conn, uid: int) -> bool:
    row = db.one(conn, "SELECT deleted_at FROM users WHERE id=?", (uid,))
    return row is not None and row["deleted_at"] is None


def _window_cutoff(minutes: int) -> str:
    now = db.utcnow()
    base = datetime.datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    return (base - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attempts(conn, uid: int, ref: str) -> int:
    """Attempts already spent on one target ('offer:<id>' or 'initial:<candidate_id>')."""
    return db.one(
        conn,
        "SELECT COUNT(*) AS n FROM chat_turns WHERE user_id=? AND kind='negotiation' AND ref=?",
        (uid, ref),
    )["n"]


def _autonomy_used(conn, uid: int) -> int:
    """Turns of ANY kind in the window: PLAN §C2 counts the user's turns, not just ours (F12).

    The old query filtered kind='negotiation', so twelve typed turns still left the assistant
    free to spend autonomous budget the plan says it does not have.
    """
    return db.one(
        conn,
        "SELECT COUNT(*) AS n FROM chat_turns WHERE user_id=? AND at > ?",
        (uid, _window_cutoff(AUTONOMY_WINDOW_MINUTES)),
    )["n"]


def _manual_note_sent(conn, uid: int) -> bool:
    row = db.one(
        conn,
        "SELECT 1 FROM chat_messages WHERE user_id=? AND role='note' AND content=? LIMIT 1",
        (uid, MANUAL_NOTE),
    )
    return row is not None


def _pending_offers(conn) -> list[tuple[object, dict]]:
    """(candidate row, offer row) for every candidate whose CURRENT proposal has a live offer.

    `activity_offers` carries no expiry/settled timestamp — the settled marker IS `state`
    ('accepted'|'countered'|'passed'); only `state='offered'` is still pending.
    """
    found: list[tuple[object, dict]] = []
    seen_proposals: set[int] = set()
    for row in db.q(
        conn,
        "SELECT c.id AS candidate_id, c.user_a, c.user_b, p.id AS proposal_id"
        " FROM candidates c JOIN proposals p ON p.candidate_id = c.id"
        " WHERE c.state IN ('open','matched') AND p.state != 'voided'"
        " ORDER BY p.id, c.id",
    ):
        if row["proposal_id"] in seen_proposals:
            continue
        seen_proposals.add(row["proposal_id"])
        offer = offers.latest_offer(conn, row["proposal_id"])
        if offer is None or offer["state"] != "offered":
            continue
        cand = db.one(conn, "SELECT * FROM candidates WHERE id=?", (row["candidate_id"],))
        if cand is None:
            continue
        found.append((cand, dict(offer)))
    return found


def _offer_context(conn, cand, offer: dict, uid: int) -> str:
    """The `system_extra` block: the incoming offer, my profile, rounds left, reason glossary."""
    prop = db.one(conn, "SELECT * FROM proposals WHERE id=?", (offer["proposal_id"],))
    payload = activity._agent_offer_payload(conn, cand, prop, offer)  # pure: builds a dict only
    payload["proposed_by_you"] = offer["proposer_id"] == uid
    return json.dumps(
        {
            "offer": payload,
            "my_profile": chat.state_summary(conn, uid),
            "rounds_left": max(0, offers.MAX_ROUNDS - int(offer["round"])),
            "reasons": list(offers.REASONS),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _note_manual_path(conn, uid: int) -> None:
    """Exactly one templated fact per user, ever (content equality is the marker)."""
    with db.immediate(conn):
        if _manual_note_sent(conn, uid):
            return
        chat.persist_note(conn, uid, MANUAL_NOTE)


# --------------------------------------------------------------------- one offer


async def _autonomous_turn(conn, uid: int, ref: str, build_context, *, manual_note: bool) -> str:
    """One autonomous turn, with the shared lock/budget/ceiling discipline.

    The ORDER is load-bearing (gate review P1 #8): the in-flight lock is checked FIRST, so a
    busy user costs no budget, no ledger row and no note — two busy ticks used to burn the
    offer's whole attempt allowance and show the manual fallback despite zero provider calls.
    Then the attempt cap, then the autonomy ceiling (all turn kinds, F12), and only then is the
    `chat_turns` row reserved. Everything is revalidated again under the lock.
    """
    lock = chat.get_user_lock(uid)
    if lock.locked():
        return "skip:busy"
    if not _alive(conn, uid):
        return "skip:gone"
    if not _live_delegation(conn, uid):  # advisory — the activity core rechecks (see SEAM)
        return "skip:no_delegation"
    if _attempts(conn, uid, ref) >= MAX_ATTEMPTS:
        if manual_note:  # the human fallback only makes sense for an offer they can answer
            _note_manual_path(conn, uid)
        return "skip:attempts"
    if _autonomy_used(conn, uid) >= AUTONOMY_CEILING:
        return "skip:ceiling"
    try:
        reserved = chat.reserve_turn(conn, uid, "negotiation", ref)
    except chat.ChatRateLimited:
        return "skip:rate"
    if not reserved:  # the account went away between the checks and the reserve
        return "skip:gone"

    await lock.acquire()
    try:
        # Recheck under the lock: the user may have withdrawn while we waited.
        if not _alive(conn, uid) or not _live_delegation(conn, uid):
            return "skip:no_delegation"
        turn = chat.Turn(kind="negotiation", user_text=None, system_extra=build_context())
        events: list[dict] = []
        async for event in chat.run_turn(conn, uid, turn):
            events.append(event)  # streaming to nobody: drained to the end
    finally:
        lock.release()

    # Post-drain verification: the turn must have acted while delegation was live.
    if not _live_delegation(conn, uid):
        return "acted:delegation_lapsed"
    acted = any(event.get("type") == "actions" for event in events)
    return "acted" if acted else "note"


async def _answer(conn, cand, offer: dict) -> str:
    """Answer one pending offer. Returns a short outcome tag (no PII, no prose)."""
    uid = counterpart(cand, offer["proposer_id"])
    if uid == offer["proposer_id"]:  # defensive: malformed row
        return "skip:not_addressee"
    return await _autonomous_turn(
        conn,
        uid,
        f"offer:{offer['id']}",
        lambda: _offer_context(conn, cand, offer, uid),
        manual_note=True,
    )


# --------------------------------------------------------------------- one tick


async def tick_once(conn=None) -> dict:
    """One scan+answer pass. Own connection unless one is given. Returns counts only."""
    own = conn is None
    if own:
        conn = db.connect()
    stats = {"pending": 0, "answered": 0, "skipped": 0}
    try:
        if config.ai_api_url() is None:  # chat disabled: spend no budget, do nothing
            return stats
        for cand, offer in _pending_offers(conn):
            stats["pending"] += 1
            try:
                outcome = await _answer(conn, cand, offer)
            except Exception:  # one bad candidate never takes the tick down
                stats["skipped"] += 1
                continue
            if outcome.startswith("acted"):
                stats["answered"] += 1
            else:
                stats["skipped"] += 1
    finally:
        if own:
            conn.close()
    return stats


async def loop() -> None:
    """The long-running worker (the integrator mounts this in app.py's lifespan)."""
    global _STARTED
    if _STARTED:
        raise RuntimeError("negotiation loop already running")
    _STARTED = True
    try:
        while True:
            with contextlib.suppress(Exception):
                await tick_once()
            await asyncio.sleep(_TICK_SECONDS)
    finally:
        _STARTED = False
