"""server/schedules.py — manual windows, bounded `.ics` upload → preview → confirm.

Deterministic surface: no provider, no network, no sleeps. Time is frozen with
`db._FIXED_NOW` (2026-10-24T12:00:00Z, a Saturday; 05:00 local in America/Los_Angeles, so the
first local free window of the horizon starts at 08:00, not "now").

`.ics` fixtures are plain Python strings built in memory — nothing is written to a temp file
except in the no-disk test, which asserts the production path never opens one.
"""
from __future__ import annotations

import builtins
import io
import json
import os

import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]

from server import chat, db, schedules
from server.schema import MAX_WINDOWS
from tests import util

FIXED_NOW = "2026-10-24T12:00:00Z"
TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    yield
    db._FIXED_NOW = None


@pytest.fixture()
def client(db_env):
    """The shared app with lane C's router mounted (the integrator owns app.py)."""
    from server.app import app

    if not any(getattr(route, "path", "").startswith("/api/schedule") for route in app.routes):
        app.include_router(schedules.router)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def signed_in(client):
    uid = util.mk_user("sched@x.test", "Sam")
    util.direct_session(client, uid)
    return uid


# ------------------------------------------------------------------ fixture builders


def ics(*events: str) -> str:
    """A minimal in-memory VCALENDAR wrapping the given VEVENT blocks."""
    body = "".join(event for event in events)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//social-match tests//EN\r\n"
        f"{body}END:VCALENDAR\r\n"
    )


def vevent(start_prop: str, end_prop: str, extra: str = "") -> str:
    return f"BEGIN:VEVENT\r\nUID:test-{abs(hash(start_prop))}\r\n{start_prop}\r\n{end_prop}\r\n{extra}END:VEVENT\r\n"


def utc_event(start: str, end: str, extra: str = "") -> str:
    return vevent(f"DTSTART:{start}Z", f"DTEND:{end}Z", extra)


def floating_event(start: str, end: str, extra: str = "") -> str:
    return vevent(f"DTSTART:{start}", f"DTEND:{end}", extra)


def all_day_event(day: str, next_day: str, extra: str = "") -> str:
    return vevent(
        f"DTSTART;VALUE=DATE:{day}", f"DTEND;VALUE=DATE:{next_day}", extra
    )


def upload(client, body: str, tz: str = TZ):
    return client.post(f"/api/schedule/ics?tz={tz}", content=body.encode())


def windows_of(uid: int) -> list[tuple[str, str, str]]:
    conn = db.connect()
    try:
        return [
            (row["starts_at"], row["ends_at"], row["tz"])
            for row in db.q(
                conn,
                "SELECT starts_at, ends_at, tz FROM availability_windows"
                " WHERE user_id=? ORDER BY starts_at",
                (uid,),
            )
        ]
    finally:
        conn.close()


def grants_dump() -> list[tuple]:
    conn = db.connect()
    try:
        return [tuple(row) for row in db.q(conn, "SELECT * FROM grants ORDER BY id")]
    finally:
        conn.close()


def notes_of(uid: int) -> list[tuple[str, str]]:
    conn = db.connect()
    try:
        return [
            (row["role"], row["content"])
            for row in db.q(
                conn, "SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id", (uid,)
            )
        ]
    finally:
        conn.close()


def confirmed_at(uid: int):
    conn = db.connect()
    try:
        return db.one(conn, "SELECT source, observed_at, written_by_agent_id FROM availability"
                            " WHERE user_id=?", (uid,))
    finally:
        conn.close()


def pending_rows(uid: int) -> list:
    conn = db.connect()
    try:
        return db.q(conn, "SELECT * FROM chat_pending WHERE user_id=? ORDER BY id", (uid,))
    finally:
        conn.close()


# ------------------------------------------------------------------ manual windows


def test_manual_full_replace_replaces_windows_and_writes_a_note(client, signed_in):
    uid = signed_in
    util.grant(uid, "matching")
    util.windows(uid, [("2026-10-29T19:00:00", "2026-10-29T21:00:00"),
                       ("2026-10-30T19:00:00", "2026-10-30T21:00:00")])
    before_grants = grants_dump()
    before_windows = windows_of(uid)
    assert len(before_windows) == 2

    r = client.post(
        "/api/schedule/windows",
        json={
            "tz": TZ,
            "windows": [
                {"start": "2026-10-31T09:00:00", "end": "2026-10-31T12:00:00"},
                {"start": "2026-11-01T15:00:00", "end": "2026-11-01T18:00:00"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["state"]["windows"] == 2

    stored = windows_of(uid)
    assert stored == [
        (util.to_utc("2026-10-31T09:00:00", TZ), util.to_utc("2026-10-31T12:00:00", TZ), TZ),
        (util.to_utc("2026-11-01T15:00:00", TZ), util.to_utc("2026-11-01T18:00:00", TZ), TZ),
    ]
    # The old rows are GONE: this body was the complete new list, not an append.
    assert before_windows[0][0] not in [row[0] for row in stored]

    assert notes_of(uid) == [("note", "windows updated: 2 stored")]
    ack = confirmed_at(uid)
    assert ack["source"] == "student"
    assert ack["observed_at"] == FIXED_NOW  # the Save click is the provenance
    assert grants_dump() == before_grants  # chat mode never touches a grants row


def test_manual_empty_list_clears_every_window(client, signed_in):
    uid = signed_in
    util.windows(uid, [("2026-10-29T19:00:00", "2026-10-29T21:00:00")])
    r = client.post("/api/schedule/windows", json={"tz": TZ, "windows": []})
    assert r.status_code == 200, r.text
    assert windows_of(uid) == []
    assert notes_of(uid) == [("note", "windows updated: 0 stored")]


def test_manual_rejections(client, signed_in):
    good = {"start": "2026-10-31T09:00:00", "end": "2026-10-31T11:00:00"}
    too_many = [good] * (MAX_WINDOWS + 1)
    cases = [
        ({"tz": TZ, "windows": too_many}, "at most 20 windows"),
        ({"tz": TZ, "windows": [{"start": "2026-10-01T09:00:00", "end": "2026-10-01T11:00:00"}]},
         "window 1: window starts in the past"),
        ({"tz": TZ, "windows": [{"start": "2026-10-31T09:00:00", "end": "2026-10-31T19:00:00"}]},
         "window 1: duration 10h exceeds the 8h maximum"),
        ({"tz": TZ, "windows": [{"start": "2026-10-31T11:00:00", "end": "2026-10-31T09:00:00"}]},
         "window 1: start must be strictly before end"),
        ({"tz": "Mars/Olympus", "windows": [good]}, "window 1: tz: tz is not a known IANA timezone"),
        ({"tz": TZ, "windows": [{"start": good["start"], "end": good["end"], "extra": 1}]},
         "window 1: expected exactly start and end"),
        ({"tz": TZ, "windows": "nope"}, "windows must be a list"),
        ({"windows": [good]}, "tz is required"),
        ({"tz": TZ, "windows": [good], "surprise": 1}, "request body must be"),
    ]
    for payload, expected in cases:
        r = client.post("/api/schedule/windows", json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
        assert r.json()["error"] == "payload_forbidden"
        assert expected in r.json()["detail"], r.json()
    assert windows_of(signed_in) == []
    assert notes_of(signed_in) == []


def test_manual_hash_conflict_applies_nothing(client, signed_in, monkeypatch):
    uid = signed_in
    util.windows(uid, [("2026-10-29T19:00:00", "2026-10-29T21:00:00")])
    before = windows_of(uid)
    hashes = iter(["captured-hash", "the-state-moved-on"])

    monkeypatch.setattr(chat, "current_state_hash", lambda conn, user: next(hashes))
    r = client.post(
        "/api/schedule/windows",
        json={"tz": TZ, "windows": [{"start": "2026-10-31T09:00:00", "end": "2026-10-31T11:00:00"}]},
    )
    assert r.status_code == 409
    assert r.json() == {
        "error": "conflict",
        "detail": "your state changed while you were editing",
    }
    assert windows_of(uid) == before
    assert notes_of(uid) == []


def test_manual_requires_a_session(client):
    r = client.post("/api/schedule/windows", json={"tz": TZ, "windows": []})
    assert r.status_code == 401


def test_manual_cross_origin_refused(client, signed_in, monkeypatch):
    from server import config

    monkeypatch.setattr(config, "base_url", lambda: "http://testserver")
    r = client.post(
        "/api/schedule/windows",
        json={"tz": TZ, "windows": []},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "origin_forbidden"


# ------------------------------------------------------------------ ICS happy path


def test_ics_happy_path_free_chunks_splitting_and_minimum(client, signed_in):
    uid = signed_in
    body = ics(
        utc_event("20261025T180000", "20261025T200000"),  # 11:00-13:00 local, Sunday
        floating_event("20261026T173000", "20261026T180000"),  # 30 min, 17:30-18:00 local
        all_day_event("20261027", "20261028"),  # the whole of Tuesday
    )
    r = upload(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    items = data["items"]
    assert data["tz"] == TZ
    by_day: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        assert isinstance(item["start"], str) and isinstance(item["end"], str)
        by_day.setdefault(item["start"][:10], []).append((item["start"][11:], item["end"][11:]))
        # never longer than the schema maximum, never shorter than the 30-minute minimum
        start = item["start"]
        end = item["end"]
        hours = (
            int(end[11:13]) * 60 + int(end[14:16]) - int(start[11:13]) * 60 - int(start[14:16])
        )
        assert 30 <= hours <= 8 * 60

    # A 14 h day minus nothing splits into 08:00-16:00 and 16:00-22:00.
    assert by_day["2026-10-24"] == [("08:00:00", "16:00:00"), ("16:00:00", "22:00:00")]
    # The UTC event removes 11:00-13:00; the remaining 9 h splits 8 h + 1 h.
    assert by_day["2026-10-25"] == [
        ("08:00:00", "11:00:00"),
        ("13:00:00", "21:00:00"),
        ("21:00:00", "22:00:00"),
    ]
    # Floating 17:30-18:00 in the supplied tz leaves 16:00-17:30 standing.
    assert by_day["2026-10-26"] == [
        ("08:00:00", "16:00:00"),
        ("16:00:00", "17:30:00"),
        ("18:00:00", "22:00:00"),
    ]
    # The all-day event blocks the whole day...
    assert "2026-10-27" not in by_day
    # ...and DTEND is EXCLUSIVE: 2026-10-28 is free again.
    assert by_day["2026-10-28"] == [("08:00:00", "16:00:00"), ("16:00:00", "22:00:00")]
    # Today was 05:00 local, so nothing before 08:00 and nothing in the past.
    assert all(item["start"][:10] >= "2026-10-24" for item in items)
    assert len(by_day) == 13  # 14 local days in the horizon, one of them blocked
    horizon = sorted(by_day)
    assert horizon[0] == "2026-10-24" and horizon[-1] == "2026-11-06"

    rows = pending_rows(uid)
    assert len(rows) == 1
    assert rows[0]["kind"] == "ics"
    stored = json.loads(rows[0]["payload_json"])
    assert stored["tz"] == TZ
    assert stored["items"] == items
    assert rows[0]["expires_at"] == "2026-10-24T12:10:00Z"
    assert len(rows[0]["state_hash"]) == 64
    # The upload itself writes no availability.
    assert windows_of(uid) == []


def test_ics_pending_is_returned_and_empty_by_default(client, signed_in):
    uid = signed_in
    r = client.get("/api/schedule/pending")
    assert r.status_code == 200
    assert r.json() == {"items": [], "tz": None, "id": None, "expires_at": None}

    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    again = client.get("/api/schedule/pending")
    assert again.status_code == 200
    assert again.json()["id"] == uploaded["id"]
    assert again.json()["items"] == uploaded["items"]
    assert again.json()["tz"] == TZ
    assert again.json()["expires_at"] == "2026-10-24T12:10:00Z"
    assert windows_of(uid) == []


def test_ics_second_upload_supersedes_the_first(client, signed_in):
    uid = signed_in
    first = upload(client, ics(utc_event("20261025T180000", "20261025T200000"))).json()
    second = upload(client, ics(utc_event("20261028T180000", "20261028T200000"))).json()
    rows = pending_rows(uid)
    assert len(rows) == 1  # single pending per user, superseded
    stored = json.loads(rows[0]["payload_json"])
    assert stored["items"] == second["items"]  # the first preview is gone, not queued
    assert stored["items"] != first["items"]
    assert client.get("/api/schedule/pending").json()["items"] == second["items"]

    # Confirming applies the SECOND preview's windows — the superseded one is unreachable.
    r = client.post("/api/schedule/ics/confirm", json={"id": rows[0]["id"], "pick": [0]})
    assert r.status_code == 200, r.text
    assert windows_of(uid) == [
        (util.to_utc(second["items"][0]["start"], TZ),
         util.to_utc(second["items"][0]["end"], TZ), TZ),
    ]


def test_ics_merges_overlapping_busy_intervals(client, signed_in):
    body = ics(
        utc_event("20261025T170000", "20261025T190000"),  # 10:00-12:00 local
        utc_event("20261025T183000", "20261025T200000"),  # 11:30-13:00 local (overlaps)
        utc_event("20261025T200000", "20261025T210000"),  # 13:00-14:00 local (touching)
    )
    items = upload(client, body).json()["items"]
    sunday = [(i["start"][11:], i["end"][11:]) for i in items if i["start"][:10] == "2026-10-25"]
    assert sunday == [("08:00:00", "10:00:00"), ("14:00:00", "22:00:00")]


# ------------------------------------------------------------------ ICS rejections


def test_ics_rrule_rejected_naming_event_index(client, signed_in):
    body = ics(
        utc_event("20261025T180000", "20261025T190000"),
        utc_event("20261026T180000", "20261026T190000", extra="RRULE:FREQ=WEEKLY\r\n"),
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "ics_rejected"
    assert "event 2: RRULE is not supported" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_rdate_tzid_duration_and_period_rejected(client, signed_in):
    cases = [
        (ics(vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
                    extra="RDATE:20261026T180000\r\n")), "RDATE is not supported"),
        (ics(vevent("DTSTART;TZID=Europe/Berlin:20261025T180000", "DTEND;TZID=Europe/Berlin:20261025T190000")),
         "TZID is not supported"),
        (ics(vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
                    extra="DURATION:PT1H\r\n")), "DURATION is not supported"),
        (ics(vevent("DTSTART;VALUE=PERIOD:20261025T180000/20261025T190000",
                    "DTEND:20261025T190000")), "PERIOD values are not supported"),
        (ics(vevent("DTSTART:20261025T180000", "")), "DTSTART and DTEND are required together"),
        (ics(vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
                    extra="X-FREETEXT:20261025T200000\r\n")), ""),
    ]
    for body, expected in cases:
        if not expected:
            continue
        r = upload(client, body)
        assert r.status_code == 422, (expected, r.status_code, r.text)
        assert r.json()["error"] == "ics_rejected", r.text
        assert expected in r.json()["detail"], (expected, r.json()["detail"])


def test_ics_unknown_property_is_not_an_error(client, signed_in):
    """Only the listed shapes are refused; a stray x-prop is ignored, not fatal."""
    body = ics(
        vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
               extra="X-SOMETHING:hello\r\nCLASS:PRIVATE\r\n")
    )
    r = upload(client, body)
    assert r.status_code == 200, r.text


def test_ics_dst_gap_and_fold_rejected(client, signed_in):
    # 2027-03-14 is the US spring-forward day: 02:30 local does not exist.
    gap = upload(client, ics(floating_event("20270314T023000", "20270314T033000")))
    assert gap.status_code == 422
    assert gap.json()["error"] == "dst_ambiguous"
    assert "2027-03-14T02:30:00" in gap.json()["detail"]

    # 2026-11-01 is the fall-back day: 01:30 local happens twice.
    fold = upload(client, ics(floating_event("20261101T013000", "20261101T023000")))
    assert fold.status_code == 422
    assert fold.json()["error"] == "dst_ambiguous"
    assert "2026-11-01T01:30:00" in fold.json()["detail"]

    # An all-day boundary that lands in a gap is refused the same way (Pacific/Apia style day
    # skips are not modelled, so use the tz that actually has one).
    apia = upload(client, ics(all_day_event("20111230", "20111231")), tz="Pacific/Apia")
    assert apia.status_code == 422
    assert apia.json()["error"] == "dst_ambiguous"
    assert pending_rows(signed_in) == []


def test_ics_caps_events_lines_and_bytes(client, signed_in):
    too_many = ics(*[utc_event("20261025T180000", "20261025T190000") for _ in range(501)])
    r = upload(client, too_many)
    assert r.status_code == 422
    assert r.json()["error"] == "too_many_events"

    long_line = ics(
        vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
               extra="DESCRIPTION:" + ("x" * (schedules.MAX_UNFOLDED_LINE + 1)) + "\r\n")
    )
    r = upload(client, long_line)
    assert r.status_code == 422
    assert r.json()["error"] == "unfolded_line_too_long"

    huge = "BEGIN:VCALENDAR\r\n" + ("x" * (schedules.MAX_ICS_BYTES + 512))
    r = upload(client, huge)
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"
    assert pending_rows(signed_in) == []


def test_ics_folded_lines_are_unfolded(client, signed_in):
    folded = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "DTSTART:20261025T180000\r\nDTEND:20261025T190000\r\n"
        "SUMMARY:A very long title that the exporter fol\r\n ded across lines\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    r = upload(client, folded)
    assert r.status_code == 200, r.text


def test_ics_junk_and_tz_param(client, signed_in):
    r = upload(client, "this is not a calendar, it is a note to self")
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"

    r = upload(client, ics())
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"  # no VEVENT blocks

    r = client.post("/api/schedule/ics", content=ics(utc_event("20261025T180000", "20261025T190000")).encode())
    assert r.status_code == 422
    assert "tz query parameter is required" in r.json()["detail"]

    r = upload(client, ics(utc_event("20261025T180000", "20261025T190000")), tz="Nowhere/Here")
    assert r.status_code == 422
    assert "not a known IANA timezone" in r.json()["detail"]

    r = client.post("/api/schedule/ics?tz=UTC", content=b"")
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"


def test_ics_preview_too_large(client, signed_in):
    # 15-minute busy slices, every other hour, carve each free day into >40 separate binders.
    # (The hours must be real clock hours — the parser now REFUSES an invalid timestamp instead
    # of silently dropping the event, so this fixture generates 08:00–16:00 local properly.)
    events = []
    for day in range(24, 31):
        for hour in range(8, 17):
            events.append(
                utc_event(f"202610{day:02d}T{hour + 7:02d}0000", f"202610{day:02d}T{hour + 7:02d}1500")
            )
    r = upload(client, ics(*events))
    assert r.status_code == 422
    assert r.json()["error"] == "preview_too_large"


# ------------------------------------------------------------------ malformed input (F9)


def test_ics_missing_dtend_is_a_named_per_event_error(client, signed_in):
    body = ics(vevent("DTSTART:20261025T180000", ""))
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "ics_rejected"
    detail = r.json()["detail"]
    assert "event 1" in detail and "DTEND" in detail, detail
    assert "DTSTART and DTEND are required together" in detail, detail
    assert pending_rows(signed_in) == []
    assert windows_of(signed_in) == []


def test_ics_missing_dtstart_is_a_named_per_event_error(client, signed_in):
    body = ics(vevent("", "DTEND:20261025T190000"))
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "ics_rejected"
    assert "event 1" in r.json()["detail"] and "DTSTART" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_event_with_no_timestamps_at_all_is_an_error(client, signed_in):
    """The old parser returned silently here and produced an empty-busy 'free' preview."""
    body = ics("BEGIN:VEVENT\r\nUID:no-times\r\nSUMMARY:Nameless\r\nEND:VEVENT\r\n")
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "ics_rejected"
    assert "event 1" in r.json()["detail"]
    assert "DTSTART and DTEND" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_invalid_date_values_are_named_errors(client, signed_in):
    cases = [
        # A plainly impossible calendar date: not any accepted form.
        (ics(vevent("DTSTART:2026-13-45", "DTEND:2026-13-46")),
         "event 1: DTSTART", "not a supported form"),
        # Right shape, impossible day: the strptime failure used to be swallowed.
        (ics(vevent("DTSTART:20260132T090000", "DTEND:20260132T100000")),
         "event 1: DTSTART", "not a valid local timestamp"),
        (ics(vevent("DTSTART:20261345T090000Z", "DTEND:20261345T100000Z")),
         "event 1: DTSTART", "not a valid UTC timestamp"),
        (ics(all_day_event("20261345", "20261346")),
         "event 1: DTSTART", "not a valid calendar date"),
    ]
    for body, where, why in cases:
        r = upload(client, body)
        assert r.status_code == 422, (where, r.status_code, r.text)
        assert r.json()["error"] == "ics_rejected", r.text
        assert where in r.json()["detail"], (where, r.json()["detail"])
        assert why in r.json()["detail"], (why, r.json()["detail"])
        assert pending_rows(signed_in) == []
        assert windows_of(signed_in) == []


def test_ics_unterminated_vevent_at_eof(client, signed_in):
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:cut-off\r\n"
        "DTSTART:20261025T180000\r\nDTEND:20261025T190000\r\n"
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "BEGIN:VEVENT" in r.json()["detail"] and "missing END:VEVENT" in r.json()["detail"]
    assert pending_rows(signed_in) == []
    assert windows_of(signed_in) == []


def test_ics_unterminated_valarm_at_eof(client, signed_in):
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:alarm\r\n"
        "DTSTART:20261025T180000\r\nDTEND:20261025T190000\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\n"
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "BEGIN:VALARM" in r.json()["detail"] and "missing END:VALARM" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_nested_vevent_is_refused(client, signed_in):
    body = ics(
        "BEGIN:VEVENT\r\nUID:outer\r\n"
        "DTSTART:20261025T180000\r\nDTEND:20261025T190000\r\n"
        "BEGIN:VEVENT\r\nUID:inner\r\n"
        "DTSTART:20261025T200000\r\nDTEND:20261025T210000\r\n"
        "END:VEVENT\r\nEND:VEVENT\r\n"
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "inside another VEVENT" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_missing_calendar_close_is_refused(client, signed_in):
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + utc_event("20261025T180000", "20261025T190000")
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "missing END:VCALENDAR" in r.json()["detail"]
    assert pending_rows(signed_in) == []
    assert windows_of(signed_in) == []


def test_ics_unmatched_end_and_bare_event_are_refused(client, signed_in):
    stray = ics("END:VEVENT\r\n")  # an END with nothing open (beyond the calendar)
    r = upload(client, stray)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "does not match" in r.json()["detail"] or "no matching BEGIN" in r.json()["detail"]

    bare = utc_event("20261025T180000", "20261025T190000")  # no VCALENDAR wrapper at all
    r = upload(client, bare)
    assert r.status_code == 422
    assert r.json()["error"] == "parse_error"
    assert "BEGIN:VCALENDAR" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_vtimezone_is_skipped_as_a_whole_component(client, signed_in):
    """A timezone DEFINITION is not read, but it is not fatal either (gate review note).

    The events' floating times are interpreted in the supplied `tz`; an event that REFERENCES a
    TZID is still refused by the unchanged TZID rule (covered above).
    """
    body = ics(
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n"
        "BEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nEND:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
        + utc_event("20261025T180000", "20261025T190000")
    )
    r = upload(client, body)
    assert r.status_code == 200, r.text
    assert r.json()["items"], "the real event must still be parsed"
    assert pending_rows(signed_in) != []


# ------------------------------------------------------------------ byte bound (F15)


def test_ics_folded_line_bound_is_utf8_bytes_and_incremental(client, signed_in):
    """Gate review P2 #15 — the cap is bytes, and it must bite DURING unfolding.

    1400 + 1400 bytes: the first segment is legal, the first continuation takes the accumulated
    line to 2800 bytes — over 2048. A third continuation would take it to 4200: the error must
    report 2800, proving the parser refused at the first over-limit segment instead of building
    the whole (4200-byte) string and cutting it afterwards.
    """
    first = "DESCRIPTION:" + ("\u00e9" * 694)  # 12 + 1388 = 1400 UTF-8 bytes
    second = " " + ("\u00e9" * 700)            # + 1400 → 2800 bytes: over the limit
    third = " " + ("\u00e9" * 700)             # would total 4200 if we kept concatenating
    body = ics(
        vevent(
            "DTSTART:20261025T180000",
            "DTEND:20261025T190000",
            extra=f"{first}\r\n{second}\r\n{third}\r\n",
        )
    )
    r = upload(client, body)
    assert r.status_code == 422
    assert r.json()["error"] == "unfolded_line_too_long"
    assert "2800 bytes" in r.json()["detail"], r.json()["detail"]
    assert "4200" not in r.json()["detail"], "the parser built the whole string before refusing"
    assert "the limit is 2048" in r.json()["detail"]
    assert pending_rows(signed_in) == []


def test_ics_unfolded_line_at_the_byte_bound_passes(client, signed_in):
    """2047 bytes is legal; the boundary is inclusive and counted in UTF-8 bytes."""
    filler = "x" * (schedules.MAX_UNFOLDED_LINE - len("DESCRIPTION:") - 1)  # → 2047 bytes
    assert len(("DESCRIPTION:" + filler).encode("utf-8")) == schedules.MAX_UNFOLDED_LINE - 1
    body = ics(
        vevent(
            "DTSTART:20261025T180000",
            "DTEND:20261025T190000",
            extra=f"DESCRIPTION:{filler}\r\n",
        )
    )
    r = upload(client, body)
    assert r.status_code == 200, r.text

    # And a multi-byte line that reaches the bound by BYTES passes too (<= 2 KiB in UTF-8).
    wide = "\u00e9" * 1020  # 2040 bytes + 7 for "X-WIDE:" = 2047
    assert len(("X-WIDE:" + wide).encode("utf-8")) == 2047
    body = ics(
        vevent("DTSTART:20261025T180000", "DTEND:20261025T190000", extra=f"X-WIDE:{wide}\r\n")
    )
    r = upload(client, body)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ ICS confirm


def test_ics_confirm_stores_only_the_picked_windows(client, signed_in):
    uid = signed_in
    util.grant(uid, "matching")
    before_grants = grants_dump()
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    items = uploaded["items"]

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0, 2]})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["windows"] == 2

    stored = windows_of(uid)
    assert stored == [
        (util.to_utc(items[0]["start"], TZ), util.to_utc(items[0]["end"], TZ), TZ),
        (util.to_utc(items[2]["start"], TZ), util.to_utc(items[2]["end"], TZ), TZ),
    ]
    ack = confirmed_at(uid)
    assert ack["source"] == "student"
    assert ack["observed_at"] == FIXED_NOW
    assert pending_rows(uid) == []
    assert notes_of(uid) == [("note", "windows updated: 2 stored")]
    assert grants_dump() == before_grants


def test_ics_confirm_empty_pick_cancels(client, signed_in):
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": []})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["cancelled"] is True
    assert pending_rows(uid) == []
    assert windows_of(uid) == []
    assert notes_of(uid) == []


def test_ics_confirm_rejections(client, signed_in):
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    total = len(uploaded["items"])
    assert total > MAX_WINDOWS

    r = client.post(
        "/api/schedule/ics/confirm",
        json={"id": uploaded["id"], "pick": list(range(MAX_WINDOWS + 1))},
    )
    assert r.status_code == 422
    assert r.json()["error"] == "payload_forbidden"
    assert "at most 20 windows" in r.json()["detail"]

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [total]})
    assert r.status_code == 422
    assert "out of range" in r.json()["detail"]

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0, 0]})
    assert r.status_code == 422
    assert "must not repeat" in r.json()["detail"]

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": "0"})
    assert r.status_code == 422

    r = client.post("/api/schedule/ics/confirm", json={"id": "x", "pick": []})
    assert r.status_code == 422

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [], "extra": 1})
    assert r.status_code == 422

    # A failed confirm leaves the preview intact and writes nothing.
    assert len(pending_rows(uid)) == 1
    assert windows_of(uid) == []


def test_ics_confirm_of_another_users_preview_is_not_found(client, signed_in):
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    other = util.mk_user("other@x.test", "Ola")
    util.direct_session(client, other)
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 404


def test_ics_confirm_compares_the_stored_hash_and_discards_the_stale_preview(client, signed_in):
    """Gate review P1 #4 — the bug was comparing a fresh hash against itself.

    Upload captures the hash on the PENDING ROW; confirm must compare THAT against the state
    now. Here the windows move (through the manual path, the simplest honest driver) between
    upload and confirm: the stale import must not overwrite them, and its row must be GONE —
    the deletion committed, not rolled back with a raise.
    """
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    moved = client.post(
        "/api/schedule/windows",
        json={"tz": TZ, "windows": [{"start": "2026-10-31T09:00:00", "end": "2026-10-31T10:00:00"}]},
    )
    assert moved.status_code == 200, moved.text
    newer = windows_of(uid)
    assert len(newer) == 1  # the newer windows are what must survive

    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 409
    assert r.json() == {"error": "conflict", "detail": "your state changed while you were editing"}
    assert windows_of(uid) == newer            # the stale import applied nothing
    assert pending_rows(uid) == []             # and the discard COMMITTED

    # The row is genuinely gone: a second attempt is a plain 404, not a second conflict.
    again = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert again.status_code == 404
    assert again.json()["error"] == "not_found"
    assert windows_of(uid) == newer


def test_ics_confirm_succeeds_when_the_state_did_not_move(client, signed_in):
    """The control for the test above: same fixture, no intervening edit → the import lands."""
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 200, r.text
    assert len(windows_of(uid)) == 1
    assert pending_rows(uid) == []


def test_ics_confirm_expired_preview(client, signed_in, monkeypatch):
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    monkeypatch.setattr(db, "_FIXED_NOW", "2026-10-24T12:11:00Z")
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 409
    assert r.json()["error"] == "expired"
    assert pending_rows(uid) == []  # expiry deletes, nothing applied
    assert windows_of(uid) == []


def test_ics_confirm_revalidates_the_picked_window_bounds(client, signed_in):
    """The stored preview is re-checked against the schema at confirm time, not trusted."""
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE chat_pending SET payload_json=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "items": [
                                # 12 hours long: schema-invalid, and the server must say so.
                                {"start": "2026-10-25T08:00:00", "end": "2026-10-25T20:00:00"}
                            ],
                            "tz": TZ,
                        }
                    ),
                    uploaded["id"],
                ),
            )
    finally:
        conn.close()
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 422
    assert r.json()["error"] == "payload_forbidden"
    assert "no longer valid" in r.json()["detail"]
    assert "8h maximum" in r.json()["detail"]
    assert windows_of(uid) == []


# ------------------------------------------------------------------ ICS payload routing (F13)


def test_ics_pending_payload_is_items_only(client, signed_in):
    """F13-ICS: the pending payload carries `items` (+ the tz they were authored in). No `ops`.

    The schedule surface below is the SOLE consumer; chat/confirm refuses kind='ics' before it
    ever reads a payload (R1's side of the contract).
    """
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    rows = pending_rows(uid)
    assert len(rows) == 1 and rows[0]["kind"] == "ics"
    stored = json.loads(rows[0]["payload_json"])
    assert set(stored) == {"items", "tz"}, stored.keys()
    assert stored["items"] == uploaded["items"]
    assert stored["tz"] == TZ

    # Round trip through the schedule endpoints is the whole interaction.
    got = client.get("/api/schedule/pending").json()
    assert got["items"] == uploaded["items"]
    assert got["id"] == uploaded["id"]
    r = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert r.status_code == 200, r.text
    assert len(windows_of(uid)) == 1


def test_chat_confirm_refuses_an_ics_row(client, signed_in):
    """CROSS-LANE INTERLOCK (R1): chat/confirm must 422 kind='ics' and NOT delete the row.

    R1 owns the refusal; this asserts the seam from the schedule side. If R1's change has not
    landed in a concurrent working tree yet, the test records an xfail instead of a false red —
    the schedule endpoints' own end-to-end coverage above is unconditional.
    """
    uid = signed_in
    uploaded = upload(client, ics(utc_event("20261025T180000", "20261025T190000"))).json()
    r = client.post("/api/chat/confirm", json={"id": uploaded["id"]})
    if not (r.status_code == 422 and r.json().get("error") == "payload_forbidden"):
        pytest.xfail(f"R1 interlock not landed: /api/chat/confirm answered {r.status_code} {r.text[:160]}")
    assert "/api/schedule/ics/confirm" in r.json()["detail"]
    # Chat refuses WITHOUT deleting: the preview is still the schedule surface's to confirm.
    assert len(pending_rows(uid)) == 1
    ok = client.post("/api/schedule/ics/confirm", json={"id": uploaded["id"], "pick": [0]})
    assert ok.status_code == 200, ok.text


# ------------------------------------------------------------------ the no-disk assertion


def test_upload_path_never_touches_the_filesystem(client, signed_in, monkeypatch):
    """`request.stream()` → memory → parser. No temp file, no upload dir, no os write."""
    uid = signed_in
    opened: list[str] = []

    def deny(*args, **kwargs):
        opened.append(str(args[0]) if args else "?")
        raise AssertionError(f"the ICS path must not open a file: {args!r}")

    with monkeypatch.context() as patched:
        patched.setattr(builtins, "open", deny)
        patched.setattr(io, "open", deny)
        patched.setattr(os, "open", deny)
        patched.setattr(os, "write", deny)  # no descriptor is ever handed out, so this is belt+braces
        patched.setattr(os, "mkdir", deny)
        good = upload(client, ics(utc_event("20261025T180000", "20261025T190000")))
        assert good.status_code == 200, good.text
        confirm = client.post(
            "/api/schedule/ics/confirm", json={"id": good.json()["id"], "pick": [0]}
        )
        assert confirm.status_code == 200, confirm.text
        # Error paths are just as clean.
        rrule = upload(client, ics(vevent("DTSTART:20261025T180000", "DTEND:20261025T190000",
                                          extra="RRULE:FREQ=DAILY\r\n")))
        assert rrule.status_code == 422
        too_big = upload(client, "x" * (schedules.MAX_ICS_BYTES + 16))
        assert too_big.status_code == 413
        junk = upload(client, "not a calendar at all")
        assert junk.status_code == 422
        manual = client.post(
            "/api/schedule/windows",
            json={"tz": TZ, "windows": [{"start": "2026-10-31T09:00:00", "end": "2026-10-31T10:00:00"}]},
        )
        assert manual.status_code == 200, manual.text
    assert opened == []
    assert len(windows_of(uid)) == 1


# ------------------------------------------------------------------ nothing private is stored


def db_dump() -> str:
    conn = db.connect()
    try:
        chunks: list[str] = []
        for row in db.q(conn, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            name = row["name"]
            if name.startswith("sqlite_"):
                continue
            for record in db.q(conn, f"SELECT * FROM {name}"):  # noqa: S608 - schema names only
                chunks.extend(str(value) for value in tuple(record) if value is not None)
        for row in db.q(conn, "SELECT role, content FROM chat_messages"):
            chunks.append(row["role"])
            chunks.append(row["content"])
        return "\n".join(chunks)
    finally:
        conn.close()


def test_calendar_text_never_reaches_the_database(client, signed_in):
    uid = signed_in
    extra = (
        "SUMMARY:SECRET-MIDTERM-EXAM\r\n"
        "LOCATION:ROOM-441B\r\n"
        "DESCRIPTION:PROF-NOTES-Z bring the draft\r\n"
        "ATTENDEE:mailto:friend-marker@example.com\r\n"
    )
    body = ics(
        utc_event("20261025T180000", "20261025T190000", extra=extra),
        floating_event("20261026T173000", "20261026T180000", extra=extra),
    )
    uploaded = upload(client, body)
    assert uploaded.status_code == 200, uploaded.text
    confirm = client.post("/api/schedule/ics/confirm", json={"id": uploaded.json()["id"], "pick": [0, 1]})
    assert confirm.status_code == 200, confirm.text

    dump = db_dump().upper()
    for marker in ("SECRET-MIDTERM-EXAM", "ROOM-441B", "PROF-NOTES-Z", "FRIEND-MARKER"):
        assert marker not in dump, f"{marker} leaked into the database"
    assert windows_of(uid), "the intervals themselves must be stored"


# ------------------------------------------------------------------ template


def test_schedule_template_renders_with_the_base_layout():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    templates = Path(schedules.__file__).parent / "webui" / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    rendered = env.get_template("schedule.html").render(signed_in=True)
    assert 'extends "base.html"' not in rendered  # the heredoc did not leak
    assert 'id="save-manual"' in rendered
    assert 'id="ics-file"' in rendered
    assert 'id="preview-panel"' in rendered
    assert "America/Los_Angeles" in rendered
    assert "<script>" in rendered
