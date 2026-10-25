"""Tests for server/ai.py — the OpenAI chat-completions client (CONTRACT-chat.md §2).

ZERO network. The injection seam is the `transport` kwarg of `server.ai.stream_reply`
(an `httpx.MockTransport` is passed straight in). Lanes B and N monkeypatch
`server.ai.stream_reply` itself with async generators — do not fork the seam.

No asyncio pytest plugin is installed in this repo (deps are frozen), so every async case
is driven by an `asyncio.run(...)` wrapper inside a sync test function.
"""
import asyncio
import json

import httpx
import pytest

from server import ai, config

SSE = {"content-type": "text/event-stream"}
JSON = {"content-type": "application/json"}
URL = "https://provider.test/v1/chat/completions"


@pytest.fixture()
def configured(monkeypatch):
    """Point config at a fake provider; every config value is read at call time."""
    monkeypatch.setattr(config, "ai_api_url", lambda: URL)
    monkeypatch.setattr(config, "ai_api_key", lambda: "sk-test-key")
    monkeypatch.setattr(config, "ai_model", lambda: "test-model")
    monkeypatch.setattr(config, "ai_max_tokens", lambda: 800)
    monkeypatch.setattr(config, "ai_timeout_seconds", lambda: 5.0)


def _collect(agen):
    """Run an async generator to completion; returns (completion_status, text chunks).

    The stream's last item is an `ai.Completion` carrying DONE / CAPPED / EOF (F5).
    """

    async def run():
        chunks: list[str] = []
        status = ai.EOF
        async for item in agen:
            if isinstance(item, ai.Completion):
                status = item.status
                continue
            chunks.append(item)
        return status, chunks

    return asyncio.run(run())


def _collect_error(agen) -> ai.AIError:
    """Run an async generator expecting an AIError; returns it."""

    async def run():
        async for _chunk in agen:
            pass

    try:
        asyncio.run(run())
    except ai.AIError as exc:
        return exc
    raise AssertionError("expected AIError, stream completed instead")


def _transport(handler):
    return httpx.MockTransport(handler)


def _sse(*objects) -> bytes:
    """Encode objects as OpenAI SSE `data:` lines (dicts serialized, strings verbatim)."""
    out = []
    for obj in objects:
        body = obj if isinstance(obj, str) else json.dumps(obj)
        out.append(f"data: {body}\n\n")
    return "".join(out).encode()


def _delta(content):
    return {"id": "c1", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": content}}]}


def test_happy_path_joins_deltas(configured):
    """3 content chunks + 1 role-only chunk (no content) + [DONE] -> joined string."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers=SSE,
            content=_sse(
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                _delta("Hello"),
                _delta(", wor"),
                _delta("ld!"),
                "[DONE]",
            ),
        )

    status, chunks = _collect(
        ai.stream_reply("sys prompt", [{"role": "user", "content": "hi"}], transport=_transport(handler))
    )

    assert status == ai.DONE  # [DONE] seen: the ONLY clean completion
    assert "".join(chunks) == "Hello, world!"
    assert seen["url"] == URL
    assert seen["headers"]["authorization"] == "Bearer sk-test-key"
    body = seen["body"]
    assert body["model"] == "test-model"
    assert body["stream"] is True
    assert body["max_tokens"] == 800
    assert body["messages"] == [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "hi"},
    ]


def test_empty_completion_raises_protocol(configured):
    def handler(request):
        return httpx.Response(
            200, headers=SSE, content=_sse({"choices": []}, "[DONE]")
        )

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_protocol"


def test_anthropic_event_stream_raises_protocol(configured):
    """An Anthropic-shaped SSE stream is not OpenAI chat-completions."""

    def handler(request):
        return httpx.Response(
            200,
            headers=SSE,
            content=_sse(
                {"type": "message_start", "message": {"id": "m1"}},
                {"type": "content_block_delta", "delta": {"text": "x"}},
            ),
        )

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_protocol"


def test_401_raises_auth_with_redacted_message(configured):
    raw_message = "Incorrect API key provided: sk-abc\x00\x07\n do not share"

    def handler(request):
        return httpx.Response(
            401,
            headers=JSON,
            content=json.dumps({"error": {"message": raw_message, "type": "invalid_request_error"}}).encode(),
        )

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_auth"
    assert exc.detail == ai.redact(raw_message)
    assert "\x00" not in exc.detail and "\x07" not in exc.detail
    assert "Incorrect API key provided" in exc.detail
    assert "\n" in exc.detail  # newlines survive redaction


def test_403_raises_auth_with_status_line_fallback(configured):
    def handler(request):
        return httpx.Response(403, headers={"content-type": "text/plain"}, content=b"Forbidden")

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_auth"
    assert "403" in exc.detail


def test_500_raises_http(configured):
    def handler(request):
        return httpx.Response(
            500, headers=JSON, content=json.dumps({"error": {"message": "upstream exploded"}}).encode()
        )

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_http"
    assert "upstream exploded" in exc.detail


def test_500_non_json_body_includes_status_text(configured):
    def handler(request):
        return httpx.Response(502, headers={"content-type": "text/html"}, content=b"<h1>Bad gateway</h1>")

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_http"
    assert "502" in exc.detail
    assert "Bad gateway" in exc.detail


def test_timeout_mid_request_raises_timeout(configured):
    def handler(request):
        raise httpx.TimeoutException("read timeout", request=request)

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_timeout"


def test_unconfigured_when_url_or_key_missing(configured, monkeypatch):
    monkeypatch.setattr(config, "ai_api_url", lambda: None)
    monkeypatch.setattr(config, "ai_api_key", lambda: None)

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("no request may be attempted when unconfigured")

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_unconfigured"

    monkeypatch.setattr(config, "ai_api_url", lambda: URL)
    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_unconfigured"


def test_byte_cap_ends_cleanly(configured):
    """A stream totalling > 256 KiB stops at the cap with no exception, cleanly."""
    chunk_text = "x" * 10_000
    total_chunks = 40  # 400 000 bytes offered, cap is 262 144

    def handler(request):
        return httpx.Response(
            200,
            headers=SSE,
            content=_sse(*[_delta(chunk_text) for _ in range(total_chunks)]),
        )

    status, chunks = _collect(ai.stream_reply("s", [], transport=_transport(handler)))
    joined = "".join(chunks)
    assert status == ai.CAPPED  # the budget, not the provider, ended this reply
    assert len(joined) == ai.CAP_BYTES == 256 * 1024
    assert joined.strip("x") == ""


def test_non_event_stream_content_type_raises_protocol(configured):
    def handler(request):
        return httpx.Response(
            200,
            headers=JSON,
            content=json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode(),
        )

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_protocol"


def test_unknown_non_data_line_raises_protocol(configured):
    def handler(request):
        return httpx.Response(200, headers=SSE, content=b"event: ping\n\ndata: {}\n")

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_protocol"


def test_comment_lines_are_tolerated(configured):
    def handler(request):
        return httpx.Response(
            200, headers=SSE, content=b": keep-alive\n\n" + _sse(_delta("ok"), "[DONE]")
        )

    status, chunks = _collect(ai.stream_reply("s", [], transport=_transport(handler)))
    assert status == ai.DONE
    assert "".join(chunks) == "ok"


def test_malformed_json_data_line_raises_protocol(configured):
    def handler(request):
        return httpx.Response(200, headers=SSE, content=b"data: {not json}\n\n")

    exc = _collect_error(ai.stream_reply("s", [], transport=_transport(handler)))
    assert exc.code == "ai_protocol"


def test_eof_without_done_marker_is_not_done(configured):
    """A stream that just stops (no `data: [DONE]`) is EOF, never a clean completion."""

    def handler(request):
        return httpx.Response(200, headers=SSE, content=_sse(_delta("half a thou"), _delta("ght")))

    status, chunks = _collect(ai.stream_reply("s", [], transport=_transport(handler)))
    assert status == ai.EOF
    assert "".join(chunks) == "half a thought"


def test_cap_boundary_after_a_complete_fence_is_capped(configured):
    """The nastiest case: the cap lands right AFTER a complete, valid-looking action fence.

    The text is a perfectly executable-looking reply, but the provider never got to finish, so
    the status must still be CAPPED and run_turn must refuse to execute it.
    """
    fence = '```json\n{"actions": [{"op": "set_interests", "tags": ["matcha"]}]}\n```\n'
    filler_len = ai.CAP_BYTES - len(fence)
    filler = "x" * filler_len

    def handler(request):
        return httpx.Response(
            200,
            headers=SSE,
            content=_sse(_delta(filler), _delta(fence), _delta("!")),
        )

    status, chunks = _collect(ai.stream_reply("s", [], transport=_transport(handler)))
    joined = "".join(chunks)
    assert status == ai.CAPPED
    assert joined.endswith("```\n"), "the complete fence is inside the truncated text"
    assert json.loads(joined.split("```json\n")[1].split("\n```")[0])["actions"]


def test_redact_strips_control_keeps_newline_and_caps():
    assert ai.redact("a\x00b\x1bc") == "abc"
    assert ai.redact("keep\nthis\tand that") == "keep\nthis\tand that"
    assert len(ai.redact("x" * 400)) == 300
    assert ai.redact("  padded  ") == "padded"
    # Control characters are dropped before the 300-char cap is applied.
    assert ai.redact(("\x00" + "y" * 2) * 200) == "yy" * 150
