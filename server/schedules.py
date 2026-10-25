"""The demo affordance: define availability by hand, or upload a bounded `.ics`
(CONTRACT-chat.md §4, PLAN-chat-frontend.md §D).

Two write paths, both chat-mode:

  * `POST /api/schedule/windows` — the manual form. The body IS the complete new window list
    (replace semantics, PLAN §B); no preview step because the student just typed it. One
    `statewrite(mode="chat")` inside one `db.immediate`, behind the same optimistic state-hash
    check the chat applies (lane B's helpers, imported — never forked).
  * `POST /api/schedule/ics` — raw-body upload → in-memory parse → free/busy arithmetic →
    `chat_pending` row kind='ics' → `POST /api/schedule/ics/confirm` applies the ticked subset.

ICS posture is REJECT-NEVER-IGNORE (PLAN §D): RRULE/RDATE/TZID/PERIOD/DURATION are refused with
422 naming the property and the event index, because a recurring class is busy time we refuse to
pretend we understood. Only UTC (`...Z`), all-day DATE (`YYYYMMDD`, DTEND exclusive per RFC 5545)
and floating local times (interpreted in the supplied profile tz) are accepted. A DST gap or fold
timestamp is refused with `dst_ambiguous` instead of silently shifting the student's day.

The upload never touches disk: `request.stream()` accumulates at most 2 MiB in memory and the
bytes are decoded and parsed there. Only intervals survive — SUMMARY/LOCATION/DESCRIPTION/
ATTENDEE are not parsed forward at all (tests assert no calendar string reaches the database).

Arithmetic note (surprising, load-bearing): all free/busy maths happens in LOCAL wall time, not
in UTC instants. `datetime` subtraction between two aware values carrying the SAME ZoneInfo is
wall-clock subtraction (fold/DST ignored), and a UTC-based 8 h split on a spring-forward day is
9 h of wall time — which the schema's `MAX_WINDOW_HOURS` check would then reject. Splitting in
wall space keeps every emitted window schema-valid, and wall time is what the student reads.
"""
from __future__ import annotations

import datetime
import json
import re
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from server import chat, db, logging as smlog, statewrite

log = smlog.get("schedule")
from server.schema import MAX_WINDOW_HOURS, MAX_WINDOWS, StatePayload, Window
from server.sessions import require_session

router = APIRouter(prefix="/api", tags=["schedule"])

# Upload caps (PLAN §D).
MAX_ICS_BYTES = 2 * 1024 * 1024
MAX_UNFOLDED_LINE = 2 * 1024
MAX_ICS_EVENTS = 500
MAX_NESTING = 8
# Free-window shape.
HORIZON_DAYS = 14
FREE_DAY_START = datetime.time(8, 0)
FREE_DAY_END = datetime.time(22, 0)
MIN_CHUNK = datetime.timedelta(minutes=30)
MAX_CANDIDATES = 40

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"
_LOCAL_FMT = "%Y-%m-%dT%H:%M:%S"

# Property names we refuse outright (never ignored — PLAN §D reject-never-ignore).
_UNSUPPORTED = {
    "RRULE": "RRULE is not supported — export plain events",
    "RDATE": "RDATE is not supported — export plain events",
    "EXRULE": "EXRULE is not supported — export plain events",
    "DURATION": "DURATION is not supported — send DTSTART and DTEND",
}


# --------------------------------------------------------------------- typed refusals


class _Reject(Exception):
    """A refusal that maps straight onto the {"error", "detail"} HTTP shape."""

    def __init__(self, status: int, code: str, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        # Optional machine-readable fields merged into the {"error", "detail"} JSON body.
        self.extra = extra or {}


def _rejected(detail: str) -> _Reject:
    return _Reject(422, "ics_rejected", detail)


# C8: a Google/Outlook export can produce thousands of per-event refusals; the raw
# "; ".join wall is unreadable (and multi-KB). Past this length the detail becomes
# per-property counts + the first three affected event indices, with the structured
# counts also returned so the page can put them in a collapsible <details> block.
_MAX_DETAIL_CHARS = 400
_ERROR_LABEL = re.compile(r"event (\d+): ([A-Z]+)")


def _collapse_errors(errors: list[str]) -> tuple[str, dict | None]:
    unique = list(dict.fromkeys(errors))
    joined = "; ".join(unique)
    if len(joined) <= _MAX_DETAIL_CHARS:
        return joined, None
    counts: dict[str, int] = {}
    indices: list[int] = []
    for message in unique:
        match = _ERROR_LABEL.match(message)
        label = match.group(2) if match else "calendar"
        counts[label] = counts.get(label, 0) + 1
        if match is not None:
            index = int(match.group(1))
            if index not in indices:
                indices.append(index)
    indices.sort()
    first = indices[:3]
    where = ", ".join(str(index) for index in first) if first else "unknown"
    if len(indices) > len(first):
        where += " and others"
    parts = ", ".join(f"{label} ×{count}" for label, count in sorted(counts.items()))
    detail = (
        f"this calendar has {len(unique)} problems in events {where}: {parts}. "
        "Nothing was stored."
    )
    problems = [
        {"property": label, "count": count} for label, count in sorted(counts.items())
    ]
    return detail, {"problems": problems, "indices": first}


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "payload_forbidden", "detail": detail})


def _conflict() -> JSONResponse:
    # Wording pinned by CONTRACT-chat.md §4 / the chat lane's conflict text.
    return JSONResponse(
        status_code=409,
        content={"error": "conflict", "detail": "your state changed while you were editing"},
    )


# --------------------------------------------------------------------- small time helpers


def _utc(stamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(stamp, _UTC_FMT).replace(tzinfo=datetime.UTC)


def _wall(moment: datetime.datetime, zone: ZoneInfo) -> datetime.datetime:
    """Aware UTC moment → naive LOCAL wall time in `zone` (the Window/tz shape)."""
    return moment.astimezone(zone).replace(tzinfo=None)


def _resolve_local(local: datetime.datetime, zone: ZoneInfo) -> tuple[datetime.datetime | None, str]:
    """Naive local wall time → UTC instant, refusing DST gaps and folds.

    Two checks, both required:
      * fold — the naive datetime appears twice (fall-back hour). Comparing the offsets of the
        fold=0 and fold=1 renderings is the only reliable test: naive datetimes compare equal
        regardless of `fold`, so the `dt.replace(fold=0) != dt.replace(fold=1)` spelling of this
        check silently never fires.
      * gap — the naive datetime does not exist (spring-forward hour); detected by round-tripping
        local → UTC → local and requiring the same wall clock back.
    Returns (utc, "") or (None, reason).
    """
    aware0 = local.replace(tzinfo=zone, fold=0)
    aware1 = local.replace(tzinfo=zone, fold=1)
    if aware0.utcoffset() != aware1.utcoffset():
        return None, f"{local.strftime(_LOCAL_FMT)} is ambiguous in {zone.key} (daylight-saving fold)"
    back = aware0.astimezone(datetime.UTC).astimezone(zone).replace(tzinfo=None)
    if back != local:
        return None, f"{local.strftime(_LOCAL_FMT)} does not exist in {zone.key} (daylight-saving gap)"
    return aware0.astimezone(datetime.UTC), ""


# --------------------------------------------------------------------- ICS unfolding / props


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding, with the 2 KiB-per-unfolded-line cap (→ 422).

    Gate review P2 #15: the cap is UTF-8 BYTES and it is enforced INCREMENTALLY. Each
    continuation segment is measured as it arrives and the line is refused the moment the
    running byte width exceeds the limit — the over-long string is never assembled, so a
    folding bomb cannot make us concatenate megabytes before the refusal.
    """
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    width = 0  # UTF-8 bytes in lines[-1], tracked as we grow it
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            segment = line[1:]
            width += len(segment.encode("utf-8"))
            if width > MAX_UNFOLDED_LINE:
                raise _Reject(
                    422,
                    "unfolded_line_too_long",
                    f"an unfolded line is {width} bytes; the limit is {MAX_UNFOLDED_LINE}",
                )
            lines[-1] += segment
            continue
        width = len(line.encode("utf-8"))
        if width > MAX_UNFOLDED_LINE:
            raise _Reject(
                422,
                "unfolded_line_too_long",
                f"an unfolded line is {width} bytes; the limit is {MAX_UNFOLDED_LINE}",
            )
        lines.append(line)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """'DTSTART;TZID=X;VALUE=DATE:20261029' → (name, params, value). Quotes are respected."""
    in_quotes = False
    cut = len(line)
    for pos, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            cut = pos
            break
    head, value = line[:cut], line[cut + 1 :]
    parts = head.split(";")
    name = parts[0].strip().upper()
    params: dict[str, str] = {}
    for chunk in parts[1:]:
        if "=" not in chunk:
            continue
        key, _, raw = chunk.partition("=")
        params[key.strip().upper()] = raw.strip().strip('"').strip().upper()
    return name, params, value.strip()


def _parse_ics_time(
    value: str, zone: ZoneInfo, label: str, errors: list[str], dst_errors: list[str]
) -> datetime.datetime | None:
    """Accept exactly three forms: UTC 'Z', all-day DATE, floating local. Returns UTC."""
    text = value.strip().upper()
    if len(text) == 16 and text[8] == "T" and text.endswith("Z"):
        try:
            local = datetime.datetime.strptime(text[:-1], "%Y%m%dT%H%M%S")
        except ValueError:
            # Gate review P1 #9: an unparseable timestamp is an ERROR, never a dropped event.
            errors.append(f"{label}: value {value!r} is not a valid UTC timestamp")
            return None
        return local.replace(tzinfo=datetime.UTC)
    if len(text) == 15 and text[8] == "T":
        try:
            local = datetime.datetime.strptime(text, "%Y%m%dT%H%M%S")
        except ValueError:
            errors.append(f"{label}: value {value!r} is not a valid local timestamp")
            return None
        resolved, reason = _resolve_local(local, zone)
        if resolved is None:
            dst_errors.append(reason)
        return resolved
    if len(text) == 8 and text.isdigit():
        try:
            local = datetime.datetime.strptime(text, "%Y%m%d")
        except ValueError:
            errors.append(f"{label}: value {value!r} is not a valid calendar date")
            return None
        resolved, reason = _resolve_local(local, zone)
        if resolved is None:
            dst_errors.append(reason)
        return resolved
    errors.append(
        f"{label}: value {value!r} is not a supported form (UTC, all-day DATE, or floating local time)"
    )
    return None


def parse_ics(text: str, tz: str) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Parse into UTC busy intervals. REJECT-NEVER-IGNORE: structure is checked like properties.

    Gate review P1 #9 — malformed input must never masquerade as free time. Three structural
    rules sit alongside the per-property refusals:

      * every VEVENT must carry BOTH a parseable DTSTART and a parseable DTEND; a missing or
        invalid timestamp is a per-event error naming the index and the property;
      * component boundaries must balance: the file is exactly one BEGIN:VCALENDAR ... END:VCALENDAR,
        a VEVENT may not nest inside a VEVENT, an unmatched END is refused, and EOF inside an
        open component (VEVENT, VALARM, anything) is refused as unterminated;
      * a component that is neither VEVENT nor VCALENDAR (VTIMEZONE, VALARM, X-blocks) is
        skipped AS A WHOLE — but an event that references such a definition through TZID is
        still refused by the TZID rule below, which is unchanged.

    A silent drop of an event can therefore never turn into a successful empty-busy preview.
    """
    zone = ZoneInfo(tz)
    lines = _unfold(text)
    errors: list[str] = []
    dst_errors: list[str] = []
    busy: list[tuple[datetime.datetime, datetime.datetime]] = []
    stack: list[str] = []  # component names, innermost last
    skip_depth = 0  # > 0 while inside a component we do not read
    index = 0
    seen_event = 0
    seen_calendar = 0
    current: dict[str, str] | None = None
    start_value: str | None = None
    end_value: str | None = None

    def malformed(detail: str) -> _Reject:
        return _Reject(422, "parse_error", detail)

    def finish_event() -> None:
        if start_value is None or end_value is None:
            missing = [
                prop
                for prop, value in (("DTSTART", start_value), ("DTEND", end_value))
                if value is None
            ]
            errors.append(
                f"event {index}: {' and '.join(missing)} is missing"
                " (DTSTART and DTEND are required together on every event)"
            )
            return
        start = _parse_ics_time(start_value, zone, f"event {index}: DTSTART", errors, dst_errors)
        end = _parse_ics_time(end_value, zone, f"event {index}: DTEND", errors, dst_errors)
        if start is None or end is None:
            return
        if end <= start:
            errors.append(f"event {index}: DTEND must be after DTSTART")
            return
        busy.append((start, end))

    for line in lines:
        if not line.strip():
            continue
        name, params, value = _split_property(line)
        if name == "BEGIN":
            component = value.upper()
            if skip_depth:
                # Inside a skipped component: only its nesting matters, its content never does.
                skip_depth += 1
                stack.append(component)
                continue
            if len(stack) >= MAX_NESTING:
                raise malformed("this calendar nests components too deeply")
            if component == "VEVENT":
                if "VEVENT" in stack:
                    raise malformed(
                        f"VEVENT {seen_event + 1} begins inside another VEVENT — "
                        "nested events cannot be read"
                    )
                if not stack or stack[-1] != "VCALENDAR":
                    raise malformed(
                        "BEGIN:VEVENT appears outside BEGIN:VCALENDAR ... END:VCALENDAR — "
                        "the calendar wrapper is missing or broken"
                    )
                seen_event += 1
                if seen_event > MAX_ICS_EVENTS:
                    raise _Reject(
                        422,
                        "too_many_events",
                        f"this calendar has more than {MAX_ICS_EVENTS} events — export a shorter range",
                    )
                index = seen_event
                current = {}
                start_value = None
                end_value = None
            elif component == "VCALENDAR":
                if stack:
                    raise malformed("BEGIN:VCALENDAR appears inside another component")
                seen_calendar += 1
            else:
                # Not a component we read: skip its whole body (definitions, alarms, x-blocks).
                skip_depth = 1
            stack.append(component)
            continue
        if name == "END":
            component = value.upper()
            if not stack:
                raise malformed(f"END:{component} has no matching BEGIN — the file is unbalanced")
            top = stack.pop()
            if skip_depth:
                skip_depth -= 1
                if top != component:
                    raise malformed(f"END:{component} does not match BEGIN:{top}")
                continue
            if top != component:
                raise malformed(f"END:{component} does not match BEGIN:{top}")
            if component == "VEVENT":
                finish_event()
                current = None
            continue
        if skip_depth or current is None:
            continue
        if "TZID" in params:
            errors.append(
                f"event {index}: TZID is not supported — export plain events in UTC or your local time"
            )
            continue
        if name in _UNSUPPORTED:
            errors.append(f"event {index}: {_UNSUPPORTED[name]}")
            continue
        if params.get("VALUE") == "PERIOD" or (name in ("DTSTART", "DTEND") and "/" in value):
            errors.append(f"event {index}: PERIOD values are not supported — send DTSTART and DTEND")
            continue
        if name == "DTSTART":
            start_value = value
        elif name == "DTEND":
            end_value = value

    if stack:
        top = stack[-1]
        raise malformed(f"the file ends inside BEGIN:{top} — missing END:{top}")
    if seen_calendar == 0:
        raise malformed(
            "no BEGIN:VCALENDAR found — a calendar needs a BEGIN:VCALENDAR ... END:VCALENDAR wrapper"
        )
    if errors:
        detail, extra = _collapse_errors(errors)
        raise _Reject(422, "ics_rejected", detail, extra)
    if dst_errors:
        raise _Reject(422, "dst_ambiguous", "; ".join(dict.fromkeys(dst_errors)))
    if seen_event == 0:
        raise _Reject(422, "parse_error", "no VEVENT blocks found — is this an .ics file?")
    return busy


# --------------------------------------------------------------------- free/busy arithmetic


def _merge(intervals: list[tuple[datetime.datetime, datetime.datetime]]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Sort + merge. Touching intervals merge: back-to-back classes leave no fake gap."""
    ordered = sorted(intervals)
    merged: list[tuple[datetime.datetime, datetime.datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _subtract(
    busy: list[tuple[datetime.datetime, datetime.datetime]],
    start: datetime.datetime,
    end: datetime.datetime,
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """[start, end) minus the (already merged, sorted) busy list."""
    free: list[tuple[datetime.datetime, datetime.datetime]] = []
    cursor = start
    for busy_start, busy_end in busy:
        if busy_end <= cursor:
            continue
        if busy_start >= end:
            break
        if busy_start > cursor:
            free.append((cursor, min(busy_start, end)))
        cursor = max(cursor, busy_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(s, e) for s, e in free if e > s]


def _split(segment: tuple[datetime.datetime, datetime.datetime]) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """Split one free segment into consecutive windows of at most MAX_WINDOW_HOURS.

    A 14 h day minus nothing becomes 08:00-16:00 and 16:00-22:00. A tail shorter than the
    30-minute minimum is dropped rather than padded: the schema forbids a 9 h window, and a
    25-minute window is not worth offering.
    """
    start, end = segment
    ceiling = datetime.timedelta(hours=MAX_WINDOW_HOURS)
    pieces: list[tuple[datetime.datetime, datetime.datetime]] = []
    cursor = start
    while end - cursor > ceiling:
        pieces.append((cursor, cursor + ceiling))
        cursor += ceiling
    if end - cursor >= MIN_CHUNK:
        pieces.append((cursor, end))
    return pieces


def free_windows(busy: list[tuple[datetime.datetime, datetime.datetime]], tz: str, now: datetime.datetime) -> list[dict]:
    """08:00–22:00 local per day over the next HORIZON_DAYS, minus busy, split ≤ 8 h."""
    zone = ZoneInfo(tz)
    horizon_end = now + datetime.timedelta(days=HORIZON_DAYS)
    clipped = _merge(
        [(max(s, now), min(e, horizon_end)) for s, e in busy if e > now and s < horizon_end]
    )
    busy_local = [(_wall(s, zone), _wall(e, zone)) for s, e in clipped]
    today = _wall(now, zone).date()
    now_local = _wall(now, zone)
    candidates: list[dict] = []
    for day_index in range(HORIZON_DAYS):
        day = today + datetime.timedelta(days=day_index)
        day_start = datetime.datetime.combine(day, FREE_DAY_START)
        day_end = datetime.datetime.combine(day, FREE_DAY_END)
        start = max(day_start, now_local)
        end = min(day_end, _wall(horizon_end, zone))
        if end - start < MIN_CHUNK:
            continue
        for segment in _subtract(busy_local, start, end):
            for piece in _split(segment):
                if piece[1] - piece[0] < MIN_CHUNK:
                    continue
                candidates.append(
                    {"start": piece[0].strftime(_LOCAL_FMT), "end": piece[1].strftime(_LOCAL_FMT)}
                )
    # Validate every emitted window through the schema: bounds, span, horizon. A candidate that
    # does not validate is dropped here rather than poisoning the preview.
    valid: list[dict] = []
    for item in candidates:
        try:
            Window(start=item["start"], end=item["end"], tz=tz)
        except ValidationError:
            continue
        valid.append(item)
    if len(valid) > MAX_CANDIDATES:
        raise _Reject(
            422,
            "preview_too_large",
            f"this upload would leave {len(valid)} free windows (max {MAX_CANDIDATES}) — "
            "export a shorter range or fewer busy days",
        )
    return valid


# --------------------------------------------------------------------- pending store


def _store_ics_pending(conn, uid: int, items: list[dict], tz: str, state_hash: str) -> int:
    """One `chat_pending` row kind='ics', superseding any previous one for this user.

    Same row shape and same persist discipline as chat._store_pending, plus the `tz` the items
    were authored in — the items themselves are bare local start/end strings.
    """
    now = db.utcnow()
    # F13-ICS: an ICS preview carries `items` only — no `ops` key at all. The schedule surface
    # below is its SOLE consumer (chat/confirm refuses kind='ics' before reading the payload).
    payload = json.dumps({"items": items, "tz": tz})
    with db.immediate(conn):
        # B3: only a previous ICS preview is superseded. A pending chat windows preview
        # (kind='windows') belongs to the chat surface and must survive this upload.
        conn.execute("DELETE FROM chat_pending WHERE user_id=? AND kind='ics'", (uid,))
        cursor = conn.execute(
            "INSERT INTO chat_pending (user_id, kind, payload_json, state_hash, created_at,"
            " expires_at) VALUES (?,?,?,?,?,?)",
            (
                uid,
                "ics",
                payload,
                state_hash,
                now,
                chat._shift(now, minutes=chat.PENDING_TTL_MINUTES),
            ),
        )
        return int(cursor.lastrowid)


def _read_pending_ics(conn, uid: int):
    return db.one(
        conn,
        "SELECT * FROM chat_pending WHERE user_id=? AND kind='ics' ORDER BY id DESC LIMIT 1",
        (uid,),
    )


def _decode_pending(row) -> dict:
    stored = json.loads(row["payload_json"])
    return {
        "id": int(row["id"]),
        "items": list(stored.get("items") or []),
        "tz": stored.get("tz"),
        "expires_at": row["expires_at"],
        "state_hash": row["state_hash"],
    }


# --------------------------------------------------------------------- manual windows


@router.get("/schedule/windows")
def get_windows(user=Depends(require_session), conn=Depends(db.get_conn)):
    """A1: the stored windows, so the page can prefill instead of loading blank."""
    uid = user["id"]
    rows = db.q(
        conn,
        "SELECT starts_at, ends_at, tz FROM availability_windows"
        " WHERE user_id=? ORDER BY starts_at, id",
        (uid,),
    )
    latest = db.one(
        conn,
        "SELECT tz FROM availability_windows WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (uid,),
    )
    # One select controls every row, so render every stored instant in that zone. Converting
    # per-window instead would shift mixed-zone lists on the next Save.
    tz = latest["tz"] if latest is not None else "UTC"
    zone = ZoneInfo(tz)
    windows = [
        {
            "start": _wall(_utc(row["starts_at"]), zone).strftime(_LOCAL_FMT),
            "end": _wall(_utc(row["ends_at"]), zone).strftime(_LOCAL_FMT),
            "tz": tz,
        }
        for row in rows
    ]
    return {"windows": windows, "tz": tz}


@router.post("/schedule/windows")
async def set_windows(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    """Full replacement of the windows field. The body IS the complete new list (PLAN §B)."""
    uid = user["id"]
    captured = chat.current_state_hash(conn, uid)  # captured BEFORE the request is parsed
    bad_origin = chat._origin_error(request)
    if bad_origin is not None:
        return bad_origin
    body, err = await chat._read_json(request)
    if err is not None:
        return err
    if not isinstance(body, dict) or set(body) - {"tz", "windows"}:
        return _forbidden('request body must be {"tz": "IANA/Zone", "windows": [{start, end}...]}')
    tz = body.get("tz")
    raw_windows = body.get("windows")
    if not isinstance(tz, str) or not tz.strip():
        return _forbidden("tz is required (an IANA timezone name)")
    if not isinstance(raw_windows, list):
        return _forbidden("windows must be a list")
    if len(raw_windows) > MAX_WINDOWS:
        return _forbidden(f"at most {MAX_WINDOWS} windows allowed, got {len(raw_windows)}")

    windows: list[Window] = []
    for position, raw in enumerate(raw_windows):
        if not isinstance(raw, dict) or set(raw) - {"start", "end"}:
            return _forbidden(f"window {position + 1}: expected exactly start and end")
        start, end = raw.get("start"), raw.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            return _forbidden(f"window {position + 1}: start and end must be local datetime strings")
        try:
            windows.append(Window(start=start, end=end, tz=tz))
        except ValidationError as exc:
            return _forbidden(f"window {position + 1}: {chat._first_error(exc)}")

    now = db.utcnow()
    try:
        with db.immediate(conn):
            if not chat._user_alive(conn, uid):
                return JSONResponse(
                    status_code=409,
                    content={"error": "conflict", "detail": "your account is no longer available"},
                )
            if chat.current_state_hash(conn, uid) != captured:
                return _conflict()
            base = chat.build_current_payload(conn, uid).model_dump(mode="json")
            base["windows"] = [window.model_dump(mode="json") for window in windows]
            base["free_time"]["student_confirmed_at"] = now  # the Save click is the provenance
            payload = StatePayload.model_validate(base)
            result = statewrite.write_state(conn, uid, payload, mode="chat")
            chat.persist_note(conn, uid, f"windows updated: {len(payload.windows)} stored")
            log.info(
                "manual_windows_saved user=%s windows=%s changed=%s",
                uid, len(payload.windows), result.get("changed"),
            )
    except ValidationError as exc:
        return _forbidden(f"those windows cannot be stored: {chat._first_error(exc)}")

    return {
        "ok": True,
        "state": chat.state_summary(conn, uid),
        # B8: the page re-renders its rows from what the server actually stored.
        "windows": [window.model_dump(mode="json") for window in windows],
        "tz": tz,
    }


# --------------------------------------------------------------------- ICS upload


async def _read_raw_body(request: Request) -> bytes:
    """Stream the upload into memory with a running 2 MiB cap. Nothing is ever spooled."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_ICS_BYTES:
            raise _Reject(413, "too_large", "the upload is larger than 2 MiB")
    return bytes(body)


def _tz_param(request: Request) -> tuple[str, JSONResponse | None]:
    """`?tz=IANA/Zone` is required on the upload — floating times mean nothing without it."""
    tz = (request.query_params.get("tz") or "").strip()
    if not tz:
        return "", _forbidden("tz query parameter is required (an IANA timezone name)")
    try:
        ZoneInfo(tz)
    except Exception:
        return "", _forbidden(f"tz is not a known IANA timezone: {tz!r}")
    return tz, None


@router.post("/schedule/ics")
async def upload_ics(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    """Raw body → in-memory parse → preview candidate windows in a pending row."""
    uid = user["id"]
    captured = chat.current_state_hash(conn, uid)
    bad_origin = chat._origin_error(request)
    if bad_origin is not None:
        return bad_origin
    tz, tz_error = _tz_param(request)
    if tz_error is not None:
        return tz_error
    try:
        raw = await _read_raw_body(request)
        if not raw:
            raise _Reject(422, "parse_error", "the upload was empty — is this an .ics file?")
        text = raw.decode("utf-8", errors="replace")
        busy = parse_ics(text, tz)
        items = free_windows(busy, tz, _utc(db.utcnow()))
    except _Reject as exc:
        content = {"error": exc.code, "detail": exc.detail}
        content.update(exc.extra)
        return JSONResponse(status_code=exc.status, content=content)

    pending_id = _store_ics_pending(conn, uid, items, tz, captured)
    return {
        "id": pending_id,
        "items": items,
        "tz": tz,
        "expires_at": db.one(conn, "SELECT expires_at FROM chat_pending WHERE id=?", (pending_id,))[
            "expires_at"
        ],
    }


@router.get("/schedule/pending")
def get_pending(user=Depends(require_session), conn=Depends(db.get_conn)):
    """The unsaved preview, so a page refresh does not lose the checkboxes."""
    uid = user["id"]
    row = _read_pending_ics(conn, uid)
    if row is None or row["expires_at"] < db.utcnow():
        return {"items": [], "tz": None, "id": None, "expires_at": None}
    stored = _decode_pending(row)
    return {
        "id": stored["id"],
        "items": stored["items"],
        "tz": stored["tz"],
        "expires_at": stored["expires_at"],
    }


# --------------------------------------------------------------------- ICS confirm


@router.post("/schedule/ics/confirm")
async def confirm_ics(request: Request, user=Depends(require_session), conn=Depends(db.get_conn)):
    """Apply the ticked subset of an upload preview. Empty `pick` cancels and applies nothing.

    Gate review P1 #4: the optimistic check compares the CURRENT state hash against the hash the
    PENDING ROW captured at upload time — not against a hash recomputed at confirm entry (the old
    code compared a hash with itself, so an upload-then-edit-then-confirm silently overwrote the
    newer windows). Gate review P1 #10: every terminating outcome is computed INSIDE the
    transaction and mapped to a response after it COMMITS — nothing raises through
    `db.immediate`, so the stale-row deletion a conflict promises actually happens.
    """
    uid = user["id"]
    bad_origin = chat._origin_error(request)
    if bad_origin is not None:
        return bad_origin
    body, err = await chat._read_json(request)
    if err is not None:
        return err
    if not isinstance(body, dict) or set(body) - {"id", "pick"}:
        return _forbidden('request body must be {"id": int, "pick": [indices]}')
    pending_id = body.get("id")
    if isinstance(pending_id, bool) or not isinstance(pending_id, int):
        return _forbidden("id must be an integer")
    pick = body.get("pick")
    if not isinstance(pick, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in pick):
        return _forbidden("pick must be a list of item indices")
    if len(set(pick)) != len(pick):
        return _forbidden("pick must not repeat an index")
    if len(pick) > MAX_WINDOWS:
        return _forbidden(f"at most {MAX_WINDOWS} windows can be confirmed, got {len(pick)}")

    now = db.utcnow()
    picked: list[Window] = []
    outcome: _Reject | None = None  # set for every non-applying ending; mapped after COMMIT
    with db.immediate(conn):
        row = db.one(conn, "SELECT * FROM chat_pending WHERE id=? AND user_id=?", (pending_id, uid))
        if row is None:
            outcome = _Reject(404, "not_found", "no such preview")
        elif row["expires_at"] < now:
            # TTL expiry: the row is dead, delete it for real (the commit below lands it).
            conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
            outcome = _Reject(409, "expired", "that preview expired — upload the file again")
        else:
            try:
                stored = _decode_pending(row)
            except (TypeError, ValueError):
                stored = None
            if stored is None or not isinstance(stored["tz"], str):
                conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
                outcome = _Reject(
                    409, "conflict", "that preview is unreadable — upload the file again"
                )
            elif chat.current_state_hash(conn, uid) != stored["state_hash"]:
                # F4: compare the STORED hash captured at upload against the state NOW. The
                # import is stale — PLAN §B says the batch is dead, so the delete must commit
                # (a raise here would roll it back and leave the stale preview confirmable).
                conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
                outcome = _Reject(409, "conflict", "your state changed while you were editing")
            elif not pick:
                conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
                outcome = _Reject(200, "cancelled", "")
            else:
                items, stored_tz = stored["items"], stored["tz"]
                for position in pick:
                    if position < 0 or position >= len(items):
                        outcome = _Reject(
                            422, "payload_forbidden", f"pick index {position} is out of range"
                        )
                        break
                if outcome is None:
                    for position in sorted(pick):
                        item = items[position]
                        start, end = item.get("start"), item.get("end")
                        if not isinstance(start, str) or not isinstance(end, str):
                            outcome = _Reject(
                                422, "payload_forbidden", f"window {position + 1} is unreadable"
                            )
                            break
                        try:
                            picked.append(Window(start=start, end=end, tz=stored_tz))
                        except ValidationError as exc:
                            outcome = _Reject(
                                422,
                                "payload_forbidden",
                                f"window {position + 1} is no longer valid: {chat._first_error(exc)}",
                            )
                            break
                if outcome is None and not chat._user_alive(conn, uid):
                    outcome = _Reject(409, "conflict", "your account is no longer available")
                if outcome is None:
                    base = chat.build_current_payload(conn, uid).model_dump(mode="json")
                    base["windows"] = [window.model_dump(mode="json") for window in picked]
                    base["free_time"]["student_confirmed_at"] = now  # the Confirm click is the provenance
                    try:
                        payload = StatePayload.model_validate(base)
                        statewrite.write_state(conn, uid, payload, mode="chat")
                    except ValidationError as exc:
                        outcome = _Reject(
                            422,
                            "payload_forbidden",
                            f"those windows cannot be stored: {chat._first_error(exc)}",
                        )
                    else:
                        conn.execute("DELETE FROM chat_pending WHERE id=?", (pending_id,))
                        chat.persist_note(conn, uid, f"windows updated: {len(payload.windows)} stored")

    if outcome is not None:
        if outcome.code == "cancelled":
            return {"ok": True, "cancelled": True, "state": chat.state_summary(conn, uid)}
        return JSONResponse(
            status_code=outcome.status, content={"error": outcome.code, "detail": outcome.detail}
        )
    return {"ok": True, "state": chat.state_summary(conn, uid)}


__all__ = ["router", "parse_ics", "free_windows", "MAX_ICS_BYTES", "MAX_CANDIDATES", "HORIZON_DAYS"]
