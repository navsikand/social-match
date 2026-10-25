"""Canonical accepted payload for POST /api/state (CONTRACT §5). P1b owns this file.

This module is the allowlist. Everything the Cubbie skill is allowed to send is described
here; anything else (unknown keys at any level, free-text event titles, extra metadata) is
rejected with 422 `payload_forbidden`. The skill never sends calendar event titles — only
ranges plus tags — and this schema is the code that makes that a guarantee rather than a
convention.
"""
import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from server import db

# Closed vocabulary (CONTRACT §5) — exact order, exact strings.
TAG_VOCAB: list[str] = [
    "bouldering",
    "climbing",
    "hiking",
    "running",
    "gym",
    "swimming",
    "cycling",
    "basketball",
    "soccer",
    "tennis",
    "ultimate_frisbee",
    "board_games",
    "video_games",
    "chess",
    "painting",
    "drawing",
    "photography",
    "pottery",
    "guitar",
    "piano",
    "singing",
    "live_music",
    "edm",
    "techno",
    "rock",
    "jazz",
    "coffee",
    "baking",
    "cooking",
    "language_exchange",
    "debate",
    "anime",
    "cinema",
    "volunteering",
]

WANT_VOCAB: tuple[str, ...] = ("hangout", "event")
MAX_WINDOWS = 20
MAX_TAGS = 5
MAX_WINDOW_HOURS = 8
WINDOW_HORIZON_DAYS = 30

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_local(value: object, field: str) -> datetime.datetime:
    """Local wall-time ISO string ('2026-10-29T19:00' / '...:00') → naive datetime."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty local ISO datetime string")
    try:
        parsed = datetime.datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid ISO local datetime: {value!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{field} must be local wall time without an offset (send tz separately)")
    return parsed


def _now_utc() -> datetime.datetime:
    return datetime.datetime.strptime(db.utcnow(), _UTC_FMT).replace(tzinfo=datetime.UTC)


class _ForbidExtra(BaseModel):
    """extra='forbid' at every level — the recursive allowlist rule."""

    model_config = ConfigDict(extra="forbid")


class Window(_ForbidExtra):
    start: str
    end: str
    tz: str

    @field_validator("tz")
    @classmethod
    def _tz_must_load(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfoNotFoundError / ValueError / OSError
            raise ValueError(f"tz is not a known IANA timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_span(self) -> "Window":
        start = _parse_local(self.start, "start")
        end = _parse_local(self.end, "end")
        if start >= end:
            raise ValueError("start must be strictly before end")
        hours = (end - start).total_seconds() / 3600.0
        if hours > MAX_WINDOW_HOURS:
            raise ValueError(f"duration {hours:g}h exceeds the {MAX_WINDOW_HOURS}h maximum")
        start_utc = start.replace(tzinfo=ZoneInfo(self.tz)).astimezone(datetime.UTC)
        now = _now_utc()
        horizon = now + datetime.timedelta(days=WINDOW_HORIZON_DAYS)
        if start_utc < now:
            raise ValueError("window starts in the past")
        if start_utc > horizon:
            raise ValueError(f"window starts more than {WINDOW_HORIZON_DAYS} days out")
        return self


class FreeTime(_ForbidExtra):
    source: Literal["student", "cubbie_computed"]
    # PROVENANCE, not proof: the client says "the student saw/approved this". The server
    # cannot verify that a human read anything (CONTRACT §16).
    student_confirmed_at: str | None = None

    @field_validator("student_confirmed_at")
    @classmethod
    def _ack_must_parse(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValueError("student_confirmed_at is not a valid ISO datetime") from exc
        return value

    @model_validator(mode="after")
    def _computed_needs_ack(self) -> "FreeTime":
        if self.source == "cubbie_computed" and not self.student_confirmed_at:
            raise ValueError(
                "free_time.source 'cubbie_computed' requires student_confirmed_at "
                "(student acknowledgement of the computed windows)"
            )
        return self


class Publication(_ForbidExtra):
    tier: Literal["card", "card+name"]


class Consent(_ForbidExtra):
    matching: bool
    # Absent publication == "off": the payload is the current truth (see server/state.py).
    publication: Publication | None = None


class StatePayload(_ForbidExtra):
    windows: list[Window] = Field(default_factory=list, max_length=MAX_WINDOWS)
    tags: list[str] = Field(default_factory=list)
    wants: list[str] = Field(default_factory=list)
    free_time: FreeTime
    consent: Consent

    @field_validator("tags")
    @classmethod
    def _tags_closed_vocab(cls, value: list[str]) -> list[str]:
        unknown = [tag for tag in value if tag not in TAG_VOCAB]
        if unknown:
            raise ValueError(
                f"tag(s) outside the closed vocabulary: {', '.join(unknown)}; "
                f"allowed: {', '.join(TAG_VOCAB)}"
            )
        deduped = list(dict.fromkeys(value))
        if len(deduped) > MAX_TAGS:
            raise ValueError(f"at most {MAX_TAGS} tags allowed, got {len(deduped)}")
        return deduped

    @field_validator("wants")
    @classmethod
    def _wants_whitelist(cls, value: list[str]) -> list[str]:
        unknown = [want for want in value if want not in WANT_VOCAB]
        if unknown:
            raise ValueError(
                f"wants outside {', '.join(WANT_VOCAB)}: {', '.join(unknown)}"
            )
        return list(dict.fromkeys(value))
