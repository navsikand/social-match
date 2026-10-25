"""OpenAI chat-completions client (CONTRACT-chat.md §2).

One protocol only: OpenAI chat-completions server-sent events. `data: {json}` lines
carrying `choices[0].delta.content`, terminated by `data: [DONE]`. Any other shape is a
typed `AIError("ai_protocol", ...)` surfaced to the caller — never a silent empty reply.

Injection seam for tests: the module-level `transport=None` kwarg of `stream_reply`
(an `httpx.MockTransport` in tests). Nothing here opens a socket at import time; the
only network call happens inside the returned async generator.
"""
from __future__ import annotations

import json
import unicodedata
from typing import AsyncGenerator

import httpx

from server import config

#: Hard ceiling on accumulated reply text (bytes of UTF-8). Hitting it ends the stream
#: cleanly — the caller still decides whether a capped reply is executable.
CAP_BYTES = 256 * 1024

#: Completion-status contract (gate review P1 #5).
#:
#: CPython forbids `return value` inside an ASYNC generator ("SyntaxError: 'return' with value in
#: async generator"), so `StopAsyncIteration.value` cannot carry the status here. The status
#: therefore travels as the FINAL ITEM the stream yields — a `Completion` — which is exactly
#: equivalent for the consumer: `chat.run_turn` reads it and executes actions ONLY on DONE. A
#: stream that yields no Completion (or an unknown one) means "not a clean finish" (EOF).
DONE = "done"  #: the provider sent the `data: [DONE]` terminator
CAPPED = "capped"  #: the CAP_BYTES budget ended the read
EOF = "eof"  #: the stream ended without a terminator


class Completion:
    """The last item of a reply stream: how the stream ended. Never reply text."""

    __slots__ = ("status",)

    def __init__(self, status: str):
        self.status = status

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Completion({self.status!r})"


def finished(status: str = DONE) -> Completion:
    """Convenience for scripted streams (tests, demo): yield this as the final item."""
    return Completion(status)

#: How much of an error body is worth echoing back into a detail string.
ERROR_BODY_LIMIT = 2048

_REDACT_LIMIT = 300


class AIError(Exception):
    """Typed provider failure. ``detail`` is display-safe (already redacted)."""

    def __init__(self, code: str, detail: str):
        self.code = code  # ai_unconfigured | ai_auth | ai_http | ai_timeout | ai_protocol
        self.detail = redact(detail)
        super().__init__(f"{code}: {self.detail}")


_BEARER_RE = None  # compiled lazily: module import must never touch the network or env secrets
_TOKEN_RE = None


def _redact_secrets(text: str) -> str:
    """Finding 11: configured credentials and credential-shaped material never survive redaction.

    The order matters: replacement happens BEFORE the control-strip and the length cap, so a
    secret straddling the 300-char boundary cannot keep half of itself visible.
    """
    global _BEARER_RE, _TOKEN_RE
    import re

    if _BEARER_RE is None:
        _BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)
        _TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")

    key = config.ai_api_key()
    if key:
        text = text.replace(key, "[redacted]")
    url = config.ai_api_url()
    if url:
        text = text.replace(url, "[redacted]")
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _TOKEN_RE.sub("[redacted]", text)
    return text


def redact(text: str) -> str:
    """Display-safe text: secrets replaced, control characters dropped (newline/tab kept), capped at 300."""
    if not isinstance(text, str):
        text = str(text)
    text = _redact_secrets(text)
    kept: list[str] = []
    for char in text:
        if char in ("\n", "\t"):
            kept.append(char)
        elif unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            continue
        else:
            kept.append(char)
    return "".join(kept)[:_REDACT_LIMIT].strip()


def _message_from_error_body(body: bytes) -> str | None:
    """OpenAI-style ``{"error": {"message": ...}}`` → the message, else None."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message
    elif isinstance(error, str) and error.strip():
        return error
    message = parsed.get("message")
    if isinstance(message, str) and message.strip():
        return message
    return None


def _http_detail(response: httpx.Response, body: bytes) -> str:
    """Redacted detail for a non-success status: provider message, else status + snippet."""
    message = _message_from_error_body(body)
    if message is not None:
        return message
    reason = getattr(response, "reason_phrase", "") or ""
    head = f"provider returned HTTP {response.status_code}"
    if reason:
        head = f"{head} {reason}"
    snippet = body.decode("utf-8", "replace").strip()
    return f"{head}: {snippet}" if snippet else head


def _delta_content(chunk: object) -> str | None:
    """Extract ``choices[0].delta.content``; ``None`` means "nothing to yield in this chunk"."""
    if not isinstance(chunk, dict):
        raise AIError("ai_protocol", "stream chunk is not a JSON object")
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIError("ai_protocol", "stream chunk carries no choices (not an OpenAI completion)")
    first = choices[0]
    if not isinstance(first, dict):
        raise AIError("ai_protocol", "stream chunk choice is not a JSON object")
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if isinstance(content, str):
        return content or None
    return None


async def _read_error_body(response: httpx.Response) -> bytes:
    """Read at most ERROR_BODY_LIMIT bytes of a failing response."""
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) >= ERROR_BODY_LIMIT:
            break
    return bytes(buffer[:ERROR_BODY_LIMIT])


async def stream_reply(
    system: str,
    messages: list[dict],
    *,
    transport=None,  # httpx.MockTransport — tests only (see module docstring)
) -> AsyncGenerator[str | Completion, None]:
    """Stream assistant text deltas from the configured provider.

    Raises `AIError` for every failure path: unconfigured, auth, http, timeout, protocol.

    The LAST item yielded is always a `Completion` carrying DONE / CAPPED / EOF. Only DONE
    means the reply is complete; the caller must not execute a truncated (CAPPED) or
    unterminated (EOF) reply.
    """
    url = config.ai_api_url()
    key = config.ai_api_key()
    if not url or not key:
        raise AIError("ai_unconfigured", "the AI provider is not configured")

    payload = {
        "model": config.ai_model(),
        "messages": [{"role": "system", "content": system}, *messages],
        "stream": True,
        "max_tokens": config.ai_max_tokens(),
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    total = 0
    try:
        async with (
            httpx.AsyncClient(
                timeout=config.ai_timeout_seconds(), transport=transport
            ) as client,
            client.stream("POST", url, json=payload, headers=headers) as response,
        ):
            if response.status_code in (401, 403):
                body = await _read_error_body(response)
                raise AIError("ai_auth", _http_detail(response, body))
            if response.status_code != 200:
                body = await _read_error_body(response)
                raise AIError("ai_http", _http_detail(response, body))

            content_type = response.headers.get("content-type", "")
            if content_type and "event-stream" not in content_type.lower():
                body = await _read_error_body(response)
                snippet = body.decode("utf-8", "replace").strip()
                detail = f"provider returned {content_type!r} instead of text/event-stream"
                raise AIError("ai_protocol", f"{detail}: {snippet}" if snippet else detail)

            async for raw_line in response.aiter_lines():
                line = raw_line.rstrip("\r")
                if not line.strip():
                    continue  # blank separator between SSE events
                if line.startswith(":"):
                    continue  # SSE comment / keep-alive
                if not line.startswith("data:"):
                    raise AIError(
                        "ai_protocol", f"unexpected non-data SSE line: {line[:120]}"
                    )
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    yield finished(DONE)
                    return
                if not data:
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    raise AIError(
                        "ai_protocol", f"malformed SSE data line: {data[:120]}"
                    ) from None

                content = _delta_content(chunk)
                if not content:
                    continue

                encoded = content.encode("utf-8")
                remaining = CAP_BYTES - total
                if len(encoded) > remaining:
                    tail = encoded[:remaining].decode("utf-8", "ignore")
                    if tail:
                        yield tail
                    yield finished(CAPPED)  # the budget ended the read: NOT a complete reply
                    return
                total += len(encoded)
                yield content
    except httpx.TimeoutException as exc:
        raise AIError("ai_timeout", f"provider timed out: {exc!r}") from exc
    except httpx.HTTPError as exc:
        raise AIError("ai_http", f"provider request failed: {exc!r}") from exc

    yield finished(EOF)  # the stream ended without `data: [DONE]`
