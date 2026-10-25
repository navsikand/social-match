"""The single validated state-write service (PLAN-chat-frontend.md, the round-1 P0 fix).

post_state (mode="agent") and the chat/schedule surfaces (mode="chat") both land here, so there
is exactly one place that writes availability/windows/tags/events/busy/wants. Grant handling is
the mode split:

  mode="agent" — today's /api/state semantics, byte-identical (the 273-test suite is the
                 regression proof): the payload IS the current truth, including consent —
                 matching is re-granted or left via the cascade, publication renews or withdraws.
  mode="chat"  — GRANT-PRESERVING: the payload's consent block is an echoed snapshot, never
                 applied. No grant row is created, renewed, restored or withdrawn. Grants keep
                 whatever Settings last made them; availability data is inert until a live
                 matching grant exists, exactly like the existing refusal path.

Callers own validation (StatePayload is already validated) and the transaction: statewrite
runs inside the caller's `db.immediate` and never opens or commits one itself. The chat layer
performs its optimistic-conflict and account-liveness revalidation in that same transaction
before calling in (CONTRACT-chat.md §3).
"""
import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

from server import config, db, interests, logging as smlog, matching, scheduling, wording

log = smlog.get("state")
from server.schema import StatePayload

_UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


class StaleState(Exception):
    """Raised by chat-mode callers' checks (not here) — kept here so both layers share the type."""


def canonical_hash(payload: StatePayload) -> str:
    """sha256 of the validated payload as sorted-key JSON — stable across resends."""
    canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _void_uncovered_plans(conn, user_id: int, now: str) -> int:
    """FLOW §5: replacing availability must invalidate derived state in the same transaction.

    Any live plan (proposed/a_confirmed/both_confirmed on an open/matched candidate) whose
    window is no longer fully covered by ONE of this user's live windows is voided, its
    candidate is voided, and both users get a withdrawal notice. Returns the number voided.
    """
    rows = conn.execute(
        "SELECT p.id AS proposal_id, p.window_start, p.window_end,"
        " c.id AS candidate_id, c.user_a, c.user_b, c.state AS cand_state FROM proposals p"
        " JOIN candidates c ON c.id = p.candidate_id"
        " WHERE (c.user_a=? OR c.user_b=?) AND p.state IN ('proposed','a_confirmed','both_confirmed')"
        " AND c.state IN ('open','matched')",
        (user_id, user_id),
    ).fetchall()
    voided = 0
    for row in rows:
        covered = conn.execute(
            "SELECT 1 FROM availability_windows"
            " WHERE user_id=? AND starts_at <= ? AND ends_at >= ? LIMIT 1",
            (user_id, row["window_start"], row["window_end"]),
        ).fetchone()
        if covered is not None:
            continue
        conn.execute(
            "UPDATE proposals SET state='voided' WHERE candidate_id=?"
            " AND state IN ('proposed','a_confirmed','both_confirmed')",
            (row["candidate_id"],),
        )
        conn.execute(
            "UPDATE candidates SET state='voided', closed_at=? WHERE id=?",
            (now, row["candidate_id"]),
        )
        tz = scheduling.display_tz(conn, row)
        msg = wording.revocation_text(row["window_start"], row["window_end"], tz)
        for who in (row["user_a"], row["user_b"]):
            db.push_inbox(conn, who, "revocation", msg)
        voided += 1
    return voided


def _to_utc(local_s: str, tz: str) -> str:
    dt = datetime.datetime.fromisoformat(local_s.strip()).replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(datetime.UTC).strftime(_UTC_FMT)


def _withdraw_live_grant(conn, user_id: int, kind: str, now: str) -> int:
    """Withdraw any live grant of this kind. Returns the number of rows withdrawn."""
    cur = conn.execute(
        "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind=? AND withdrawn_at IS NULL",
        (now, user_id, kind),
    )
    return cur.rowcount


def _upsert_grant(conn, user_id: int, kind: str, tier: str | None, now: str) -> int:
    """CONTRACT §7: one live row per (user, kind) — withdraw old, insert version+1."""
    _withdraw_live_grant(conn, user_id, kind, now)
    row = db.one(conn, "SELECT MAX(version) AS v FROM grants WHERE user_id=? AND kind=?", (user_id, kind))
    version = (row["v"] or 0) + 1 if row is not None else 1
    conn.execute(
        "INSERT INTO grants (user_id, kind, tier, version, fields_json, audience, granted_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            user_id,
            kind,
            tier,
            version,
            json.dumps({"audience": "matched_users_only"}),
            "matched_users_only",
            now,
            db.utcnow(days=config.grant_ttl_days()),
        ),
    )
    return version


def _log_consent(conn, user_id: int, action: str, scope: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (db.user_hash(user_id), action, scope, json.dumps(detail), db.utcnow()),
    )


def write_state(
    conn,
    user_id: int,
    payload: StatePayload,
    *,
    mode: str,
    agent_id: int | None = None,
) -> dict:
    """Apply a validated StatePayload inside the caller's open `db.immediate` transaction.

    Returns the response dict post_state wraps for HTTP. See the module docstring for the
    mode split; agent mode is byte-identical to the former inlined implementation.
    """
    if mode not in ("agent", "chat"):
        raise ValueError(f"unknown statewrite mode {mode!r}")

    now = db.utcnow()
    expires_at = db.utcnow(days=config.state_ttl_days())
    digest = canonical_hash(payload)

    prev = db.one(conn, "SELECT payload_hash FROM availability WHERE user_id=?", (user_id,))
    changed = prev is None or prev["payload_hash"] != digest
    # Defaults: the refusal path stores no interests, but still answers with the shape.
    canonical: list[str] = []
    mapped: list[dict] = []
    unscored: list[str] = []

    if mode == "agent" and not payload.consent.matching:
        # CONTRACT S5: consent.matching false stores NOTHING but a consent_log entry.
        # The payload is the current truth: instead of writing availability, run the
        # leave-matching cascade — withdraw the grant, drop availability (windows
        # cascade), void every live plan involving this user, notify counterparts.
        live = db.one(
            conn,
            "SELECT 1 FROM grants WHERE user_id=? AND kind='matching'"
            " AND withdrawn_at IS NULL LIMIT 1",
            (user_id,),
        )
        if live is not None or prev is not None:
            changed = True
        # The cascade exclusively owns the withdrawal AND its single audit row
        # (gate-review P2: state used to log its own withdraw:matching too,
        # producing duplicate consent_log records for one refusal).
        scheduling._leave_cascade(conn, user_id, "state_payload")
        _ = now  # refusal path: `now` only annotated the grant row; cascade uses its own clock
    else:
        if mode == "agent":
            version = _upsert_grant(conn, user_id, "matching", None, now)
            _log_consent(conn, user_id, "grant:matching", "matching", {"version": version})
        conn.execute(
            "INSERT INTO availability (user_id, written_by_agent_id, payload_hash, observed_at,"
            " expires_at, source) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET written_by_agent_id=excluded.written_by_agent_id,"
            " payload_hash=excluded.payload_hash, observed_at=excluded.observed_at,"
            " expires_at=excluded.expires_at, source=excluded.source",
            (
                user_id,
                agent_id,
                digest,
                now,
                expires_at,
                payload.free_time.source,
            ),
        )

        # Windows: local wall time + tz → UTC strings (CONTRACT §2/§5).
        conn.execute("DELETE FROM availability_windows WHERE user_id=?", (user_id,))
        for win in payload.windows:
            conn.execute(
                "INSERT INTO availability_windows (user_id, starts_at, ends_at, tz) VALUES (?,?,?,?)",
                (user_id, _to_utc(win.start, win.tz), _to_utc(win.end, win.tz), win.tz),
            )

        # Interests: store the CANONICAL tag (what the matcher scores and what a card
        # shows) plus every raw string the student actually said, so we can tell them
        # "matcha -> coffee" instead of rejecting their word.
        canonical, mapped, unscored = interests.normalize_many(payload.tags)
        conn.execute("DELETE FROM hobby_tags WHERE user_id=?", (user_id,))
        for tag in canonical:
            conn.execute(
                "INSERT OR REPLACE INTO hobby_tags (user_id, tag) VALUES (?,?)", (user_id, tag)
            )
        conn.execute("DELETE FROM interest_raw WHERE user_id=?", (user_id,))
        for entry in mapped:
            conn.execute(
                "INSERT OR REPLACE INTO interest_raw (user_id, raw, canonical) VALUES (?,?,?)",
                (user_id, entry["raw"], entry["tag"]),
            )
        for raw in unscored:
            conn.execute(
                "INSERT OR REPLACE INTO interest_raw (user_id, raw, canonical) VALUES (?,?,NULL)",
                (user_id, raw),
            )

        # Wants persist now (schema.sql user_wants), replace-style like tags.
        # The refusal branch deliberately does NOT touch them: a refusal stops
        # matching, it doesn't erase preferences (they're inert without a live
        # matching grant).
        # Pairing signals: replace-style, like tags. Absent == cleared (the payload is
        # the current truth), so a student who drops an event stops being matched on it.
        conn.execute("DELETE FROM user_events WHERE user_id=?", (user_id,))
        for ev in payload.events:
            conn.execute(
                "INSERT OR REPLACE INTO user_events"
                " (user_id, url, category, starts_at, ends_at, tz) VALUES (?,?,?,?,?,?)",
                (
                    user_id,
                    ev.url,
                    ev.category,
                    _to_utc(ev.start, ev.tz),
                    _to_utc(ev.end, ev.tz),
                    ev.tz,
                ),
            )
        conn.execute("DELETE FROM user_load WHERE user_id=?", (user_id,))
        if payload.busy_load is not None:
            conn.execute(
                "INSERT INTO user_load (user_id, busy_load) VALUES (?,?)",
                (user_id, payload.busy_load),
            )

        conn.execute("DELETE FROM user_wants WHERE user_id=?", (user_id,))
        for want in payload.wants:
            conn.execute(
                "INSERT OR REPLACE INTO user_wants (user_id, want) VALUES (?,?)",
                (user_id, want),
            )

    if mode == "agent":
        # Consent: publication grants are processed on both paths — they are consent records,
        # independent of whether availability is being stored. The payload is the CURRENT
        # TRUTH: a tier absent from the payload withdraws any live grant of that kind.
        publication = payload.consent.publication
        if publication is not None:
            version = _upsert_grant(conn, user_id, "publication", publication.tier, now)
            _log_consent(
                conn,
                user_id,
                "grant:publication",
                "publication",
                {"version": version, "tier": publication.tier},
            )
        elif _withdraw_live_grant(conn, user_id, "publication", now):
            _log_consent(
                conn, user_id, "withdraw:publication", "publication", {"source": "state_payload"}
            )

    # Audit row: agent mode keeps the historical action name so existing tooling reads the
    # same rows; chat mode records its own, so a chat-originated write is distinguishable.
    if mode == "agent":
        _log_consent(
            conn,
            user_id,
            "state:received",
            "state",
            {
                "payload_hash": digest,
                "student_confirmed_at": payload.free_time.student_confirmed_at,
            },
        )
    else:
        _log_consent(
            conn,
            user_id,
            "state:written",
            "chat",
            {"payload_hash": digest, "mode": mode},
        )

    if (mode == "agent" and payload.consent.matching) or mode == "chat":
        # FLOW §5 / reviewer P1 #5: invalidation runs BEFORE generation so a dying pair
        # doesn't suppress the fresh candidate over what's still shared — the student's
        # end state after one POST is already up to date. Chat mode always runs this: it
        # only writes when data changed, and uncovered plans must die the same way.
        _void_uncovered_plans(conn, user_id, now)
        new_ids = matching.generate_for_user(conn, user_id)
        matching.refresh_events(conn, user_id)
        log.info(
            "state_write user=%s mode=%s changed=%s windows=%s new_candidates=%s",
            user_id, mode, changed, len(payload.windows), len(new_ids),
        )

    return {
        "changed": changed,
        "expires_at": expires_at,
        # Tell the student what their word became. `scored` is what matching compares;
        # `kept_unscored` is theirs alone and never appears on a card.
        "interests": {
            "scored": canonical if (mode == "agent" and payload.consent.matching) or mode == "chat" else [],
            "mapped": mapped if (mode == "agent" and payload.consent.matching) or mode == "chat" else [],
            "kept_unscored": unscored if (mode == "agent" and payload.consent.matching) or mode == "chat" else [],
        },
        # The links the server actually stored, so the client can show the student exactly
        # what is now visible to a match who has the same event.
        "events": {
            "stored": [ev.url for ev in payload.events]
            if (mode == "agent" and payload.consent.matching) or mode == "chat"
            else []
        },
    }
