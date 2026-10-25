"""server/negotiate.py — the autonomous negotiation worker (CONTRACT-chat.md §6, PLAN §C2).

Same provider seam as tests/test_chat.py: `server.ai.stream_reply` is monkeypatched with async
generator fixtures (`fake(system, messages, *, transport=None)`), zero network, zero sleeps,
`db._FIXED_NOW` for every time-sensitive assertion.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from server import activity, ai, chat, config, db, negotiate, offers
from tests import util

FIXED_NOW = "2026-10-24T12:00:00Z"
URL = "https://example.com/cafe"
URL2 = "https://example.com/wall"
PROSE_MARK = "SECRET-PROSE-7"  # distinctive provider prose, must never cross transcripts

# The counterpart templates chat.py renders from {'decided', 'tag', 'url', 'reason'} with who="they".
THEY_ACCEPT = "they accepted the activity"
THEY_PASSED_NOTE = "they passed: too far"
THEY_COUNTER = f"they proposed climbing instead: {URL2}"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(db, "_FIXED_NOW", FIXED_NOW)
    monkeypatch.setattr(config, "ai_api_url", lambda: "https://provider.example/v1/chat/completions")
    negotiate._STARTED = False
    chat._LOCKS.clear()
    yield
    db._FIXED_NOW = None
    negotiate._STARTED = False
    chat._LOCKS.clear()


# ------------------------------------------------------------------ scripted provider


def scripted(text: str):
    """A stream_reply stand-in yielding `text` in small chunks."""

    async def fake(system, messages, *, transport=None):
        for start in range(0, len(text), 40):
            yield text[start : start + 40]

    return fake


def neg_reply(ops, prose: str = "Looking at the offer."):
    if isinstance(ops, dict):
        ops = [ops]
    return scripted(f"{prose} {PROSE_MARK}\n```json\n{json.dumps({'actions': ops})}\n```\n")


def provider_error(code: str = "ai_timeout", detail: str = "the provider timed out"):
    async def fake(system, messages, *, transport=None):
        raise ai.AIError(code, detail)
        yield ""  # pragma: no cover - unreachable, keeps the signature a generator

    return fake


def withdraw_delegation_mid_stream(text: str, uid: int):
    """Withdraw the grant the moment the provider starts streaming (PLAN §C2a race)."""

    async def fake(system, messages, *, transport=None):
        conn = db.connect()
        try:
            with db.immediate(conn):
                conn.execute(
                    "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='delegation'",
                    (db.utcnow(), uid),
                )
        finally:
            conn.close()
        yield text

    return fake


# ------------------------------------------------------------------ fixtures / helpers


def _pair():
    """Two users, a matched candidate + proposal v1, and one interest both share."""
    a = util.mk_user("na@x.test", "Ash")
    b = util.mk_user("nb@x.test", "Bree")
    util.pub_card(a)
    util.pub_card(b)
    cid, pid = util.mk_candidate(a, b)
    util.tags(a, ["coffee", "climbing"])
    util.tags(b, ["coffee", "climbing"])
    return a, b, cid, pid


def _pending_offer(cid: int, proposer: int, tag: str = "coffee", url: str = URL) -> int:
    """A from-the-agent offer: build it through the activity core, never by hand."""
    conn = db.connect()
    try:
        activity.propose(conn, proposer, cid, 1, tag, url)
        row = db.one(
            conn, "SELECT id FROM activity_offers WHERE candidate_id=? ORDER BY id DESC LIMIT 1", (cid,)
        )
        assert row is not None
        return int(row["id"])
    finally:
        conn.close()


def _tick() -> dict:
    return asyncio.run(negotiate.tick_once())


def _rows(uid: int) -> list[tuple[str, str]]:
    conn = db.connect()
    try:
        return [
            (r["role"], r["content"])
            for r in db.q(conn, "SELECT role, content FROM chat_messages WHERE user_id=? ORDER BY id", (uid,))
        ]
    finally:
        conn.close()


def _contents(uid: int) -> list[str]:
    return [content for _, content in _rows(uid)]


def _turn_rows(uid: int) -> list:
    conn = db.connect()
    try:
        return db.q(conn, "SELECT * FROM chat_turns WHERE user_id=? ORDER BY id", (uid,))
    finally:
        conn.close()


def _offer(offer_id: int):
    conn = db.connect()
    try:
        return db.one(conn, "SELECT * FROM activity_offers WHERE id=?", (offer_id,))
    finally:
        conn.close()


def _offer_count(cid: int) -> int:
    conn = db.connect()
    try:
        return db.one(conn, "SELECT COUNT(*) AS n FROM activity_offers WHERE candidate_id=?", (cid,))["n"]
    finally:
        conn.close()


# ------------------------------------------------------------------ singleton


def test_loop_is_a_singleton_and_started_flips(client, monkeypatch):
    # The app's own lifespan starts the loop whenever the provider is configured (the
    # module's autouse fixture does), so the module-global flag is already True on entry.
    # This test exercises the singleton GUARD itself, not the app's instance: reset the
    # flag and drive a second, independent loop on our own event loop.
    monkeypatch.setattr(negotiate, "_STARTED", False)
    async def scenario():
        assert negotiate.started() is False
        task = asyncio.create_task(negotiate.loop())
        await asyncio.sleep(0)
        assert negotiate.started() is True
        with pytest.raises(RuntimeError, match="negotiation loop already running"):
            await negotiate.loop()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert negotiate.started() is False

    asyncio.run(scenario())


# ------------------------------------------------------------------ delegation gate


def test_disabled_delegation_ticks_do_nothing(client, monkeypatch):
    a, b, cid, pid = _pair()
    offer_id = _pending_offer(cid, a)  # B is the addressee, B has no delegation grant
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))

    stats = _tick()

    assert stats == {"pending": 1, "answered": 0, "skipped": 1}
    assert _turn_rows(b) == []  # no budget spent on a user who never opted in
    assert _contents(b) == []  # and nothing in their transcript
    assert _offer(offer_id)["state"] == "offered"


# ------------------------------------------------------------------ accept / pass / counter


def test_enabled_delegation_scripted_accept_settles_and_partitions_transcripts(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))

    stats = _tick()

    assert stats == {"pending": 1, "answered": 1, "skipped": 0}
    assert _offer(offer_id)["state"] == "accepted"
    # the acting user's transcript: the assistant prose plus the server-templated fact
    roles = [role for role, _ in _rows(b)]
    assert "assistant" in roles and "note" in roles
    assert any(PROSE_MARK in content for content in _contents(b))
    assert "your assistant accepted the activity" in _contents(b)
    # the counterpart's transcript: EXACTLY one row, the structured note, no provider prose
    assert _contents(a) == [THEY_ACCEPT]
    assert all(PROSE_MARK not in content for content in _contents(a))
    # budget ledger: one negotiation row against the offer
    turns = _turn_rows(b)
    assert len(turns) == 1 and turns[0]["kind"] == "negotiation" and turns[0]["ref"] == f"offer:{offer_id}"


def test_pass_with_reason_settles(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(
        chat.ai, "stream_reply", neg_reply({"op": "pass", "offer_id": offer_id, "reason": "too_far"})
    )

    _tick()

    row = _offer(offer_id)
    assert row["state"] == "passed" and row["reason"] == "too_far"
    assert _contents(a) == [THEY_PASSED_NOTE]
    assert any("passed: too far" in content for content in _contents(b))


def test_counter_creates_new_offer_and_decrements_rounds(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(
        chat.ai,
        "stream_reply",
        neg_reply({"op": "counter", "offer_id": offer_id, "tag": "climbing", "url": URL2}),
    )

    # rounds_left must be visible to the model: round 1 used of MAX_ROUNDS
    seen: list[str] = []
    inner = chat.ai.stream_reply

    async def spy(system, messages, *, transport=None):
        seen.append(system)
        async for piece in inner(system, messages, transport=transport):
            yield piece

    monkeypatch.setattr(chat.ai, "stream_reply", spy)

    _tick()

    assert _offer(offer_id)["state"] == "countered"
    assert _offer_count(cid) == 2
    conn = db.connect()
    try:
        prop = db.one(conn, "SELECT * FROM proposals WHERE id=?", (pid,))
        newest = offers.latest_offer(conn, prop["id"])
    finally:
        conn.close()
    assert newest["round"] == 2 and newest["state"] == "offered"
    assert newest["proposer_id"] == b and newest["activity_tag"] == "climbing"
    # the rounds_left hint that the worker put in `system_extra` reached the prompt
    assert '"rounds_left":2' in seen[0]
    assert _contents(a) == [THEY_COUNTER]


# ------------------------------------------------------------------ refusals


def test_more_than_one_op_refused_note_persisted_offer_untouched(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    accept = {"op": "accept", "offer_id": offer_id}
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply([accept, accept]))

    stats = _tick()

    assert stats["answered"] == 0  # no actions event: only an honest note
    assert "more than one negotiation op — nothing applied" in _contents(b)
    assert _contents(a) == []  # the counterpart learns nothing about a refusal
    assert _offer(offer_id)["state"] == "offered"
    assert _offer_count(cid) == 1


def test_delegation_withdrawn_mid_stream_applies_nothing(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    text = f"Answering.\n```json\n{json.dumps({'actions': [{'op': 'accept', 'offer_id': offer_id}]})}\n```\n"
    monkeypatch.setattr(chat.ai, "stream_reply", withdraw_delegation_mid_stream(text, b))

    stats = _tick()

    assert stats["answered"] == 1  # the turn ran; the apply refused
    assert _offer(offer_id)["state"] == "offered"
    assert any("your delegation was off when this would have applied" in c for c in _contents(b))
    assert _contents(a) == []


# ------------------------------------------------------------------ bounds


def test_withdrawal_between_precheck_and_core_still_refuses(client, monkeypatch):
    """Gate review P1 #1, worker path: every advisory check passes, the CORE still refuses.

    The grant is withdrawn by a racing write between `_guard`'s authorization reads and the
    core's own writes — i.e. inside the same BEGIN IMMEDIATE. Only an in-transaction re-read
    can see that, so removing the core guard makes this test fail.
    """
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)  # B is the addressee
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))

    real = activity._guard

    def racing_guard(conn, uid, candidate_id, now):
        out = real(conn, uid, candidate_id, now)
        conn.execute(
            "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='delegation'"
            " AND withdrawn_at IS NULL",
            (db.utcnow(), b),
        )
        return out

    monkeypatch.setattr(activity, "_guard", racing_guard)

    _tick()

    assert _offer(offer_id)["state"] == "offered"
    assert _contents(a) == [], "nothing may reach the counterpart's transcript"
    assert any("your delegation was off when this would have applied" in c for c in _contents(b))


def test_attempt_cap_stops_after_two_failures_and_notes_once(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(chat.ai, "stream_reply", provider_error())

    _tick()
    _tick()
    assert len(_turn_rows(b)) == 2
    assert negotiate.MANUAL_NOTE not in _contents(b)

    _tick()
    assert len(_turn_rows(b)) == 2, "the capped tick must not reserve more budget"
    assert _contents(b).count(negotiate.MANUAL_NOTE) == 1
    assert _offer(offer_id)["state"] == "offered"

    _tick()  # idempotent: still exactly one note, still no new budget row
    assert len(_turn_rows(b)) == 2
    assert _contents(b).count(negotiate.MANUAL_NOTE) == 1
    assert _contents(a) == []


def test_autonomy_ceiling_skips_without_consuming_budget(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))
    conn = db.connect()
    try:
        with db.immediate(conn):
            for _ in range(negotiate.AUTONOMY_CEILING):
                conn.execute(
                    "INSERT INTO chat_turns (user_id, kind, ref, at) VALUES (?,?,?,?)",
                    (b, "negotiation", None, db.utcnow()),
                )
    finally:
        conn.close()

    stats = _tick()

    assert stats == {"pending": 1, "answered": 0, "skipped": 1}
    assert len(_turn_rows(b)) == negotiate.AUTONOMY_CEILING  # a ceiling skip writes no row
    assert _offer(offer_id)["state"] == "offered"
    assert _contents(b) == []


def test_shared_rate_budget_full_skips_cleanly(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))
    monkeypatch.setattr(chat, "RATE_LIMIT", 0)

    stats = _tick()

    assert stats == {"pending": 1, "answered": 0, "skipped": 1}
    assert _turn_rows(b) == []
    assert _offer(offer_id)["state"] == "offered"


def test_lock_busy_skips_cleanly(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(chat.ai, "stream_reply", neg_reply({"op": "accept", "offer_id": offer_id}))

    async def scenario():
        lock = chat.get_user_lock(b)
        await lock.acquire()  # a human turn is mid-flight
        try:
            return await negotiate.tick_once()
        finally:
            lock.release()

    stats = asyncio.run(scenario())

    assert stats == {"pending": 1, "answered": 0, "skipped": 1}
    assert _offer(offer_id)["state"] == "offered"
    assert _contents(b) == []


def test_chat_disabled_ticks_do_nothing(client, monkeypatch):
    a, b, cid, pid = _pair()
    util.grant(b, "delegation")
    offer_id = _pending_offer(cid, a)
    monkeypatch.setattr(config, "ai_api_url", lambda: None)
    monkeypatch.setattr(chat.ai, "stream_reply", provider_error("ai_unconfigured", "not configured"))

    assert _tick() == {"pending": 0, "answered": 0, "skipped": 0}
    assert _turn_rows(b) == []
    assert _offer(offer_id)["state"] == "offered"
