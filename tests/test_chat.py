"""server/chat.py — the turn engine, the action protocol, the pending store, the endpoints.

The provider seam is server.ai.stream_reply, monkeypatched everywhere below with async
generator fixtures (CONTRACT-chat.md §2); zero network, zero sleeps, frozen clock.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # pyright: ignore[reportMissingImports]

from server import activity, ai, chat, config, db
from server.scheduling import SchedulingError
from server.schema import MAX_WINDOWS
from tests import util

FIXED_NOW = "2026-10-24T12:00:00Z"
TZ = "America/Los_Angeles"
WINDOW_OK = {"start": "2026-10-29T19:00:00", "end": "2026-10-29T21:00:00", "tz": TZ}


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    yield
    db._FIXED_NOW = None


# ------------------------------------------------------------------ scripted provider


def scripted(text: str):
    """A stream_reply stand-in yielding `text` in three chunks."""
    third = max(1, len(text) // 3)
    pieces = [text[:third], text[third : 2 * third], text[2 * third :]]

    async def fake(system, messages, *, transport=None):
        for piece in pieces:
            yield piece
        yield ai.finished()  # F5: only a [DONE]-terminated reply may apply anything

    return fake


def reply_with_fence(actions: dict | list | None = None, prose: str = "Sure."):
    body = json.dumps({"actions": actions if actions is not None else []})
    return scripted(f"{prose}\n```json\n{body}\n```\n")


def neg_reply(ops, prose: str = "Looking at the offer."):
    if isinstance(ops, dict):
        ops = [ops]
    return scripted(f"{prose} SECRET-PROSE-7\n```json\n{json.dumps({'actions': ops})}\n```\n")

def events_of(response) -> list[dict]:
    out = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: ") :]))
    return out


def last_event(response, kind: str):
    found = [e for e in events_of(response) if e["type"] == kind]
    assert found, f"no {kind} event in stream: {response.text[:400]}"
    return found[-1]


@pytest.fixture(autouse=True)
def ai_configured(monkeypatch):
    """Every streaming test needs a configured provider; 503 has its own test."""
    monkeypatch.setattr(config, "ai_api_url", lambda: "https://provider.example/v1/chat/completions")
    monkeypatch.setattr(config, "api_key_unused", lambda: None, raising=False)
    yield


# ------------------------------------------------------------------ section: endpoints hardening


def _register(client, email="ch@x.test", name="Chip"):
    r = client.post(
        "/api/register",
        json={
            "email": email,
            "password": "pw-123456",
            "display_name": name,
            "avatar_id": 2,
            "adult_attested": True,
            "registration_code": "testiva",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["user_id"]


def test_history_shape_and_ordering(client):
    uid = _register(client)
    conn = db.connect()
    try:
        chat.persist_user_message(conn, uid, "first")
        chat.persist_note(conn, uid, "a note in the middle")
        chat.persist_final_assistant(conn, uid, "second")
    finally:
        conn.close()

    chat_rows = client.get("/api/chat/history")
    assert chat_rows.status_code == 200
    body = chat_rows.json()
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "note", "assistant"]
    assert set(body["state"]) == {
        "windows", "interests", "wants", "busy", "events", "delegation", "matching", "ai",
    }
    assert body["pending"] is None
    assert body["state"]["ai"] is True  # the autouse fixture configures the provider


def test_unconfigured_returns_503(client, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: None)
    _register(client)
    r = client.post("/api/chat", json={"content": "hello"}, headers={"Content-Type": "application/json"})
    assert r.status_code == 503
    assert r.json()["error"] == "ai_unconfigured"


def test_content_type_and_body_caps(client):
    _register(client)
    r = client.post("/api/chat", content=b"{}", headers={"Content-Type": "text/plain"})
    assert r.status_code == 422

    big = json.dumps({"content": "x" * 70_000}).encode()
    r = client.post("/api/chat", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["error"] == "too_large"

    r = client.post("/api/chat", json={"content": "x" * 4001})
    assert r.status_code == 422
    r = client.post("/api/chat", json={"content": ""})
    assert r.status_code == 200 and r.text  # empty string is fine as a turn
    r = client.post("/api/chat", json={"content": "x", "extra": 1})
    assert r.status_code == 422


def test_cross_origin_refused(client, monkeypatch):
    monkeypatch.setattr(config, "base_url", lambda: "http://testserver")
    _register(client)
    r = client.post(
        "/api/chat",
        json={"content": "hello"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "origin_forbidden"
    # Same-origin passes the check (and then streams; ai stub raises — handled as SSE error).
    ok = client.post("/api/chat", json={"content": "hello"}, headers={"Origin": "http://testserver"})
    assert ok.status_code == 200


def test_rate_limit_twenty_per_ten_minutes(client):
    uid = _register(client)
    conn = db.connect()
    try:
        with db.immediate(conn):
            for _ in range(20):
                conn.execute(
                    "INSERT INTO chat_turns (user_id, kind, ref, at) VALUES (?,?,?,?)",
                    (uid, "chat", None, db.utcnow()),
                )
    finally:
        conn.close()
    r = client.post("/api/chat", json={"content": "hi"})
    assert r.status_code == 429
    assert r.json()["error"] == "rate_limited"


def test_turn_busy_conflict(client):
    uid = _register(client)
    lock = chat.get_user_lock(uid)
    asyncio.run(lock.acquire())  # a bare acquire() in sync context is an unawaited coroutine
    try:
        r = client.post("/api/chat", json={"content": "hi"})
        assert r.status_code == 409
        assert r.json()["error"] == "turn_busy"
    finally:
        lock.release()

# ------------------------------------------------------------------ section: action protocol


def test_stored_prose_never_contains_the_action_fence(client, monkeypatch):
    """The fence is wire protocol, not conversation: stored assistant prose is prose only."""
    uid = _register(client)
    fenced = (
        "Saved 3 windows for you.\n"
        '```json\n{"actions": [{"op": "set_windows", "windows": [' +
        '{"start": "2026-10-29T19:00:00", "end": "2026-10-29T21:00:00", "tz": "America/Los_Angeles"}]}]}\n```\n'
    )
    monkeypatch.setattr(chat.ai, "stream_reply", scripted(fenced))
    r = client.post("/api/chat", json={"content": "free Thursday evening"})
    assert r.status_code == 200

    conn = db.connect()
    try:
        rows = db.q(
            conn, "SELECT content FROM chat_messages WHERE user_id=? AND role='assistant'", (uid,)
        )
        assert rows, "the assistant reply must be stored"
        for row in rows:
            assert "```" not in row["content"], "the action fence must not be stored as prose"
            assert "actions" not in row["content"], "protocol payload must not be stored as prose"
    finally:
        conn.close()

    # And the live stream never EMITS tokens after the fence sentinel.
    ev = [e for e in events_of(r) if e["type"] == "token"]
    assert all("```" not in t["text"] for t in ev), "fence text must not stream as a token"
    assert any("Saved" in t["text"] for t in ev), "prose before the fence still streams"


def test_plain_conversation_persists_nothing_else(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", scripted("Just chatting, no actions here."))

    r = client.post("/api/chat", json={"content": "tell me a joke"})
    assert r.status_code == 200
    ev = events_of(r)
    assert any(e["type"] == "token" for e in ev)
    note = last_event(r, "note")
    assert note["text"].startswith("Just chatting")
    assert last_event(r, "done")["type"] == "done"

    conn = db.connect()
    try:
        rows = db.q(conn, "SELECT role, content FROM chat_messages WHERE user_id=?", (uid,))
        assert [x["role"] for x in rows] == ["user", "assistant"]
        assert db.one(conn, "SELECT COUNT(*) AS n FROM availability_windows")["n"] == 0
    finally:
        conn.close()


def test_multiple_fences_apply_nothing(client, monkeypatch):
    uid = _register(client)
    double = "```json\n{\"actions\": [{\"op\": \"none\"}]}\n```\nagain\n```json\n{\"actions\": []}\n```\n"
    monkeypatch.setattr(chat.ai, "stream_reply", scripted(double))

    r = client.post("/api/chat", json={"content": "do two things"})
    note = last_event(r, "note")
    assert "multiple action blocks" in note["text"]
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM chat_pending")["n"] == 0
    finally:
        conn.close()


def test_fence_not_last_applies_nothing(client, monkeypatch):
    uid = _register(client)
    text = "```json\n{\"actions\": []}\n```\nbut then I kept talking."
    monkeypatch.setattr(chat.ai, "stream_reply", scripted(text))
    r = client.post("/api/chat", json={"content": "hi"})
    assert "last in the reply" in last_event(r, "note")["text"]


def test_bad_json_fence_is_refused(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", scripted("x\n```json\n{not json\n```\n"))
    r = client.post("/api/chat", json={"content": "hi"})
    assert "couldn't read the action block" in last_event(r, "note")["text"]


@pytest.mark.parametrize(
    "actions, fragment",
    [
        ([{"op": "delete_everything"}], "unknown op"),
        ([{"op": "set_busy_load", "value": 2, "evil": 1}], "wrong fields"),
        ([{"op": "set_wants", "wants": ["hangout"]}, {"op": "set_wants", "wants": ["event"]}], "same field"),
        ([{"op": "set_busy_load", "value": 9}], "busy load"),
        ([{"op": "none"}, {"op": "set_wants", "wants": ["hangout"]}], "can't be combined"),
    ],
)
def test_batch_level_rejections(client, monkeypatch, actions, fragment):
    _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", reply_with_fence(actions))
    r = client.post("/api/chat", json={"content": "do it"})
    assert fragment in last_event(r, "note")["text"]


def test_immediate_apply_canonicalises_and_never_touches_grants(client, monkeypatch):
    uid = _register(client)
    before = _grants_dump(uid)

    monkeypatch.setattr(chat.ai, "stream_reply", reply_with_fence([{"op": "set_interests", "tags": ["matcha"]}]))
    r = client.post("/api/chat", json={"content": "matcha is my interest"})
    applied = last_event(r, "actions")
    assert applied["applied"][0]["op"] == "set_interests"
    assert last_event(r, "state")["state"]["interests"] == ["matcha"]

    conn = db.connect()
    try:
        tags = [x["tag"] for x in db.q(conn, "SELECT tag FROM hobby_tags WHERE user_id=?", (uid,))]
        assert "coffee" in tags  # matcha → coffee, canonicalised in the matcher's vocabulary
        rows = db.q(
            conn, "SELECT action, scope FROM consent_log WHERE user_id_hash=?", (db.user_hash(uid),)
        )
        assert [x["action"] for x in rows].count("state:written") == 1
        assert all(x["scope"] != "matching" for x in rows)
    finally:
        conn.close()
    assert _grants_dump(uid) == before


def test_immediate_apply_wants_and_busy_reject_out_of_vocab(client, monkeypatch):
    _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", reply_with_fence([{"op": "set_wants", "wants": ["hangout"]}]))
    r = client.post("/api/chat", json={"content": "hangouts only"})
    assert last_event(r, "actions")["applied"][0]["op"] == "set_wants"

    bad = client.post("/api/chat", json={"content": "again"})
    monkeypatch.setattr(chat.ai, "stream_reply", reply_with_fence([{"op": "set_wants", "wants": ["party"]}]))
    bad = client.post("/api/chat", json={"content": "again"})
    # set_wants accepts any list of strings at validation; StatePayload enforces the vocabulary.
    kinds = [e["type"] for e in events_of(bad)]
    assert "conflict" in kinds or "note" in kinds  # refused, never silently accepted


def _grants_dump(uid: int) -> list[tuple]:
    conn = db.connect()
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT user_id, kind, tier, version, fields_json, audience, granted_at,"
                " expires_at, withdrawn_at FROM grants WHERE user_id=? ORDER BY kind, version",
                (uid,),
            ).fetchall()
        ]
    finally:
        conn.close()


# ------------------------------------------------------------------ section: pending previews


def test_windows_become_a_preview_not_a_write(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence([{"op": "set_windows", "windows": [WINDOW_OK]}]),
    )
    r = client.post("/api/chat", json={"content": "I'm free Thursday evening"})
    preview = last_event(r, "preview")
    assert preview["kind"] == "windows"
    assert set(preview["items"]) == {"windows", "events"}, "typed sections (F13)"
    assert preview["items"]["windows"][0]["tz"] == TZ
    assert preview["items"]["events"] == []

    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,))["n"] == 0
        row = db.one(conn, "SELECT * FROM chat_pending WHERE user_id=?", (uid,))
        assert row is not None and row["kind"] == "windows"
    finally:
        conn.close()

    hist = client.get("/api/chat/history").json()
    assert hist["pending"]["id"] == preview["id"]


def test_confirm_applies_windows_and_riders_atomically(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence(
            [
                {"op": "set_windows", "windows": [WINDOW_OK]},
                {"op": "set_interests", "tags": ["matcha"]},
            ]
        ),
    )
    r = client.post("/api/chat", json={"content": "free Thursday and I like matcha"})
    pending_id = last_event(r, "preview")["id"]

    conn = db.connect()
    try:
        # RIDERS: strictly nothing applies before the Confirm — the whole batch defers.
        assert db.one(conn, "SELECT COUNT(*) AS n FROM hobby_tags WHERE user_id=?", (uid,))["n"] == 0
        assert db.one(conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,))["n"] == 0
    finally:
        conn.close()

    ok = client.post("/api/chat/confirm", json={"id": pending_id})
    assert ok.status_code == 200, ok.text
    ops = sorted(c["op"] for c in ok.json()["confirmations"])
    assert ops == ["set_interests", "set_windows"]

    conn = db.connect()
    try:
        wins = db.q(conn, "SELECT starts_at, ends_at, tz FROM availability_windows WHERE user_id=?", (uid,))
        assert len(wins) == 1 and wins[0]["tz"] == TZ
        tags = [x["tag"] for x in db.q(conn, "SELECT tag FROM hobby_tags WHERE user_id=?", (uid,))]
        assert tags == ["coffee"]
    finally:
        conn.close()


def test_confirm_conflict_when_state_changed(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence([{"op": "set_windows", "windows": [WINDOW_OK]}]),
    )
    r = client.post("/api/chat", json={"content": "free Thursday"})
    pending_id = last_event(r, "preview")["id"]

    # A second writer changes state between preview and confirm. The payload hash covers
    # the RAW interest words (interest_raw), not the canonical hobby_tags table.
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "INSERT OR REPLACE INTO interest_raw (user_id, raw, canonical) VALUES (?,?,?)",
                (uid, "bouldering", "bouldering"),
            )
    finally:
        conn.close()
    conflict = client.post("/api/chat/confirm", json={"id": pending_id})
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "conflict"
    # The batch is DEAD: the deletion commits, so the stale preview can never be confirmed.
    again = client.post("/api/chat/confirm", json={"id": pending_id})
    assert again.status_code == 404
    assert again.json()["error"] == "not_found"


def test_confirm_expired_preview(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence([{"op": "set_windows", "windows": [WINDOW_OK]}]),
    )
    r = client.post("/api/chat", json={"content": "free"})
    pending_id = last_event(r, "preview")["id"]

    db._FIXED_NOW = "2026-10-24T12:30:00Z"  # 30 min later: past the 10-minute TTL
    expired = client.post("/api/chat/confirm", json={"id": pending_id})
    assert expired.status_code == 409
    assert expired.json()["error"] == "expired"


def test_second_proposal_supersedes_the_first(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai, "stream_reply", reply_with_fence([{"op": "set_windows", "windows": [WINDOW_OK]}])
    )
    first = client.post("/api/chat", json={"content": "one"}).text
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence(
            [
                {
                    "op": "set_windows",
                    "windows": [dict(WINDOW_OK, start="2026-10-28T18:00:00", end="2026-10-28T20:00:00")],
                }
            ]
        ),
    )
    second = client.post("/api/chat", json={"content": "two"}).text

    conn = db.connect()
    try:
        rows = db.q(conn, "SELECT id FROM chat_pending WHERE user_id=?", (uid,))
        assert len(rows) == 1
        ids = [
            json.loads(line[len("data: ") :])["id"]
            for text in (first, second)
            for line in text.splitlines()
            if line.startswith("data: ") and '"preview"' in line
        ]
        assert len(ids) == 2  # two previews emitted server-side, one row kept
    finally:
        conn.close()


def test_invalid_window_in_proposal_is_refused(client, monkeypatch):
    _register(client)
    bad_window = dict(WINDOW_OK, start="2026-10-29T22:00:00", end="2026-10-29T19:00:00")
    monkeypatch.setattr(
        chat.ai, "stream_reply", reply_with_fence([{"op": "set_windows", "windows": [bad_window]}])
    )
    r = client.post("/api/chat", json={"content": "free"})
    note = last_event(r, "note")
    assert "couldn't use that as-is" in note["text"]
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM chat_pending")["n"] == 0
    finally:
        conn.close()


# ------------------------------------------------------------------ section: negotiation turns


def _pair():
    a = util.mk_user("na@x.test", "Ash")
    b = util.mk_user("nb@x.test", "Bree")
    util.pub_card(a)
    util.pub_card(b)
    cid, pid = util.mk_candidate(a, b)
    # activity.propose requires the tag to be an interest BOTH sides share.
    util.tags(a, ["coffee"])
    util.tags(b, ["coffee"])
    return a, b, cid, pid

def _delegation_on(uid: int):
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "INSERT INTO grants (user_id, kind, version, audience, granted_at, expires_at)"
                " VALUES (?,?,?,?,?,?)",
                (uid, "delegation", 1, "matched_users_only", db.utcnow(), db.utcnow(days=30)),
            )
    finally:
        conn.close()


def _act(uid: int, turn: chat.Turn) -> list[dict]:
    conn = db.connect()
    try:
        async def drain():
            return [e async for e in chat.run_turn(conn, uid, turn)]

        return asyncio.run(drain())
    finally:
        conn.close()


def test_negotiation_turn_offers_and_partitions_transcripts(client, monkeypatch):
    a, b, cid, pid = _pair()
    _delegation_on(a)
    conn = db.connect()
    try:
        prop = db.one(conn, "SELECT * FROM proposals WHERE id=?", (pid,))
    finally:
        conn.close()

    op = {
        "op": "offer",
        "candidate_id": cid,
        "proposal_version": prop["version"],
        "tag": "coffee",
        "url": "https://example.com/cafe",
    }
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply(op))
    events = _act(a, chat.Turn(kind="negotiation", user_text=None, system_extra="offer context"))

    assert [e["type"] for e in events if e["type"] in ("actions", "note")][-1] == "actions"
    assert events[-1]["type"] == "done"

    conn = db.connect()
    try:
        offer = db.one(conn, "SELECT * FROM activity_offers WHERE candidate_id=? ORDER BY id DESC", (cid,))
        assert offer is not None and offer["state"] == "offered"
        mine = [r["content"] for r in db.q(conn, "SELECT content FROM chat_messages WHERE user_id=?", (a,))]
        theirs = [r["content"] for r in db.q(conn, "SELECT content FROM chat_messages WHERE user_id=?", (b,))]
        assert any("your assistant proposed" in m for m in mine)
        assert any("they proposed" in m and "cafe" in m for m in theirs)
        assert all("SECRET-PROSE-7" not in m for m in theirs), "provider prose leaked to the counterpart"
        assert any("SECRET-PROSE-7" in m for m in mine), "acting user keeps their assistant's prose"
    finally:
        conn.close()


def test_negotiation_delegation_off_drops_the_op(client, monkeypatch):
    a, b, cid, pid = _pair()
    conn = db.connect()
    try:
        prop = db.one(conn, "SELECT * FROM proposals WHERE id=?", (pid,))
    finally:
        conn.close()
    op = {
        "op": "offer",
        "candidate_id": cid,
        "proposal_version": prop["version"],
        "tag": "coffee",
        "url": "https://example.com/cafe",
    }
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply(op))
    events = _act(a, chat.Turn(kind="negotiation", user_text=None, system_extra="ctx"))
    assert events[-2]["type"] == "note"
    assert "delegation was off" in events[-2]["text"]
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM activity_offers WHERE candidate_id=?", (cid,))["n"] == 0
    finally:
        conn.close()


def test_negotiation_multiple_ops_refused(client, monkeypatch):
    a, b, cid, pid = _pair()
    op = {"op": "accept", "offer_id": 1}
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply([op, op]))
    events = _act(a, chat.Turn(kind="negotiation", user_text=None, system_extra="ctx"))
    assert "more than one negotiation op" in events[-2]["text"]


def test_manual_offer_respond_needs_no_delegation(client):
    a, b, cid, pid = _pair()
    conn = db.connect()
    # activity.propose manages its own transaction — never wrap it in another.
    activity.propose(conn, a, cid, 1, "coffee", "https://example.com/cafe")
    offer_id = db.one(conn, "SELECT id FROM activity_offers WHERE candidate_id=?", (cid,))["id"]
    conn.close()

    util.direct_session(client, b)  # B, delegation OFF, answers by hand
    r = client.post(
        "/api/chat/offer-respond",
        json={"action": "accept", "candidate_id": cid, "offer_id": offer_id},
    )
    assert r.status_code == 200, r.text

    conn = db.connect()
    try:
        theirs = [r2["content"] for r2 in db.q(conn, "SELECT content FROM chat_messages WHERE user_id=?", (a,))]
        assert any("they accepted" in m for m in theirs)
    finally:
        conn.close()


# ------------------------------------------------------------------ section: consent atomicity
# Gate review P1 #1: the delegation check must live INSIDE the activity core's write
# transaction. These tests withdraw the grant after the core's authorization reads and before
# its first write, which is exactly the window the pre-flight check cannot cover.


def _withdraw_after_authorization(monkeypatch, uid: int) -> None:
    """Race the core: the grant dies between its `_guard` reads and its writes (same txn)."""
    real = activity._guard

    def racing_guard(conn, guarded_uid, candidate_id, now):
        out = real(conn, guarded_uid, candidate_id, now)
        conn.execute(
            "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='delegation'"
            " AND withdrawn_at IS NULL",
            (db.utcnow(), uid),
        )
        return out

    monkeypatch.setattr(activity, "_guard", racing_guard)


def test_autonomous_core_refuses_when_grant_vanishes_after_authorization(client, monkeypatch):
    """No chat pre-check involved: the CORE alone must refuse (nothing written)."""
    a, b, cid, pid = _pair()
    _delegation_on(a)
    _withdraw_after_authorization(monkeypatch, a)

    conn = db.connect()
    try:
        with pytest.raises(SchedulingError) as caught:
            activity.propose(
                conn, a, cid, 1, "coffee", "https://example.com/cafe", autonomous=True
            )
        assert (caught.value.status, caught.value.code) == (409, "delegation_required")
        assert "delegation was off" in caught.value.detail
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM activity_offers WHERE candidate_id=?", (cid,)
        )["n"] == 0
    finally:
        conn.close()


def test_manual_core_call_needs_no_delegation(client, monkeypatch):
    """The same core without `autonomous=True`: a human click is the consent."""
    a, b, cid, pid = _pair()
    _withdraw_after_authorization(monkeypatch, a)  # no grant to withdraw: must not matter
    conn = db.connect()
    try:
        out = activity.propose(conn, a, cid, 1, "coffee", "https://example.com/cafe")
        assert out["state"] == "offered"
    finally:
        conn.close()


# ------------------------------------------------------------------ section: value validation
# Gate review P1 #2: an allow-listed KEY is not an allow-listed VALUE. The cores validate.

BAD_VALUES = {
    "prose_url": ("coffee", "https://example.com/SECRET-PROSE-7 my windows are Sunday"),
    "prose_tag": ("my windows are Sunday", "https://example.com/cafe"),
    "javascript_url": ("coffee", "javascript:alert(1)"),
    "oversized_url": ("coffee", "https://example.com/" + "a" * 400),
}


def _notes_for(conn, uid: int) -> list[str]:
    return [
        row["content"]
        for row in db.q(
            conn, "SELECT content FROM chat_messages WHERE user_id=? AND role='note'", (uid,)
        )
    ]


@pytest.mark.parametrize("case", sorted(BAD_VALUES))
def test_negotiation_op_values_validated_by_the_core(client, monkeypatch, case):
    tag, url = BAD_VALUES[case]
    a, b, cid, pid = _pair()
    _delegation_on(a)
    conn = db.connect()
    try:
        version = db.one(conn, "SELECT version FROM proposals WHERE id=?", (pid,))["version"]
    finally:
        conn.close()
    op = {
        "op": "offer",
        "candidate_id": cid,
        "proposal_version": version,
        "tag": tag,
        "url": url,
    }
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply(op))

    events = _act(a, chat.Turn(kind="negotiation", user_text=None, system_extra="ctx"))

    assert events[-2]["type"] == "note"
    conn = db.connect()
    try:
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM activity_offers WHERE candidate_id=?", (cid,)
        )["n"] == 0
        assert _notes_for(conn, b) == [], "the counterpart must learn nothing about a refusal"
        assert all("SECRET-PROSE-7" not in note for note in _notes_for(conn, a))
    finally:
        conn.close()


@pytest.mark.parametrize("case", sorted(BAD_VALUES))
def test_manual_offer_endpoint_validates_values_too(client, case):
    tag, url = BAD_VALUES[case]
    a, b, cid, pid = _pair()
    conn = db.connect()
    try:
        version = db.one(conn, "SELECT version FROM proposals WHERE id=?", (pid,))["version"]
    finally:
        conn.close()
    util.direct_session(client, a)  # delegation OFF: the click is the consent, the value is not

    r = client.post(
        "/api/chat/offer-respond",
        json={
            "action": "offer",
            "candidate_id": cid,
            "proposal_version": version,
            "tag": tag,
            "url": url,
        },
    )

    assert r.status_code == 422, r.text
    assert r.json()["error"] == "payload_forbidden"
    conn = db.connect()
    try:
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM activity_offers WHERE candidate_id=?", (cid,)
        )["n"] == 0
        assert _notes_for(conn, b) == []
    finally:
        conn.close()


# ------------------------------------------------------------------ section: confirm outcomes
# Gate review P1 #10: the old code raised _ConfirmError INSIDE db.immediate, which rolled the
# transaction back — cancellation answered "ok" while the row survived (still confirmable).


def _pending_rows(uid: int) -> int:
    conn = db.connect()
    try:
        return db.one(conn, "SELECT COUNT(*) AS n FROM chat_pending WHERE user_id=?", (uid,))["n"]
    finally:
        conn.close()


def _rider_counts(uid: int) -> tuple[int, int]:
    conn = db.connect()
    try:
        tags = db.one(conn, "SELECT COUNT(*) AS n FROM hobby_tags WHERE user_id=?", (uid,))["n"]
        wins = db.one(
            conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,)
        )["n"]
    finally:
        conn.close()
    return tags, wins


def test_confirm_cancel_really_deletes_the_row(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence(
            [
                {"op": "set_windows", "windows": [WINDOW_OK]},
                {"op": "set_interests", "tags": ["matcha"]},
            ]
        ),
    )
    preview = last_event(client.post("/api/chat", json={"content": "free Thursday"}), "preview")
    assert _pending_rows(uid) == 1

    cancel = client.post("/api/chat/confirm", json={"id": preview["id"], "cancel": True})

    assert cancel.json() == {"ok": True, "cancelled": True}
    assert _pending_rows(uid) == 0, "cancellation must COMMIT the deletion"
    assert _rider_counts(uid) == (0, 0), "cancelling applies no rider"
    late = client.post("/api/chat/confirm", json={"id": preview["id"]})
    assert late.status_code == 404 and late.json()["error"] == "not_found"


def test_confirm_expiry_deletes_the_row(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai, "stream_reply", reply_with_fence([{"op": "set_windows", "windows": [WINDOW_OK]}])
    )
    preview = last_event(client.post("/api/chat", json={"content": "free"}), "preview")

    db._FIXED_NOW = "2026-10-24T12:30:00Z"  # 30 min later: past the 10-minute TTL
    expired = client.post("/api/chat/confirm", json={"id": preview["id"]})

    assert expired.status_code == 409 and expired.json()["error"] == "expired"
    assert _pending_rows(uid) == 0, "expiry must COMMIT the deletion"
    assert _rider_counts(uid) == (0, 0)
    again = client.post("/api/chat/confirm", json={"id": preview["id"]})
    assert again.status_code == 404 and again.json()["error"] == "not_found"


def test_confirm_conflict_deletes_the_row_and_applies_no_rider(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence(
            [
                {"op": "set_windows", "windows": [WINDOW_OK]},
                {"op": "set_interests", "tags": ["matcha"]},
            ]
        ),
    )
    preview = last_event(client.post("/api/chat", json={"content": "free and matcha"}), "preview")

    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "INSERT OR REPLACE INTO interest_raw (user_id, raw, canonical) VALUES (?,?,?)",
                (uid, "bouldering", "bouldering"),
            )
    finally:
        conn.close()

    conflict = client.post("/api/chat/confirm", json={"id": preview["id"]})

    assert conflict.status_code == 409 and conflict.json()["error"] == "conflict"
    assert _pending_rows(uid) == 0, "a conflicted batch is dead and its row is gone"
    assert _rider_counts(uid) == (0, 0), "the interest_raw rider must not have applied"


# ------------------------------------------------------------------ section: deletion safety
# Gate review P1 #3: provider work that finishes AFTER delete_account must not recreate chat
# rows, reserve budget or apply actions for an account that no longer exists.


def _soft_delete(uid: int) -> None:
    """Delete the account the way the cascade does, but from inside a streamed turn."""
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE users SET deleted_at=?, email=? WHERE id=?",
                (db.utcnow(), f"deleted-{uid}@invalid", uid),
            )
            conn.execute("DELETE FROM chat_messages WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM chat_pending WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM chat_turns WHERE user_id=?", (uid,))
    finally:
        conn.close()


def deletes_the_account_mid_stream(uid: int, text: str):
    """The provider finishes only after the account is gone: the exact P1 window."""

    async def fake(system, messages, *, transport=None):
        _soft_delete(uid)
        for start in range(0, len(text), 40):
            yield text[start : start + 40]

    return fake


def _chat_row_counts(uid: int) -> tuple[int, int, int]:
    conn = db.connect()
    try:
        return (
            db.one(conn, "SELECT COUNT(*) AS n FROM chat_messages WHERE user_id=?", (uid,))["n"],
            db.one(conn, "SELECT COUNT(*) AS n FROM chat_pending WHERE user_id=?", (uid,))["n"],
            db.one(conn, "SELECT COUNT(*) AS n FROM chat_turns WHERE user_id=?", (uid,))["n"],
        )
    finally:
        conn.close()


def test_completed_turn_after_deletion_writes_nothing(client, monkeypatch):
    uid = _register(client)
    text = "All done.\n```json\n{\"actions\": [{\"op\": \"set_busy_load\", \"value\": 2}]}\n```\n"
    monkeypatch.setattr(chat.ai, "stream_reply", deletes_the_account_mid_stream(uid, text))

    r = client.post("/api/chat", json={"content": "busy week"})

    assert r.status_code == 200
    assert last_event(r, "done")["type"] == "done"
    assert _chat_row_counts(uid) == (0, 0, 0)
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM user_load WHERE user_id=?", (uid,))["n"] == 0
    finally:
        conn.close()


def test_proposal_after_deletion_stores_no_preview(client, monkeypatch):
    uid = _register(client)
    text = (
        "Here you go.\n```json\n"
        + json.dumps({"actions": [{"op": "set_windows", "windows": [WINDOW_OK]}]})
        + "\n```\n"
    )
    monkeypatch.setattr(chat.ai, "stream_reply", deletes_the_account_mid_stream(uid, text))

    r = client.post("/api/chat", json={"content": "free Thursday"})

    assert events_of(r)[-1]["type"] == "done"
    assert not [e for e in events_of(r) if e["type"] == "preview"]
    assert _chat_row_counts(uid) == (0, 0, 0)
    conn = db.connect()
    try:
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,)
        )["n"] == 0
    finally:
        conn.close()


def test_error_note_after_deletion_persists_nothing(client, monkeypatch):
    uid = _register(client)

    async def broken(system, messages, *, transport=None):
        _soft_delete(uid)
        raise chat.ai.AIError("ai_timeout", "the provider took too long")
        yield ""  # pragma: no cover

    monkeypatch.setattr(chat.ai, "stream_reply", broken)

    r = client.post("/api/chat", json={"content": "hello?"})

    assert last_event(r, "error")["code"] == "ai_timeout"
    assert _chat_row_counts(uid) == (0, 0, 0), "no note may be recreated for a deleted account"


def test_reserve_turn_refuses_for_a_deleted_account(client):
    uid = _register(client)
    _soft_delete(uid)
    conn = db.connect()
    try:
        assert chat.reserve_turn(conn, uid, "negotiation", "offer:1") is False
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM chat_turns WHERE user_id=?", (uid,)
        )["n"] == 0
        assert chat.persist_note(conn, uid, "a note") is None
        assert chat.persist_final_assistant(conn, uid, "text") is None
    finally:
        conn.close()


# ------------------------------------------------------------------ section: completion status
# Gate review P1 #5: a truncated (capped) or unterminated reply is text, never an instruction.


def cut_short(text: str, status: str):
    """A provider stand-in that yields a complete-looking reply and then says how it ended."""

    async def fake(system, messages, *, transport=None):
        yield text
        yield ai.finished(status)

    return fake


COMPLETE_LOOKING = 'Sure.\n```json\n{"actions": [{"op": "set_interests", "tags": ["matcha"]}]}\n```\n'


@pytest.mark.parametrize("status", [ai.CAPPED, ai.EOF])
def test_cut_off_reply_applies_nothing(client, monkeypatch, status):
    uid = _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", cut_short(COMPLETE_LOOKING, status))

    r = client.post("/api/chat", json={"content": "matcha please"})

    kinds = [e["type"] for e in events_of(r)]
    assert "actions" not in kinds, "a cut-off reply must not execute its fence"
    assert "state" not in kinds
    assert last_event(r, "note")["text"] == chat.CUT_OFF_NOTE
    assert events_of(r)[-1]["type"] == "done"
    conn = db.connect()
    try:
        # the text is still honest record-keeping; only the instruction is dropped
        roles = [x["role"] for x in db.q(conn, "SELECT role FROM chat_messages WHERE user_id=?", (uid,))]
        assert roles == ["user", "assistant", "note"]
        assert db.one(conn, "SELECT COUNT(*) AS n FROM hobby_tags WHERE user_id=?", (uid,))["n"] == 0
    finally:
        conn.close()


def test_missing_completion_marker_is_not_executable(client, monkeypatch):
    """A stream that ends without any status marker (an old-style scripted reply) is EOF."""

    uid = _register(client)

    async def markerless(system, messages, *, transport=None):
        yield COMPLETE_LOOKING
        # no ai.finished(): the stream simply stops

    monkeypatch.setattr(chat.ai, "stream_reply", markerless)

    r = client.post("/api/chat", json={"content": "matcha please"})

    assert "actions" not in [e["type"] for e in events_of(r)]
    assert last_event(r, "note")["text"] == chat.CUT_OFF_NOTE


def test_done_marker_executes(client, monkeypatch):
    """Control: the same reply WITH the [DONE] marker does apply."""
    uid = _register(client)
    monkeypatch.setattr(chat.ai, "stream_reply", cut_short(COMPLETE_LOOKING, ai.DONE))

    r = client.post("/api/chat", json={"content": "matcha please"})

    assert last_event(r, "actions")["applied"][0]["op"] == "set_interests"


# ------------------------------------------------------------------ section: pending sections
# Gate review P2 #13: a batch that proposes windows AND events used to arrive as one flattened
# list under a single `kind`; `ops` still applies, `items` is now typed.

EVENT_OK = {
    "url": "https://example.com/gig",
    "category": "music",
    "start": "2026-10-30T19:00:00",
    "end": "2026-10-30T22:00:00",
    "tz": TZ,
}


def test_mixed_proposal_payload_is_typed_sections(client, monkeypatch):
    uid = _register(client)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        reply_with_fence(
            [
                {"op": "set_windows", "windows": [WINDOW_OK]},
                {"op": "set_events", "events": [EVENT_OK]},
                {"op": "set_wants", "wants": ["hangout"]},
            ]
        ),
    )

    preview = last_event(client.post("/api/chat", json={"content": "free Thursday"}), "preview")

    assert set(preview["items"]) == {"windows", "events"}
    assert len(preview["items"]["windows"]) == 1
    assert len(preview["items"]["events"]) == 1
    assert preview["items"]["events"][0]["url"] == EVENT_OK["url"]

    # the history representation carries the same shape for the same preview
    pending = client.get("/api/chat/history").json()["pending"]
    assert pending["id"] == preview["id"]
    assert set(pending["items"]) == {"windows", "events"}
    assert pending["items"]["events"][0]["category"] == "music"

    # and the APPLY path is unchanged: one confirm applies every op in the batch
    ok = client.post("/api/chat/confirm", json={"id": preview["id"]})
    assert ok.status_code == 200, ok.text
    assert sorted(item["op"] for item in ok.json()["confirmations"]) == [
        "set_events", "set_wants", "set_windows",
    ]
    conn = db.connect()
    try:
        assert db.one(conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,))["n"] == 1
        assert db.one(conn, "SELECT COUNT(*) AS n FROM user_events WHERE user_id=?", (uid,))["n"] == 1
    finally:
        conn.close()


def test_chat_confirm_refuses_an_ics_preview(client):
    """F13: an ICS preview belongs to /api/schedule/ics/confirm, never to chat."""
    uid = _register(client)
    conn = db.connect()
    try:
        with db.immediate(conn):
            cursor = conn.execute(
                "INSERT INTO chat_pending (user_id, kind, payload_json, state_hash, created_at,"
                " expires_at) VALUES (?,?,?,?,?,?)",
                (
                    uid,
                    "ics",
                    json.dumps({"ops": [], "items": {"windows": [WINDOW_OK], "events": []}}),
                    chat.current_state_hash(conn, uid),
                    db.utcnow(),
                    db.utcnow(days=1),
                ),
            )
            pending_id = int(cursor.lastrowid or 0)
    finally:
        conn.close()

    r = client.post("/api/chat/confirm", json={"id": pending_id})

    assert r.status_code == 422
    assert r.json()["error"] == "payload_forbidden"
    assert "schedule" in r.json()["detail"]
    assert _pending_rows(uid) == 1, "the schedule surface still owns that row"
    conn = db.connect()
    try:
        assert db.one(
            conn, "SELECT COUNT(*) AS n FROM availability_windows WHERE user_id=?", (uid,)
        )["n"] == 0
    finally:
        conn.close()


# ------------------------------------------------------------------ section: lifecycle


def test_purge_and_cascade(db_env):
    # db_env patches the DB path; init explicitly (this test uses no client fixture).
    db.init_db()
    conn = db.connect()
    try:
        with db.immediate(conn):
            conn.execute("INSERT INTO users (email, display_name, preset_avatar_id, created_at)"
                         " VALUES ('p@x.test','P',1,?)", (db.utcnow(),))
            uid = conn.execute("SELECT id FROM users WHERE email='p@x.test'").fetchone()["id"]
            chat.persist_user_message(conn, uid, "old")
        with db.immediate(conn):
            conn.execute("UPDATE chat_messages SET created_at=? WHERE user_id=?", ("2026-10-01T00:00:00Z", uid))
        conn.execute("INSERT INTO chat_turns (user_id, kind, at) VALUES (?,'chat','2026-10-01T00:00:00Z')", (uid,))
        removed = chat.purge_chat(conn, db.utcnow())
        assert removed >= 2
        assert db.one(conn, "SELECT COUNT(*) AS n FROM chat_messages WHERE user_id=?", (uid,))["n"] == 0

        chat.persist_user_message(conn, uid, "fresh")
        chat.cascade_chat(conn, uid)
        assert db.one(conn, "SELECT COUNT(*) AS n FROM chat_messages WHERE user_id=?", (uid,))["n"] == 0
    finally:
        conn.close()


def test_delete_history_serialized_with_lock(client):
    uid = _register(client)
    conn = db.connect()
    try:
        chat.persist_user_message(conn, uid, "whisper")
        chat.persist_final_assistant(conn, uid, "reply")
    finally:
        conn.close()
    r = client.delete("/api/chat")
    assert r.json() == {"ok": True}
    conn = db.connect()
    try:
        rows = [x["content"] for x in db.q(conn, "SELECT content FROM chat_messages WHERE user_id=?", (uid,))]
        assert rows == ["history cleared"]
    finally:
        conn.close()


def test_provider_error_becomes_honest_error_event(client, monkeypatch):
    uid = _register(client)

    async def broken(system, messages, *, transport=None):
        raise chat.ai.AIError("ai_timeout", "the provider took too long")
        yield ""

    monkeypatch.setattr(chat.ai, "stream_reply", broken)
    r = client.post("/api/chat", json={"content": "hello?"})
    err = last_event(r, "error")
    assert err["code"] == "ai_timeout"
    conn = db.connect()
    try:
        notes = [x["content"] for x in db.q(conn, "SELECT content FROM chat_messages WHERE user_id=? AND role='note'", (uid,))]
        assert any("could not answer" in n for n in notes)
    finally:
        conn.close()
