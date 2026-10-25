"""Structured logging for Social Match. One place, one switch.

Why this exists: a student typed "find me hangouts" and got silence — the state said
"windows updated: 8 stored" while zero candidates were ever generated, and there was no
record anywhere of WHY. Every decision that can produce a silent no-op now writes a line.

Enable/verbosity
----------------
    LOG_LEVEL=DEBUG      # per-candidate decision lines, request bodies' shapes, SQL counts
    LOG_LEVEL=INFO       # default-on summary lines (startup, each turn, each generate pass)
    LOG_LEVEL=WARNING    # only refusals and failures

Lines go to stderr, which is where the deploy supervisor (tmux / run.sh / systemd) already
collects them. Nothing here ever logs message text, window times, tags or any other student
content: ids, counts, enums and refusals only. That is deliberate — the transcript and the
profile are private and the log is not.

Every line is prefixed `sm.<area>:` so `grep 'sm.match'` gives you one subsystem.
"""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def _level() -> int:
    raw = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    return getattr(logging, raw, logging.INFO) if raw in (
        "CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"
    ) else logging.INFO


def setup() -> logging.Logger:
    """Idempotent: called from app startup and safe to call from any module."""
    global _CONFIGURED
    root = logging.getLogger("sm")
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
        handler.formatter.converter = _utc  # type: ignore[assignment]
        root.addHandler(handler)
        root.setLevel(_level())
        root.propagate = False
        _CONFIGURED = True
    return root


def _utc(*_args):
    import time

    return time.gmtime()


def get(area: str) -> logging.Logger:
    """`get("match")` → the `sm.match` logger (all lines prefixed by area)."""
    setup()
    return logging.getLogger(f"sm.{area}")


def level_name() -> str:
    return logging.getLevelName(logging.getLogger("sm").level)
