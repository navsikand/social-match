"""Chat turn engine, streaming protocol and endpoints (CONTRACT-chat.md §3, PLAN §B/B2/C).

One LLM entrypoint: `run_turn` — user turns (typed text) and negotiation turns (the worker's
incoming-offer contexts) go through the same function, the same provider seam
(`server.ai.stream_reply`) and the same event shapes. Everything else in this module is the
state echo (`build_current_payload`), the optimistic-concurrency hash, the rate/budget ledger
(`chat_turns`), the single pending preview (`chat_pending`) and the HTTP surface.

Design rules that are load-bearing:
  * `statewrite.write_state(mode="chat")` is the only writer. Grants are never touched and the
    payload's consent block is an echo, never applied.
  * A batch is all-immediate (tags/wants/busy) or one proposal (windows/events). Immediate ops
    in a proposal batch ride along and apply only at Confirm. There is no partial apply.
  * Windows/events never write on propose — they become one `chat_pending` row with the exact
    canonical hash they were previewed against. Confirm re-validates, re-checks the hash and
    applies inside ONE transaction.
  * Notes in the transcript are SERVER-TEMPLATED FACTS built from structured fields only.
    Provider prose goes into `assistant` rows, never into `note` rows.

CONTRACT seam note: the activity cores `activity.propose` / `activity.respond` open their own
`db.immediate`; SQLite cannot nest `BEGIN IMMEDIATE`, so this module cannot wrap them. They are
called directly and own their transaction; the pre-checks this module adds (deleted account,
live delegation) run just before, and the core re-checks candidate/proposal/offer state inside
its own transaction. Nothing here writes outside a `db.immediate`.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from server import activity, ai, config, db, events as events_mod, offers, statewrite
from server.schema import (
    MAX_TAGS,
    MAX_WINDOWS,
    TAG_VOCAB,
    WANT_VOCAB,
    PublicEvent,
    StatePayload,
    Window,
)
from server.scheduling import SchedulingError, counterpart
from server.sessions import require_session

router = APIRouter(prefix="/api", tags=["chat"])

# Rate/budget window (PLAN §C): 20 turns per user per 10 minutes, any kind.
RATE_WINDOW_MINUTES = 10
RATE_LIMIT = 20
# A preview lives for 10 minutes and is superseded by the next proposal.
PENDING_TTL_MINUTES = 10
MAX_BODY_BYTES = 64 * 1024
MAX_CONTENT_CHARS = 4000
MAX_HISTORY = 200
PROVIDER_HISTORY = 40
#: Pinned text for a reply that never finished cleanly (F5). Tests match on it exactly.
CUT_OFF_NOTE = "that reply was cut off — nothing was applied"

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
_LOCAL_FMT = "%Y-%m-%dT%H:%M:%S"

# In-flight turn locks: one turn per user at a time, typed or autonomous (PLAN §C).
_LOCKS: dict[int, asyncio.Lock] = {}


# --------------------------------------------------------------------- errors / turn types


class ChatRateLimited(Exception):
    """Too many turns in the rate window (→ 429)."""


class TurnBusy(Exception):
    """A turn is already running for this user (→ 409)."""


@dataclass
class Turn:
    """One engine call, shared by user turns and negotiation turns."""

    kind: str  # "user" | "negotiation"
    user_text: str | None = None
    system_extra: str = ""


# --------------------------------------------------------------------- small helpers


def get_user_lock(uid: int) -> asyncio.Lock:
    """The per-user in-flight lock. Never awaited while held by the same turn."""
    lock = _LOCKS.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[uid] = lock
    return lock


async def _acquire_turn_lock(uid: int) -> asyncio.Lock:
    lock = get_user_lock(uid)
    if lock.locked():
        raise TurnBusy()
    await lock.acquire()
    return lock


def _shift(stamp: str, *, minutes: float = 0, days: float = 0) -> str:
    base = datetime.datetime.strptime(stamp, _UTC_FMT).replace(tzinfo=datetime.UTC)
    return (base + datetime.timedelta(minutes=minutes, days=days)).strftime(_UTC_FMT)


def _utc_to_local(value: str, tz: str) -> str:
    """UTC 'YYYY-MM-DDTHH:MM:SSZ' → naive local wall time in `tz` (the Window shape)."""
    parsed = datetime.datetime.strptime(value.strip(), _UTC_FMT).replace(tzinfo=datetime.UTC)
    return parsed.astimezone(ZoneInfo(tz)).strftime(_LOCAL_FMT)


def _live_grant(conn, uid: int, kind: str, now: str | None = None):
    return db.one(
        conn,
        "SELECT * FROM grants WHERE user_id=? AND kind=? AND withdrawn_at IS NULL"
        " AND expires_at > ? ORDER BY version DESC LIMIT 1",
        (uid, kind, now or db.utcnow()),
    )


def _user_alive(conn, uid: int) -> bool:
    row = db.one(conn, "SELECT deleted_at FROM users WHERE id=?", (uid,))
    return row is not None and row["deleted_at"] is None


@contextlib.contextmanager
def _live_write(conn, uid: int):
    """Run a chat write for `uid` only while the account still exists (gate review P1 #3).

    Provider work can finish AFTER `delete_account`'s cascade has committed. Without this guard
    the completion re-creates chat_messages / chat_pending / chat_turns for a deleted account.
    When the caller already holds a transaction we join it (SQLite cannot nest BEGIN IMMEDIATE),
    so the liveness read and the write share one transaction's atomicity. Yields True when the
    caller may write.
    """
    if conn.in_transaction:
        yield _user_alive(conn, uid)
        return
    with db.immediate(conn):
        yield _user_alive(conn, uid)


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


def _redact(text: str) -> str:
    return ai.redact(text)


def _error(status: int, code: str, detail: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "payload_forbidden", "detail": detail})


# --------------------------------------------------------------------- state echo


def build_current_payload(conn, uid: int) -> StatePayload:
    """The current truth as a validated StatePayload — the echo `POST /api/state` would send.

    Stored rows are converted back to LOCAL wall-time strings (the schema's Window/PublicEvent
    shape). A row that no longer validates (time passed, stale data outside the horizon) is
    dropped from the echo rather than crashing the snapshot.
    """
    windows: list[Window] = []
    for row in db.q(
        conn,
        "SELECT starts_at, ends_at, tz FROM availability_windows WHERE user_id=? ORDER BY id",
        (uid,),
    ):
        try:
            windows.append(
                Window(
                    start=_utc_to_local(row["starts_at"], row["tz"]),
                    end=_utc_to_local(row["ends_at"], row["tz"]),
                    tz=row["tz"],
                )
            )
        except (ValidationError, ValueError, KeyError):
            continue
    windows = windows[:MAX_WINDOWS]

    tags: list[str] = []
    for row in db.q(conn, "SELECT raw FROM interest_raw WHERE user_id=? ORDER BY raw", (uid,)):
        raw = (row["raw"] or "").strip()
        if raw and len(raw) <= 32 and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
            tags.append(raw)
    tags = list(dict.fromkeys(tags))[:MAX_TAGS]

    wants = [
        row["want"]
        for row in db.q(conn, "SELECT want FROM user_wants WHERE user_id=? ORDER BY want", (uid,))
        if row["want"] in WANT_VOCAB
    ]

    load_row = db.one(conn, "SELECT busy_load FROM user_load WHERE user_id=?", (uid,))
    busy_load = load_row["busy_load"] if load_row is not None else None
    if busy_load is not None and not (0 <= int(busy_load) <= 3):
        busy_load = None

    availability = db.one(conn, "SELECT observed_at FROM availability WHERE user_id=?", (uid,))

    event_rows = db.q(
        conn,
        "SELECT url, category, starts_at, ends_at, tz FROM user_events"
        " WHERE user_id=? ORDER BY id",
        (uid,),
    )
    events: list[PublicEvent] = []
    for row in event_rows:
        try:
            events.append(
                PublicEvent(
                    url=row["url"],
                    category=row["category"],
                    start=_utc_to_local(row["starts_at"], row["tz"]),
                    end=_utc_to_local(row["ends_at"], row["tz"]),
                    tz=row["tz"],
                )
            )
        except (ValidationError, ValueError, KeyError):
            continue
    events = events[: events_mod.MAX_EVENTS]
    if events and availability is None:
        # FK should make this impossible; if it ever happens the echo drops the events rather
        # than inventing an approval timestamp.
        events = []
    events_confirmed_at = availability["observed_at"] if events and availability is not None else None

    publication_row = _live_grant(conn, uid, "publication")
    publication = (
        {"tier": publication_row["tier"]}
        if publication_row is not None and publication_row["tier"] in ("card", "card+name")
        else None
    )

    return StatePayload(
        windows=windows,
        tags=tags,
        wants=wants,
        free_time={"source": "student", "student_confirmed_at": None},
        consent={
            "matching": _live_grant(conn, uid, "matching") is not None,
            "publication": publication,
        },
        events=events,
        events_confirmed_at=events_confirmed_at,
        busy_load=busy_load,
    )


def current_state_hash(conn, uid: int) -> str:
    return statewrite.canonical_hash(build_current_payload(conn, uid))


def state_summary(conn, uid: int) -> dict:
    """The chips payload for the UI (and the `state` SSE event)."""
    payload = build_current_payload(conn, uid)
    return {
        "windows": len(payload.windows),
        "interests": list(payload.tags),
        "wants": list(payload.wants),
        "busy": payload.busy_load,
        "events": len(payload.events),
        "delegation": _live_grant(conn, uid, "delegation") is not None,
        "matching": payload.consent.matching,
        "ai": bool(config.ai_api_url()),
    }


# --------------------------------------------------------------------- ledger + transcripts


def reserve_turn(conn, uid: int, kind: str, ref: str | None = None) -> bool:
    """Count this turn against the user's rate window; raise ChatRateLimited past the cap.

    Returns False when the account is gone: a deleted account must not reserve budget either.
    """
    now = db.utcnow()
    cutoff = _shift(now, minutes=-RATE_WINDOW_MINUTES)
    with _live_write(conn, uid) as alive:
        if not alive:
            return False
        used = db.one(
            conn,
            "SELECT COUNT(*) AS n FROM chat_turns WHERE user_id=? AND at > ?",
            (uid, cutoff),
        )["n"]
        if used >= RATE_LIMIT:
            raise ChatRateLimited()
        conn.execute(
            "INSERT INTO chat_turns (user_id, kind, ref, at) VALUES (?,?,?,?)",
            (uid, kind, ref, now),
        )
    return True


def _persist_message(conn, uid: int, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (user_id, role, content, created_at) VALUES (?,?,?,?)",
        (uid, role, content, db.utcnow()),
    )


def persist_user_message(conn, uid: int, text: str) -> None:
    _persist_message(conn, uid, "user", text)


def persist_final_assistant(conn, uid: int, text: str) -> bool | None:
    """Returns None when the account is gone: run_turn then treats the turn as dropped."""
    if not text.strip():
        return True
    with _live_write(conn, uid) as alive:
        if not alive:
            return None
        _persist_message(conn, uid, "assistant", text)
    return True


def persist_note(conn, uid: int, text: str) -> bool | None:
    """Server-templated FACT only — never provider prose. No-ops for a deleted account."""
    with _live_write(conn, uid) as alive:
        if not alive:
            return None
        _persist_message(conn, uid, "note", text)
    return True


def purge_chat(conn, now: str) -> int:
    """Retention: transcript past chat_ttl_days, expired previews, ledger rows past the window.

    Runs inside the caller's transaction (the integrator mounts it in sweep.run_sweep).
    Returns the total number of rows removed.
    """
    removed = 0
    cutoff = _shift(now, days=-config.chat_ttl_days())
    removed += conn.execute(
        "DELETE FROM chat_messages WHERE created_at < ?", (cutoff,)
    ).rowcount
    removed += conn.execute(
        "DELETE FROM chat_pending WHERE expires_at < ?", (now,)
    ).rowcount
    # F7: a negotiation row is the CAP COUNTER as well as budget, so it must outlive the rate
    # window while its target is still actionable — otherwise an unanswered offer resumes
    # provider retries after every sweep cycle. Exemptions:
    #   ref='offer:<id>'   that offer still exists and is still 'offered'
    #   ref='initial:<id>' that candidate is still open/matched and has no offer yet
    removed += conn.execute(
        "DELETE FROM chat_turns WHERE at < ?"
        " AND NOT (kind='negotiation' AND ref IS NOT NULL AND ("
        "  (ref LIKE 'offer:%' AND EXISTS ("
        "    SELECT 1 FROM activity_offers o"
        "    WHERE o.id = CAST(substr(ref, 7) AS INTEGER) AND o.state = 'offered'))"
        "  OR (ref LIKE 'initial:%' AND EXISTS ("
        "    SELECT 1 FROM candidates c JOIN proposals p ON p.candidate_id = c.id"
        "    WHERE c.id = CAST(substr(ref, 9) AS INTEGER)"
        "      AND c.state IN ('open','matched') AND p.state != 'voided'"
        "      AND NOT EXISTS (SELECT 1 FROM activity_offers o2 WHERE o2.proposal_id = p.id)))"
        " ))",
        (_shift(now, minutes=-RATE_WINDOW_MINUTES),),
    ).rowcount
    return removed


def cascade_chat(conn, uid: int) -> None:
    """Account deletion: the whole chat surface for this user goes."""
    conn.execute("DELETE FROM chat_messages WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM chat_pending WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM chat_turns WHERE user_id=?", (uid,))


# --------------------------------------------------------------------- system prompt

_PROTOCOL_RULES = """\
PROTOCOL — read carefully, the server enforces every line:

1. Write a short, plain-text reply (no markup, no headings). Never ask for course names,
   calendar event titles, or anything private that is not on this list.
2. End the reply with EXACTLY ONE trailing action block: a fenced code block whose fence
   line is three backticks followed by json, containing a single JSON object with an
   "actions" array, and absolutely nothing after the closing fence. Zero fences means a
   plain conversational turn (nothing is applied). Two or more fences, or a fence that is
   not the last thing in the reply, means NOTHING is applied.
3. The closed op list (op + EXACT keys, no extra keys, no duplicate field in one batch):
   {"op": "set_windows", "windows": [{"start": "YYYY-MM-DDTHH:MM:SS", "end": "...", "tz": "IANA/Zone"}]}
   {"op": "set_events", "events": [{"url": "https://...", "category": "music", "start": "...", "end": "...", "tz": "..."}]}
   {"op": "set_interests", "tags": ["matcha", "bouldering"]}
   {"op": "set_wants", "wants": ["hangout"]}
   {"op": "set_busy_load", "value": 2}
   {"op": "none"}
   Unknown ops or extra keys reject the WHOLE batch. Two ops touching the same field also
   reject the whole batch. The batch is all-or-nothing: there is no partial apply.
4. set_windows and set_events are PROPOSALS: the server shows the user a preview and the user
   must confirm it. set_interests, set_wants and set_busy_load apply immediately. If a batch
   mixes them, the immediate ones ride along and only apply when the user confirms the
   proposal. Dates use LOCAL wall time ("YYYY-MM-DDTHH:MM:SS", no offset) plus a separate tz.
   Windows: at most 20, each at most 8 hours long, starting within the next 30 days and not in
   the past. Events: at most 10, at most 24 hours each, not already ended, within 30 days.
   Times are the user's real local clock; never guess a timezone.
5. Grants, photos and account settings are NEVER settable here — there is no op for them. If
   the user asks, say that must be changed on the Settings page.
6. Keep answers short. Describe what you changed as a fact ("saved 3 windows") and never claim
   a write the server did not confirm.
"""

_NEGOTIATION_RULES = """\
NEGOTIATION TURN — the user did not type anything. Your reply must contain EXACTLY ONE op:
  {"op": "offer", "candidate_id": N, "proposal_version": N, "tag": "...", "url": "https://..."}
  {"op": "accept", "offer_id": N}
  {"op": "counter", "offer_id": N, "tag": "...", "url": "https://..."}
  {"op": "pass", "offer_id": N, "reason": "too_expensive|too_far|not_my_vibe|wrong_time"}
The op must be the same single trailing json fence as always. Offer both of you share an
interest from the tag vocabulary; the link must be a public https page. Any other op, or more
than one op, is refused and nothing changes. Your text is a short note to your own user.
"""


def _profile_lines(conn, uid: int) -> list[str]:
    payload = build_current_payload(conn, uid)
    lines = [f"- Free-time windows ({len(payload.windows)}): most recent first stored order."]
    for win in payload.windows:
        lines.append(f"    {win.start} → {win.end} ({win.tz})")
    if not payload.windows:
        lines.append("    (none stored)")
    lines.append(f"- Raw interests: {', '.join(payload.tags) if payload.tags else '(none)'}")
    canonical = [row["canonical"] for row in db.q(
        conn,
        "SELECT canonical FROM interest_raw WHERE user_id=? AND canonical IS NOT NULL ORDER BY raw",
        (uid,),
    )]
    lines.append(
        f"- Canonical interests the matcher uses: "
        f"{', '.join(dict.fromkeys(canonical)) if canonical else '(none)'}"
    )
    lines.append(f"- Wants: {', '.join(payload.wants) if payload.wants else '(none)'}")
    busy = "unknown" if payload.busy_load is None else str(payload.busy_load)
    lines.append(f"- Busy load: {busy} (0 = light week, 1 = some, 2 = heavy, 3 = overloaded)")
    lines.append(f"- Public events considered: {len(payload.events)}")
    return lines


def build_system_prompt(conn, uid: int, turn: Turn) -> str:
    """Profile + bounded vocabularies + the protocol rules the server enforces."""
    lines = [
        "You are the user's social-matching assistant on Social Match. You may read the "
        "profile summary below and propose changes through the action protocol.",
        "",
        "CURRENT PROFILE",
        *_profile_lines(conn, uid),
        "",
        "BOUNDED VOCABULARIES",
        f"- Interest tag examples (canonical names the matcher scores): {', '.join(TAG_VOCAB)}",
        "  Raw interests are allowed and are stored as the user typed them: 'matcha' is kept "
        "as raw 'matcha' and canonicalised to 'coffee' at write time. Do not silently rename "
        "the user's words; the server does the mapping.",
        f"- Wants (exactly these): {', '.join(WANT_VOCAB)}",
        f"- Event categories: {', '.join(events_mod.EVENT_CATEGORIES)}",
        f"- Pass reasons: {', '.join(offers.REASONS)}",
        f"- State version (for your awareness only): {current_state_hash(conn, uid)}",
        "",
        _PROTOCOL_RULES,
    ]
    if turn.kind == "negotiation":
        lines.extend(["", _NEGOTIATION_RULES])
        if turn.system_extra:
            lines.extend(["", "OFFER CONTEXT", turn.system_extra])
    return "\n".join(lines)


# --------------------------------------------------------------------- action parsing

_FENCE_RE = re.compile(r"```([A-Za-z0-9_+.-]*)[^\n]*\n(.*?)```", re.DOTALL)

_OP_KEYS: dict[str, set[str]] = {
    "set_windows": {"op", "windows"},
    "set_events": {"op", "events"},
    "set_interests": {"op", "tags"},
    "set_wants": {"op", "wants"},
    "set_busy_load": {"op", "value"},
    "none": {"op"},
}
_OP_FIELD = {
    "set_windows": "windows",
    "set_events": "events",
    "set_interests": "tags",
    "set_wants": "wants",
    "set_busy_load": "busy_load",
    "none": "none",
}
_NEGOTIATION_KEYS: dict[str, set[str]] = {
    "offer": {"op", "candidate_id", "proposal_version", "tag", "url"},
    "accept": {"op", "offer_id"},
    "counter": {"op", "offer_id", "tag", "url"},
    "pass": {"op", "offer_id", "reason"},
}


@dataclass
class _Fence:
    lang: str
    body: str
    start: int
    end: int


def _fences(text: str) -> list[_Fence]:
    return [
        _Fence(match.group(1).strip().lower(), match.group(2), match.start(), match.end())
        for match in _FENCE_RE.finditer(text)
    ]


def _json_action_block(text: str) -> tuple[list[Any] | None, str | None]:
    """Return (actions, refusal_note). Exactly one trailing json fence or nothing applies."""
    fences = _fences(text)
    json_fences = [f for f in fences if f.lang == "json"]
    if not json_fences:
        return None, None
    if len(json_fences) > 1 or len(fences) > 1:
        return None, "multiple action blocks — nothing applied"
    only = json_fences[0]
    if only is not only and False:  # pragma: no cover - keep the intent obvious
        return None, "multiple action blocks — nothing applied"
    if text[only.end :].strip():
        return None, "the action block wasn't last in the reply — nothing applied"
    try:
        parsed = json.loads(only.body)
    except Exception as exc:
        return None, f"couldn't read the action block — nothing applied ({_redact(str(exc))})"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("actions"), list):
        return None, "the action block needs an actions array — nothing applied"
    return parsed["actions"], None


def _validate_actions(actions: list[Any]) -> tuple[list[dict] | None, str | None]:
    """Batch validation (§B): all-or-nothing, unknown op / extra keys / duplicate field fail."""
    if not actions:
        return None, "the action block was empty — nothing applied"
    seen: set[str] = set()
    ops: list[dict] = []
    for action in actions:
        if not isinstance(action, dict):
            return None, "each action must be an object — nothing applied"
        name = action.get("op")
        if not isinstance(name, str) or name not in _OP_KEYS:
            return None, f"unknown op {(str(name))[:20]!r} — nothing applied"
        if set(action) != _OP_KEYS[name]:
            return None, f"the {name} action has the wrong fields — nothing applied"
        field_name = _OP_FIELD[name]
        if field_name in seen:
            return None, (
                f"two actions touch the same field ({field_name}) — nothing applied"
            )
        seen.add(field_name)

        if name == "set_windows":
            windows = action["windows"]
            if not isinstance(windows, list):
                return None, "windows must be a list — nothing applied"
            if len(windows) > MAX_WINDOWS:
                return None, f"at most {MAX_WINDOWS} windows — nothing applied"
            if any(not isinstance(item, dict) for item in windows):
                return None, "each window must be an object — nothing applied"
        elif name == "set_events":
            event_list = action["events"]
            if not isinstance(event_list, list):
                return None, "events must be a list — nothing applied"
            if len(event_list) > events_mod.MAX_EVENTS:
                return None, f"at most {events_mod.MAX_EVENTS} events — nothing applied"
            if any(not isinstance(item, dict) for item in event_list):
                return None, "each event must be an object — nothing applied"
        elif name == "set_interests":
            tags = action["tags"]
            if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
                return None, "tags must be a list of strings — nothing applied"
            if len(tags) > MAX_TAGS:
                return None, f"at most {MAX_TAGS} tags — nothing applied"
        elif name == "set_wants":
            wanted = action["wants"]
            if not isinstance(wanted, list) or any(not isinstance(w, str) for w in wanted):
                return None, "wants must be a list of strings — nothing applied"
        elif name == "set_busy_load":
            value = action["value"]
            if isinstance(value, bool) or not isinstance(value, int) or not (0 <= value <= 3):
                return None, "busy load must be an integer 0-3 — nothing applied"
        ops.append(action)

    if "none" in seen and len(ops) > 1:
        return None, "the none action can't be combined with other actions — nothing applied"
    return ops, None


def _normalize_proposal(ops: list[dict]) -> tuple[str, dict[str, list[dict]], list[str]]:
    """Validate every window/event item NOW; return (kind, sections, errors).

    F13: `sections` is the payload's `items`, split by TYPE — {"windows": [...], "events": [...]}
    — so a batch that proposes both renders and confirms in groups instead of one flattened,
    wrongly-typed list. `kind` keeps its historical meaning for the surface ('windows' wins a
    mixed batch) and `ops` stays the APPLY source of truth: sections are display only.
    """
    kind = "windows" if any(op["op"] == "set_windows" for op in ops) else "events"
    sections: dict[str, list[dict]] = {"windows": [], "events": []}
    errors: list[str] = []
    for op in ops:
        if op["op"] == "set_windows":
            for index, raw in enumerate(op["windows"]):
                try:
                    sections["windows"].append(Window.model_validate(raw).model_dump(mode="json"))
                except ValidationError as exc:
                    errors.append(f"window {index + 1}: {_first_error(exc)}")
        elif op["op"] == "set_events":
            for index, raw in enumerate(op["events"]):
                try:
                    sections["events"].append(
                        PublicEvent.model_validate(raw).model_dump(mode="json")
                    )
                except ValidationError as exc:
                    errors.append(f"event {index + 1}: {_first_error(exc)}")
    return kind, sections, errors


def _op_summary(op: dict) -> str:
    name = op["op"]
    if name == "set_windows":
        return f"windows updated: {len(op['windows'])} stored"
    if name == "set_events":
        return f"events updated: {len(op['events'])} stored"
    if name == "set_interests":
        return f"interests updated: {len(op['tags'])} saved"
    if name == "set_wants":
        return f"wants updated: {len(op['wants'])} saved"
    if name == "set_busy_load":
        return f"busy load set to {op['value']}"
    return "nothing to change"


def _merge_ops(
    conn,
    uid: int,
    ops: list[dict],
    *,
    windows_stamp: str | None = None,
    events_stamp: str | None = None,
) -> StatePayload:
    """One final payload: complete replacement of each touched field, everything else kept."""
    base = build_current_payload(conn, uid).model_dump(mode="json")
    for op in ops:
        name = op["op"]
        if name == "set_windows":
            base["windows"] = [
                Window.model_validate(item).model_dump(mode="json") for item in op["windows"]
            ]
            if windows_stamp is not None:
                base["free_time"]["student_confirmed_at"] = windows_stamp
        elif name == "set_events":
            base["events"] = [
                PublicEvent.model_validate(item).model_dump(mode="json")
                for item in op["events"]
            ]
            base["events_confirmed_at"] = (
                events_stamp or base.get("events_confirmed_at") or db.utcnow()
            )
        elif name == "set_interests":
            base["tags"] = list(op["tags"])
        elif name == "set_wants":
            base["wants"] = list(op["wants"])
        elif name == "set_busy_load":
            base["busy_load"] = op["value"]
    return StatePayload.model_validate(base)


# --------------------------------------------------------------------- pending preview store


def _store_pending(
    conn, uid: int, kind: str, ops: list[dict], items: dict[str, list[dict]], state_hash: str
) -> int | None:
    """One preview row. Returns None when the account is gone (nothing is stored)."""
    now = db.utcnow()
    payload = json.dumps({"ops": ops, "items": items})
    with db.immediate(conn):
        if not _user_alive(conn, uid):
            return None
        conn.execute("DELETE FROM chat_pending WHERE user_id=?", (uid,))
        cursor = conn.execute(
            "INSERT INTO chat_pending (user_id, kind, payload_json, state_hash, created_at,"
            " expires_at) VALUES (?,?,?,?,?,?)",
            (uid, kind, payload, state_hash, now, _shift(now, minutes=PENDING_TTL_MINUTES)),
        )
        return int(cursor.lastrowid)


def _latest_pending(conn, uid: int):
    return db.one(
        conn,
        "SELECT * FROM chat_pending WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (uid,),
    )


# --------------------------------------------------------------------- negotiation notes


def _structured_note(who: str, decided: str, tag=None, url=None, reason=None) -> str:
    """A fixed template rendered from {decided, tag, url, reason} only — no provider prose."""
    if decided == "offer":
        return f"{who} proposed {offers.human_tag(str(tag))}: {url}"
    if decided == "accept":
        return f"{who} accepted the activity"
    if decided == "counter":
        return f"{who} proposed {offers.human_tag(str(tag))} instead: {url}"
    word = offers.REASON_WORDS.get(reason, reason) if isinstance(reason, str) else "no reason given"
    return f"{who} passed: {word}"


class _Drop(Exception):
    """The negotiation op was refused/dropped: record honest note, change nothing."""


def _apply_negotiation_op(conn, uid: int, op: dict) -> dict:
    """Run one negotiation op through the activity cores; raise _Drop with a note on refusal."""
    if not _user_alive(conn, uid):
        raise _Drop("your account is no longer available — nothing applied")
    if _live_grant(conn, uid, "delegation") is None:
        raise _Drop("your delegation was off when this would have applied — nothing happened")
    name = op["op"]
    try:
        if name == "offer":
            result = activity.propose(
                conn,
                uid,
                int(op["candidate_id"]),
                int(op["proposal_version"]),
                op["tag"],
                op["url"],
                autonomous=True,  # a human did not click: the core re-reads the grant itself
            )
            result["_decided"] = "offer"
        else:
            offer_row = db.one(
                conn, "SELECT candidate_id FROM activity_offers WHERE id=?", (int(op["offer_id"]),)
            )
            if offer_row is None:
                raise _Drop("that offer no longer exists — nothing applied")
            result = activity.respond(
                conn,
                uid,
                int(offer_row["candidate_id"]),
                int(op["offer_id"]),
                name,
                reason=op.get("reason"),
                tag=op.get("tag"),
                url=op.get("url"),
                autonomous=True,
            )
            result["_decided"] = name
            result["_candidate_id"] = int(offer_row["candidate_id"])
        # F2: notes are templated from the COMMITTED row, never from the provider's raw op
        # values (which may carry arbitrary prose or private content).
        result.update(_committed_fields(conn, result))
        return result
    except SchedulingError as exc:
        raise _Drop(exc.detail or exc.code) from exc


def _committed_fields(conn, result: dict) -> dict[str, str | None]:
    """The validated, committed tag/url/reason of the offer the core just wrote."""
    offer_id = result.get("offer_id")
    row = db.one(conn, "SELECT * FROM activity_offers WHERE id=?", (int(offer_id),)) if offer_id else None
    if row is None:  # pragma: no cover - the core just wrote it in the same transaction
        return {"_tag": None, "_url": None, "_reason": None}
    return {"_tag": row["activity_tag"], "_url": row["activity_url"], "_reason": row["reason"]}


def _negotiation_summary(decided: str, result: dict) -> str:
    """Rendered from the COMMITTED result only — the provider's op values never reach a user."""
    if decided in ("offer", "counter"):
        verb = "offered" if decided == "offer" else "countered with"
        tag = offers.human_tag(str(result.get("_tag") or ""))
        return f"{verb} {tag} (round {result.get('round')} of {result.get('max_rounds')})"
    if decided == "accept":
        return "accepted the activity"
    suffix = " — final round" if result.get("exhausted") else ""
    reason = result.get("_reason")
    word = offers.REASON_WORDS.get(reason, reason) if isinstance(reason, str) else "no reason given"
    return f"passed: {word}{suffix}"


def _parse_negotiation_ops(actions: list[Any]) -> tuple[dict | None, str | None]:
    if not actions:
        return None, "the assistant proposed no action"
    if len(actions) > 1:
        return None, "more than one negotiation op — nothing applied"
    action = actions[0]
    if not isinstance(action, dict) or action.get("op") not in _NEGOTIATION_KEYS:
        return None, "that wasn't a valid negotiation op — nothing applied"
    if set(action) != _NEGOTIATION_KEYS[action["op"]]:
        return None, "the negotiation op has the wrong fields — nothing applied"
    return action, None


# --------------------------------------------------------------------- the turn engine


def _provider_messages(conn, uid: int) -> list[dict]:
    rows = db.q(
        conn,
        "SELECT role, content FROM chat_messages WHERE user_id=? AND role IN ('user','assistant')"
        " ORDER BY id DESC LIMIT ?",
        (uid, PROVIDER_HISTORY),
    )
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, separators=(',', ':'), default=str)}\n\n"


async def run_turn(conn, uid: int, turn: Turn) -> AsyncIterator[dict]:
    """The single LLM entrypoint. Streams dict events; never raises mid-flight."""
    if turn.kind not in ("user", "negotiation"):
        raise ValueError(f"unknown turn kind {turn.kind!r}")

    # Capture the state version BEFORE anything else: every apply path re-checks it.
    h0 = current_state_hash(conn, uid)
    if turn.kind == "user" and turn.user_text is not None:
        persist_user_message(conn, uid, turn.user_text)
    system = build_system_prompt(conn, uid, turn)
    messages = _provider_messages(conn, uid)

    chunks: list[str] = []
    provider_error: ai.AIError | None = None
    status = ai.EOF
    stream = ai.stream_reply(system, messages)
    try:
        # F5: read the completion STATUS (`ai.Completion`, the stream's last item) rather than
        # assuming a finished stream executed cleanly. Actions below run only on ai.DONE; a
        # capped or unterminated reply is text, never an instruction.
        async for piece in stream:
            if isinstance(piece, ai.Completion):
                status = piece.status if piece.status in (ai.DONE, ai.CAPPED) else ai.EOF
                continue
            chunks.append(piece)
            yield {"type": "token", "text": piece}
    except ai.AIError as exc:
        provider_error = exc
    except Exception as exc:  # a broken provider seam never takes the stream down
        detail = _redact(str(exc))
        with contextlib.suppress(Exception):
            persist_note(conn, uid, f"the assistant could not answer: {detail}")
        yield {"type": "error", "code": "internal", "detail": detail}
        yield {"type": "done"}
        return
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()

    text = "".join(chunks)
    if provider_error is not None:
        detail = _redact(provider_error.detail)
        with contextlib.suppress(Exception):
            persist_note(conn, uid, f"the assistant could not answer: {detail}")
        yield {"type": "error", "code": provider_error.code, "detail": detail}
        yield {"type": "done"}
        return

    if persist_final_assistant(conn, uid, text) is None:
        # The account was deleted while the provider was thinking: a dropped turn. The text
        # was not stored, and no action may run (there is nobody to apply it for).
        yield {"type": "done"}
        return

    if status != ai.DONE:
        # The reply was cut off (cap) or never terminated ([DONE] missing). Its text is honest
        # record-keeping; its fence is not an instruction.
        note = CUT_OFF_NOTE
        with contextlib.suppress(Exception):
            persist_note(conn, uid, note)
        yield {"type": "note", "text": note}
        yield {"type": "done"}
        return

    actions, refusal = _json_action_block(text)
    if refusal is not None:
        with contextlib.suppress(Exception):
            persist_note(conn, uid, refusal)
        yield {"type": "note", "text": refusal}
        yield {"type": "done"}
        return
    if actions is None:
        # Plain conversational turn: nothing applied.
        yield {"type": "note", "text": text.strip()}
        yield {"type": "done"}
        return

    if turn.kind == "negotiation":
        yield_from = _negotiation_events(conn, uid, actions)
        for event in yield_from:
            yield event
        return

    ops, refusal = _validate_actions(actions)
    if refusal is not None:
        with contextlib.suppress(Exception):
            persist_note(conn, uid, refusal)
        yield {"type": "note", "text": refusal}
        yield {"type": "done"}
        return
    if ops is None:  # pragma: no cover - _validate_actions returns ops or a refusal
        yield {"type": "done"}
        return

    proposal_ops = [op for op in ops if op["op"] in ("set_windows", "set_events")]
    if proposal_ops:
        kind, items, errors = _normalize_proposal(ops)
        if errors:
            note = "I couldn't use that as-is — nothing was saved: " + "; ".join(errors)
            with contextlib.suppress(Exception):
                persist_note(conn, uid, note)
            yield {"type": "note", "text": note}
            yield {"type": "done"}
            return
        pending_id = _store_pending(conn, uid, kind, ops, items, h0)
        if pending_id is None:  # the account went away mid-turn: nothing is stored
            yield {"type": "done"}
            return
        yield {"type": "preview", "id": pending_id, "kind": kind, "items": items}
        yield {"type": "state", "state": state_summary(conn, uid)}
        yield {"type": "done"}
        return

    applied, conflict = _apply_immediate(conn, uid, ops, h0)
    if conflict is not None:
        with contextlib.suppress(Exception):
            persist_note(conn, uid, f"nothing applied: {conflict}")
        yield {"type": "conflict", "detail": conflict}
        yield {"type": "done"}
        return
    yield {"type": "actions", "applied": applied}
    yield {"type": "state", "state": state_summary(conn, uid)}
    yield {"type": "done"}


def _apply_immediate(conn, uid: int, ops: list[dict], h0: str) -> tuple[list[dict], str | None]:
    """All-immediate batch: one transaction, liveness + hash recheck, one chat-mode write."""
    real_ops = [op for op in ops if op["op"] != "none"]
    if not real_ops:
        return [], None
    applied: list[dict] = []
    conflict: str | None = None
    with db.immediate(conn):
        if not _user_alive(conn, uid):
            conflict = "your account is no longer available — nothing was saved"
        elif current_state_hash(conn, uid) != h0:
            conflict = "your state changed while I was thinking — ask me again"
        else:
            try:
                payload = _merge_ops(conn, uid, real_ops)
            except ValidationError as exc:
                conflict = f"that didn't validate: {_first_error(exc)} — nothing was saved"
            else:
                statewrite.write_state(conn, uid, payload, mode="chat")
                for op in real_ops:
                    summary = _op_summary(op)
                    applied.append({"op": op["op"], "summary": summary})
                    persist_note(conn, uid, summary)
    if conflict is not None:
        return [], conflict
    return applied, None


def _negotiation_events(conn, uid: int, actions: list[Any]):
    """Synchronous generator of the negotiation branch's events (keeps run_turn readable)."""
    op, refusal = _parse_negotiation_ops(actions)
    if refusal is not None or op is None:
        with contextlib.suppress(Exception):
            persist_note(conn, uid, refusal or "nothing applied")
        yield {"type": "note", "text": refusal or "nothing applied"}
        yield {"type": "done"}
        return
    try:
        result = _apply_negotiation_op(conn, uid, op)
    except _Drop as drop:
        note = str(drop)
        with contextlib.suppress(Exception):
            persist_note(conn, uid, note)
        yield {"type": "note", "text": note}
        yield {"type": "done"}
        return
    decided = op["op"]
    summary = _negotiation_summary(decided, result)
    committed = (result.get("_tag"), result.get("_url"), result.get("_reason"))
    with contextlib.suppress(Exception):
        persist_note(conn, uid, _structured_note("your assistant", decided, *committed))
    candidate_id = result.get("_candidate_id") or op.get("candidate_id")
    with contextlib.suppress(Exception):
        if candidate_id is not None:
            cand = db.one(conn, "SELECT * FROM candidates WHERE id=?", (int(candidate_id),))
            if cand is not None:
                other = counterpart(cand, uid)
                if _user_alive(conn, other):
                    persist_note(conn, other, _structured_note("they", decided, *committed))
    yield {"type": "actions", "applied": [{"op": decided, "summary": summary}]}
    yield {"type": "done"}


# --------------------------------------------------------------------- request hardening


def _origin_error(request: Request) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if not origin:
        return None
    parts = urlsplit(config.base_url())
    expected = f"{parts.scheme}://{parts.netloc}"
    if origin.rstrip("/") != expected:
        return _error(403, "origin_forbidden", "cross-origin requests are not allowed")
    return None


async def _read_json(request: Request) -> tuple[Any, JSONResponse | None]:
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return None, _forbidden("Content-Type must be application/json")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None, _error(413, "too_large", "request body is larger than 64 KiB")
    try:
        return json.loads(body), None
    except Exception:
        return None, _forbidden("request body must be a JSON object")


# --------------------------------------------------------------------- endpoints


@router.get("/chat/history")
def chat_history(user=Depends(require_session), conn=Depends(db.get_conn)):
    uid = user["id"]
    rows = db.q(
        conn,
        "SELECT role, content, created_at FROM chat_messages WHERE user_id=?"
        " ORDER BY id DESC LIMIT ?",
        (uid, MAX_HISTORY),
    )
    messages = [
        {"role": row["role"], "content": row["content"], "created_at": row["created_at"]}
        for row in reversed(rows)
    ]
    pending_row = _latest_pending(conn, uid)
    pending = None
    if pending_row is not None:
        try:
            items = json.loads(pending_row["payload_json"]).get("items")
        except Exception:
            items = None
        if not isinstance(items, dict):  # a row from before F13 (or a foreign writer)
            items = {"windows": [], "events": []}
        pending = {
            "id": pending_row["id"],
            "kind": pending_row["kind"],
            "items": items,
            "expires_at": pending_row["expires_at"],
            "state_hash": pending_row["state_hash"],
        }
    return {"messages": messages, "state": state_summary(conn, uid), "pending": pending}


@router.post("/chat")
async def post_chat(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    body, err = await _read_json(request)
    if err is not None:
        return err
    if not isinstance(body, dict):
        return _forbidden("request body must be a JSON object")
    if set(body) - {"content"}:
        return _forbidden("unknown fields in the request body")
    content = body.get("content")
    if not isinstance(content, str):
        return _forbidden("content must be a string")
    if len(content) > MAX_CONTENT_CHARS:
        return _forbidden(f"content is {len(content)} chars; max {MAX_CONTENT_CHARS}")

    uid = user["id"]
    if config.ai_api_url() is None:
        return _error(503, "ai_unconfigured", "chat is not configured on this server")
    bad_origin = _origin_error(request)
    if bad_origin is not None:
        return bad_origin
    try:
        reserve_turn(conn, uid, "chat")
    except ChatRateLimited:
        return _error(429, "rate_limited", "at most 20 turns per 10 min")
    try:
        lock = await _acquire_turn_lock(uid)
    except TurnBusy:
        return _error(409, "turn_busy", "a turn is already running — wait for it to finish")

    turn = Turn(kind="user", user_text=content)
    return StreamingResponse(
        _stream(conn, uid, turn, lock),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


async def _stream(conn, uid: int, turn: Turn, lock: asyncio.Lock):
    try:
        async for event in run_turn(conn, uid, turn):
            yield _sse(event)
    except Exception as exc:  # persistence/DB trouble: honest note, never a broken stream
        detail = _redact(str(exc))
        with contextlib.suppress(Exception):
            persist_note(conn, uid, f"the turn failed: {detail}")
        yield _sse({"type": "error", "code": "internal", "detail": detail})
        yield _sse({"type": "done"})
    finally:
        lock.release()


class _ConfirmError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@router.post("/chat/confirm")
async def chat_confirm(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    body, err = await _read_json(request)
    if err is not None:
        return err
    if not isinstance(body, dict) or set(body) - {"id", "cancel"}:
        return _forbidden("request body must be {\"id\": int} or {\"id\": int, \"cancel\": true}")
    pending_id = body.get("id")
    if isinstance(pending_id, bool) or not isinstance(pending_id, int):
        return _forbidden("id must be an integer")
    cancel = body.get("cancel", False)
    if not isinstance(cancel, bool):
        return _forbidden("cancel must be true or false")
    bad_origin = _origin_error(request)
    if bad_origin is not None:
        return bad_origin

    uid = user["id"]
    now = db.utcnow()
    confirmations: list[dict] = []
    outcome: _ConfirmError | None = None
    with db.immediate(conn):
        row = db.one(
            conn,
            "SELECT * FROM chat_pending WHERE id=? AND user_id=?",
            (pending_id, uid),
        )
        if row is None:
            outcome = _ConfirmError(404, "not_found", "no such preview")
        elif cancel:
            conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
            outcome = _ConfirmError(200, "cancelled", "")
        elif row["expires_at"] < now:
            conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
            outcome = _ConfirmError(409, "expired", "that preview expired — ask again")
        else:
            outcome = _confirm_apply(conn, uid, int(pending_id), row, now, confirmations)
    # The transaction has COMMITTED (or rolled back for a real fault) before any response is
    # built: a cancellation, expiry or conflict is a committed, durable outcome.
    if outcome is not None:
        if outcome.code == "cancelled":
            return {"ok": True, "cancelled": True}
        return _error(outcome.status, outcome.code, outcome.detail)

    return {"ok": True, "state": state_summary(conn, uid), "confirmations": confirmations}


def _confirm_apply(
    conn, uid: int, pending_id: int, row, now: str, confirmations: list[dict]
) -> _ConfirmError | None:
    """Apply one preview inside the caller's transaction. Returns an outcome; NEVER raises.

    Gate review P1 #10: raising through `db.immediate` rolls the transaction back, so the old
    code answered "cancelled"/"expired"/"conflict" while the row survived — still confirmable —
    and riders stayed queued (PLAN §B: a dead batch is deleted, re-asking is the recovery).
    The response is mapped by the caller AFTER the commit.
    """

    def dead(status: int, code: str, detail: str) -> _ConfirmError:
        conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
        return _ConfirmError(status, code, detail)

    if row["kind"] == "ics":
        # An ICS preview belongs to the schedule surface: that row is R2's to confirm or
        # discard, so chat refuses WITHOUT deleting it.
        return _ConfirmError(
            422, "payload_forbidden", "preview belongs to /api/schedule/ics/confirm"
        )
    if row["state_hash"] != current_state_hash(conn, uid):
        return dead(409, "conflict", "your state changed since the preview — ask again")
    try:
        stored = json.loads(row["payload_json"])
        ops = stored.get("ops") or []
    except Exception:
        return dead(409, "conflict", "that preview is unreadable — ask again")
    batch_ops, refusal = _validate_actions(ops)
    if refusal is not None or batch_ops is None:
        return dead(409, "conflict", refusal or "that preview is no longer valid — ask again")
    _, sections, errors = _normalize_proposal(batch_ops)
    window_count = sum(len(op["windows"]) for op in batch_ops if op["op"] == "set_windows")
    if window_count > MAX_WINDOWS:
        errors.append(f"at most {MAX_WINDOWS} windows")
    if errors:
        return dead(
            409, "conflict", "that preview is no longer valid — ask again: " + "; ".join(errors)
        )
    has_windows = any(op["op"] == "set_windows" for op in batch_ops)
    has_events = any(op["op"] == "set_events" for op in batch_ops)
    try:
        payload = _merge_ops(
            conn,
            uid,
            batch_ops,
            windows_stamp=now if has_windows else None,
            events_stamp=now if has_events else None,
        )
    except ValidationError as exc:
        return dead(
            409, "conflict", f"that preview is no longer valid — ask again: {_first_error(exc)}"
        )
    if not _user_alive(conn, uid):
        return dead(409, "conflict", "your account is no longer available")
    statewrite.write_state(conn, uid, payload, mode="chat")
    for op in batch_ops:
        if op["op"] == "none":
            continue
        summary = _op_summary(op)
        confirmations.append({"op": op["op"], "summary": summary})
        persist_note(conn, uid, summary)
    _ = sections  # display only: `ops` above is what applied
    conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
    return None


_ALLOWED_OFFER_KEYS = {"action", "candidate_id", "proposal_version", "tag", "url", "offer_id", "reason"}


def _parse_offer_body(body: Any) -> tuple[dict | None, str | None]:
    if not isinstance(body, dict):
        return None, "request body must be a JSON object"
    if set(body) - _ALLOWED_OFFER_KEYS:
        return None, "unknown fields in the request body"
    action = body.get("action")
    if action not in ("offer", "accept", "counter", "pass"):
        return None, "action must be offer, accept, counter or pass"
    candidate_id = body.get("candidate_id")
    if isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
        return None, "candidate_id must be an integer"
    parsed: dict = {"action": action, "candidate_id": candidate_id}
    needed = {"offer": {"proposal_version", "tag", "url"},
              "accept": {"offer_id"},
              "counter": {"proposal_version", "tag", "url", "offer_id"},
              "pass": {"offer_id", "reason"}}[action]
    missing = [key for key in needed if key not in body]
    if missing:
        return None, f"missing fields for {action}: {', '.join(sorted(missing))}"
    if "offer_id" in needed:
        offer_id = body.get("offer_id")
        if isinstance(offer_id, bool) or not isinstance(offer_id, int):
            return None, "offer_id must be an integer"
        parsed["offer_id"] = offer_id
    if "proposal_version" in needed:
        version = body.get("proposal_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return None, "proposal_version must be an integer"
        parsed["proposal_version"] = version
    if "tag" in needed:
        tag = body.get("tag")
        if not isinstance(tag, str) or tag not in TAG_VOCAB:
            return None, "tag must come from the interest vocabularies"
        parsed["tag"] = tag
    if "url" in needed:
        url = body.get("url")
        if not isinstance(url, str):
            return None, "url must be a string"
        # Shape only: the activity core applies the ONE strict url rule (offers.validate_url)
        # inside its own transaction and answers 422 payload_forbidden. No second rule here.
        parsed["url"] = url
    if "reason" in needed:
        reason = body.get("reason")
        if reason not in offers.REASONS:
            return None, f"reason must be one of {', '.join(offers.REASONS)}"
        parsed["reason"] = reason
    return parsed, None


@router.post("/chat/offer-respond")
async def chat_offer_respond(
    request: Request, user=Depends(require_session), conn=Depends(db.get_conn)
):
    """The MANUAL negotiation path: a human click is the consent, no delegation grant needed."""
    body, err = await _read_json(request)
    if err is not None:
        return err
    bad_origin = _origin_error(request)
    if bad_origin is not None:
        return bad_origin
    parsed, refusal = _parse_offer_body(body)
    if refusal is not None:
        return _forbidden(refusal)

    uid = user["id"]
    if parsed is None:  # pragma: no cover - _parse_offer_body returns a dict or a refusal
        return _forbidden("request body must be a JSON object")
    action = parsed["action"]
    try:
        if action == "offer":
            result = activity.propose(
                conn,
                uid,
                parsed["candidate_id"],
                parsed["proposal_version"],
                parsed["tag"],
                parsed["url"],
            )
        else:
            result = activity.respond(
                conn,
                uid,
                parsed["candidate_id"],
                parsed["offer_id"],
                action,
                reason=parsed.get("reason"),
                tag=parsed.get("tag"),
                url=parsed.get("url"),
            )
    except SchedulingError as exc:
        return _error(exc.status, exc.code, exc.detail)

    # Transcripts: one templated fact for the actor, one structured fact for the counterpart.
    # Both are rendered from the COMMITTED row, never from the request body (F2).
    fields = _committed_fields(conn, result)
    committed = (fields["_tag"], fields["_url"], fields["_reason"])
    with db.immediate(conn):
        persist_note(conn, uid, _structured_note("you", action, *committed))
        cand = db.one(conn, "SELECT * FROM candidates WHERE id=?", (parsed["candidate_id"],))
        if cand is not None and _user_alive(conn, counterpart(cand, uid)):
            other = counterpart(cand, uid)
            persist_note(conn, other, _structured_note("they", action, *committed))
    return result


@router.delete("/chat")
async def delete_chat(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    bad_origin = _origin_error(request)
    if bad_origin is not None:
        return bad_origin
    uid = user["id"]
    lock = get_user_lock(uid)
    await lock.acquire()  # block until any in-flight turn finishes
    try:
        with db.immediate(conn):
            conn.execute("DELETE FROM chat_messages WHERE user_id=?", (uid,))
            # A preview belongs to the cleared thread: drop it so a stale Confirm can't apply.
            conn.execute("DELETE FROM chat_pending WHERE user_id=?", (uid,))
            persist_note(conn, uid, "history cleared")
    finally:
        lock.release()
    return {"ok": True}
