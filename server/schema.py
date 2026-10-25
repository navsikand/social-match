"""Canonical accepted payload for POST /api/state (CONTRACT §5). P1b owns this file.

This module is the allowlist. Everything the Cubbie skill is allowed to send is described
here; anything else (unknown keys at any level, free-text event titles, extra metadata) is
rejected with 422 `payload_forbidden`. The skill never sends calendar event titles — only
ranges plus tags — and this schema is the code that makes that a guarantee rather than a
convention.

Interests ("tags") are the one free-text field, and they are bounded: at most MAX_TAG_CHARS
characters each and MAX_TAGS of them, with no control characters. The matcher canonicalises
them into TAG_VOCAB (server/interests.py) so scoring stays comparable; unmatched interests are
kept for the student and never shown to anyone else.
"""
import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from server import db, events as events_mod

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
MAX_TAGS = 8
MAX_TAG_CHARS = 32   # interests may be free text now; bounded so a tag can't smuggle a paragraph
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


class PublicEvent(_ForbidExtra):
    """One public event. A link, a time and a category; nothing else is accepted."""

    url: str
    start: str
    end: str
    tz: str
    category: Literal[events_mod.EVENT_CATEGORIES]  # type: ignore[valid-type]

    @field_validator("url")
    @classmethod
    def _url_is_public_page(cls, value: str) -> str:
        try:
            return events_mod.canonical_url(value)
        except ValueError as exc:
            raise ValueError(str(exc).replace("activity_url", "event url")) from exc

    @field_validator("tz")
    @classmethod
    def _tz_must_load(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"tz is not a known IANA timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _check_span(self) -> "PublicEvent":
        start = _parse_local(self.start, "start")
        end = _parse_local(self.end, "end")
        if start >= end:
            raise ValueError("event start must be strictly before end")
        hours = (end - start).total_seconds() / 3600.0
        if hours > events_mod.MAX_EVENT_HOURS:
            raise ValueError(
                f"event duration {hours:g}h exceeds the {events_mod.MAX_EVENT_HOURS}h maximum"
            )
        zone = ZoneInfo(self.tz)
        now = _now_utc()
        if end.replace(tzinfo=zone).astimezone(datetime.UTC) <= now:
            raise ValueError("event has already ended")
        horizon = now + datetime.timedelta(days=events_mod.EVENT_HORIZON_DAYS)
        if start.replace(tzinfo=zone).astimezone(datetime.UTC) > horizon:
            raise ValueError(f"event starts more than {events_mod.EVENT_HORIZON_DAYS} days out")
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
    # Pairing signals (Track 2). All optional, all bounded, all replaced on every post.
    # `events` are PUBLIC events only (see server/events.py); `events_confirmed_at` is
    # provenance that the student approved that list on their phone, like free_time's.
    # `busy_load` is one coarse number for how heavy the next two weeks look (0 light .. 3
    # heavy), computed from Canvas and the calendar; course names and titles never leave.
    events: list[PublicEvent] = Field(default_factory=list, max_length=events_mod.MAX_EVENTS)
    events_confirmed_at: str | None = None
    busy_load: StrictInt | None = Field(default=None, ge=0, le=3)

    @field_validator("events_confirmed_at")
    @classmethod
    def _events_ack_must_parse(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (ValueError, AttributeError) as exc:
            raise ValueError("events_confirmed_at is not a valid ISO datetime") from exc
        return value

    @model_validator(mode="after")
    def _events_need_ack(self) -> "StatePayload":
        if self.events and not self.events_confirmed_at:
            raise ValueError(
                "events requires events_confirmed_at (student approval of the event list)"
            )
        return self

    @field_validator("tags")
    @classmethod
    def _tags_bounded_free_text(cls, value: list[str]) -> list[str]:
        """Interests are free text now, but tightly bounded.

        A student can say "matcha" even though the vocabulary has no matcha tag; the
        matcher boils it down (server/interests.py). What is still refused: anything long
        enough to carry a sentence, anything with control characters, and more than
        MAX_TAGS entries. That keeps "no free-text blobs" true while letting people name
        their actual interests.
        """
        out: list[str] = []
        for raw in value:
            tag = (raw or "").strip()
            if not tag:
                raise ValueError("tags must not be empty")
            if len(tag) > MAX_TAG_CHARS:
                raise ValueError(
                    f"tag {tag[:20]!r} is {len(tag)} chars; max {MAX_TAG_CHARS}"
                )
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in tag):
                raise ValueError(f"tag {tag[:20]!r} contains control characters")
            out.append(tag)
        deduped = list(dict.fromkeys(out))
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
