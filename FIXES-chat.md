# Fix wave — gate review REWORK(ed), 15 findings. Dispatch contract. No scope cuts.

Base: 377 tests green, demo 17/17. The expensive reviewer BLOCKED deploy. Findings numbered
as in its report; the owner column is law. Each lane ends with `uv run pytest` green and
`uv run python demo/scenario.py` → ALL 17 BEATS PASS (where its slice allows; cross-lane code
may transiently miss — mark Week interlock bits clearly in the report).

## Ownership

| Lane | Files |
|---|---|
| **R1 security-core** | `server/chat.py`, `server/ai.py`, `server/activity.py`, `server/negotiate.py`, `tests/test_chat.py`, `tests/test_ai_client.py`, `tests/test_negotiate.py`, `demo/scenario.py` beat-17 fixture ONLY (the scripted generator) |
| **R2 schedules** | `server/schedules.py`, `server/webui/templates/schedule.html`, `tests/test_schedules.py` |
| **R4 ui** | `server/webui/templates/chat.html`, `tests/test_chat_ui.py` |

Frozen: schema.sql, db.py, statewrite.py, state.py, app.py, sweep.py, config.py — errors there are
SEAMS to report, not patch. Nothing else outside your column. `uv run` only; NEVER uv sync/add/lock.

## R1 — the security core (findings 1, 2, 3, 5, 6-core, 7, 8, 10, 12, 13-core, demo fixture)

**F1 — delegation guard INSIDE the activity transaction.** `server/activity.py`: `propose(...)`
and `respond(...)` gain keyword `autonomous: bool = False`. When True, inside the existing
`db.immediate`, immediately after the existing authorization reads and BEFORE any write,
re-read `grants` for kind='delegation' of `uid` (`withdrawn_at IS NULL AND expires_at > now`)
and `users.deleted_at IS NULL`; failure → `SchedulingError(409, "delegation_required",
"your delegation was off when this would have applied")`. chat.py `_apply_negotiation_op`
passes `autonomous=True` (it is the assistant path); the MANUAL `/api/chat/offer-respond`
endpoint calls the cores WITHOUT it (a human click is consent). Test: withdraw delegation
AFTER the precheck but BEFORE the core's write — monkeypatch the offer write… simplest
deterministic proof: call `propose(..., autonomous=True)` in a direct SQL-driven test where
the grant is deleted while holding… do it via a `db.immediate` transaction ordering: patch
`chat._apply_negotiation_op`'s precheck to run, then delete the grant, then call the core
autonomous=True and assert refusal + zero offer rows. Update
`test_delegation_withdrawn_mid_stream_applies_nothing` to this atomicity (its old form passes
even with the invariant removed — the reviewer called that out).

**F2 — value validation belongs to the cores; keys are not values.** In `activity.py`
`propose`/`respond`, BEFORE any write and regardless of caller: `url = offers.validate_url(url)`
(the existing strict validator: https, ASCII, ≤300, real domain, no password, no fragment, no IP,
strip `?query`) — refusal → `SchedulingError(422, "payload_forbidden", reason)`; `tag` must be in
the vocabulary `offers`/`activity` already defines (`TAG_VOCAB`-equivalent server-side), `reason`
in `offers.REASONS`, all non-empty strings. Remove the reliance on endpoint-side validation in
 BOTH chat paths (`_parse_negotiation_ops` key check stays, add nothing there; the manual
`_parse_offer_body` may delegate its tag/url checks to the core — do not double-validate with
different rules). Counterpart notes (`_structured_note`, manual + worker paths) render ONLY the
validated/committed url+tag+reason. Tests: an offer op with url containing `SECRET-PROSE-7`,
`"my windows are Sunday"`, a `javascript:` url, an oversized url → refused, no offer row, no
counterpart note; same via the manual endpoint.

**F3 — deleted accounts must not gain chat rows from in-flight provider work.** In
`server/chat.py`: `persist_final_assistant`, `persist_note`, `_store_pending` gain an
in-txn liveness guard: every wrapper that WRITES chat_messages/chat_pending re-reads
`users.deleted_at` inside its `db.immediate` and silently no-ops when the account is gone
(return None; `run_turn` treats it as a dropped turn, no SSE error beyond the existing flow).
`reserve_turn`'s `chat_turns` writes get the same guard (a deleted account must not reserve).
Tests: delete the account (direct SQL `deleted_at`) at each completion point — success stream
completing, proposal preview, provider error note — assert zero new chat_messages/chat_pending/
chat_turns rows. Also assert the END state of `delete_account` (existing cascade) swallows
an in-flight turn cleanly.

**F5 — clean completion or nothing executes.** `ai.stream_reply` MUST signal completion
status: `return` (StopAsyncIteration.value) equals `"done"` when the terminator `[DONE]` was
seen, `"capped"` when the 256 KiB budget ended the read, `"eof"` when the stream ended without
`[DONE]`. `chat.run_turn` switches from `async for` to a manual `__anext__` loop capturing
`StopAsyncIteration.value` (None → `"eof"`). Actions (state ops, previews, negotiation ops)
execute ONLY on status `"done"`; `"capped"`/`"eof"` → persist the text (liveness-guarded), emit
a `note` ("that reply was cut off — nothing was applied"), and `done`. Update EVERY monkeypatch
fixture that streams scripted text (test_chat.py, test_negotiate.py, demo beat_17's `scripted`)
to end with `return "done"`. New ai tests: EOF-without-DONE → status "eof"; cap boundary ending
exactly after a valid JSON fence → status "capped"; [DONE] → "done". Add run_turn tests: cut-off
stream with a complete-looking action fence applies NOTHING.

**F6-core — initial offers become real (worker-side deterministic trigger).** In
`server/negotiate.py tick_once`: second scan — candidates `state IN ('open','matched')` with
current non-void proposal and NO offer yet. For each: the side with live delegation acts; if
both sides have it, the LOWER uid acts (deterministic, avoids double-offer); budget/lock/
ceiling rules identical to the respond path; attempts keyed `ref=f"initial:{candidate_id}"`
cap 2. The acting assistant's system_extra gets candidate + my-profile + windows + tag vocab
so it can pick a shared tag + url (validate via the cores — F2 makes stray tags/urls safe).
Tests: fresh candidate + both sides delegated → exactly ONE offer exists after one tick
(next tick: other side responds); one side only → that side offers; neither → silent.

**F7 — durable attempt cap.** `chat.purge_chat` currently deletes all `chat_turns` older than
the rate window; negotiation rows with non-null ref must SURVIVE while their target is
actionable: exemption = `kind='negotiation' AND ref IS NOT NULL AND (offer id still exists AND
state='offered', or ref starts 'initial:' and candidate still open/matched with no offer)`.
Implement via a small SQL join in purge_chat (frozen-file seam? NO — purge_chat lives in
chat.py, yours). Test: two failed ticks → advance `_FIXED_NOW` past 10 min → run
`chat.purge_chat(conn, now)` → tick → STILL no third provider attempt (cap held) and no
duplicate manual note.

**F8 — busy ticks must not burn attempts or budget.** Reorder in negotiate tick: lock check
FIRST (`lock.locked()` → skip: no reserve, no rows, no notes); then autonomy ceiling (12 in
window, see F12); then reserve; then revalidate under the lock; then run the turn. Extend the
busy test: assert zero new chat_turns rows and the attempt count unchanged.

**F10 — confirm/cancel/expiry must COMMIT their effects.** The current code raises
`_ConfirmError` INSIDE `db.immediate`, which rolls the transaction — cancellation returns
"ok" but the row survives (and stays confirmable!), expiry too, and conflict keeps riders alive.
Restructure: compute outcome flags INSIDE the transaction (row read + deletes staged), let the
with-block EXIT normally (commit), THEN map outcomes to responses OUTSIDE; conflict must DELETE
the pending row too (the batch is dead — re-ask is the recovery, per PLAN §B), expiry deletes,
cancel deletes. Tests: cancel → row GONE + subsequent confirm → 404 not_found; expiry → row
GONE + re-confirm → 404; conflict → row GONE + interest_raw rider did NOT apply.

**F12 — autonomy ceiling counts ALL turns.** The N-lane ceiling check currently counts only
kind='negotiation'; PLAN §C2 says no autonomous turn once the user is past 12 turns in the
window REGARDLESS of kind. Fix the count SQL; test: 12 typed turns (or a mix summing to 12)
→ tick skips without rows.

**F13-core — pending semantics:** `/api/chat/confirm` must REJECT `kind='ics'` rows with
`422 payload_forbidden "preview belongs to /api/schedule/ics/confirm"`; split mixed windows+
events pending payload into typed sections `{"windows": [...], "events": [...]}` so the surface
display and confirm can render groups (keep `ops` for the apply merge EXACTLY as today — apply
semantics unchanged, only the payload's `items` becomes `{"windows": …, "events": …}`; update
test fixtures accordingly). Chat history `pending` representation carries the same sections.

**Demo beat 17:** update its scripted generator to `return "done"` and keep it passing.

REPORT: per-finding ac diff summary, the exact ai status contract you shipped, any seam you
could not close without touching frozen files (report, don't patch).

## R2 — schedules (findings 4, 9, 15; plus 13's ICS side)

**F4 — confirm must compare the STORED hash.** `server/schedules.py` ics confirm currently
recomputes the hash at confirm-entry and compares against ITSELF. Fix: inside the confirm
transaction, `current_state_hash(conn, uid)` is compared against the PENDING ROW's
`state_hash` (captured at upload); mismatch → 409 conflict AND delete the pending row (commit
the deletion — see the R1 finding-10 pattern; do not raise through the transaction), stale
import discarded. Test: upload → change windows (util/statewrite via the manual endpoint or
chat lane helper) → confirm → 409, row gone, windows unchanged (the newer windows survive).

**F9 — malformed input must never masquerade as free time.** Parser hardening in
`server/schedules.py`:
- every VEVENT must carry BOTH parseable DTSTART and DTEND; an invalid or missing timestamp is
  a per-event ERROR appended to the 422 list (name the event index and property);
- balanced component boundaries: `BEGIN:VCALENDAR`/`END:VCALENDAR` unmatched, nested
  `BEGIN:VEVENT` inside a VEVENT, or EOF inside an unterminated VEVENT/VALARM block → 422
  with the structural reason; never a silent success;
- a component OTHER than VEVENT/VCALENDAR (e.g. VTIMEZONE) is skipped as a whole component
  (reviewer's earlier note) but a VTIMEZONE carrying a TZID reference inside its events is
  still refused via the events' TZID rule — no change to that rule.
Tests: fixture without DTEND; fixture with `BEGIN:VEVENT` unterminated at EOF; nested VEVENT;
calendar missing closing tag; date value `2026-13-45`; each → 422 naming the fault; zero
preview rows; zero pending rows.

**F15 — byte-bound during unfolding.** Enforce the 2 KiB limit on ACCUMULATED UTF-8 BYTES
incrementally: after appending each continuation segment check `len(line_bytes)` and reject
the moment it exceeds — do not build the fully-unfolded string first. Report the limit in
bytes (it already claims bytes). Test: a single folded line whose total is >2048 bytes in
multi-byte UTF-8 rejected promptly; a 2047-byte line passes.

**F13-ICS — route ICS previews away from chat/confirm.** Ensure the pending payload for ICS
uses `items` (only windows candidates) as today — no `ops` needed at all; the schedule confirm
path is the only consumer (chat/confirm will refuse kind='ics' — R1). No schema change. Test
that schedule.pending GET and the confirm path work end-to-end and that `/api/chat/confirm`
refuses an ICS row with the 422 above (cross-lane assertion: if R1's refusal hasn't landed
mid-flight, write the test as a direct call expectation guarded by the route's behaviour —
mark interlock in the report).

## R4 — chat.html UI (findings 6-UI, 13-UI, 14)

**14 — status must survive stream end.** Today any event (including error-then-done) clears
the status line. Fix: a sticky flag set on `error`/`conflict` events; status hidden only when a
subsequent SUCCESSFUL completion (done without the sticky flag) occurs; new user send clears
it. Test via template assertions (a script-level constant/flag name — assert its presence in
the rendered page and the clearing logic comments) — UI tests can't run JS, so assert the
template text markers precisely; keep it deterministic.

**6-UI — manual offer card.** chat.html renders, from history's `pending` and any offer state
exposed by `/api/chat/history` state summary (state has matching/delegation; offers live in the
inbox/dashboard — read what `${state}` exposes today), an OFFER SECTION when the shared state
carries one; simplest honest surface: a "Negotiations" panel that fetches
`/api/activity/{candidate_id}` per open card candidate (endpoint exists; returns the current
offer) and renders Accept / Counter / Pass controls: Accept → POST /api/chat/offer-respond
{"action":"accept",candidate_id,offer_id}; Counter → inputs (tag select from the ICS-style
vocab list rendered server-side into the template, url text) → action counter with
proposal_version from the activity GET; Pass → reason select (the four REASONS) → action pass.
All text nodes; errors show via the sticky status line (14). The worker's manual-fallback note
("accept, counter or pass it from the chat") now has real controls. Test: template contains the
panel markup + the endpoint references; `/chat` renders with delegation section visible.

**13-UI — ridealong display.** When `pending` carries `windows`/`events` sections (R1's
payload shape), render them grouped; when `kind == "ics"`, the Confirm button posts to
`/api/schedule/ics/confirm` with the row id and ALL picks (or checkbox selection) instead of
`/api/chat/confirm`; riders ("also updating: interests") shown as muted lines from the
sections/ops.

Keep vanilla JS, text nodes, existing styles; no new files.

## Integration (integrator, after lanes)

Docs honesty pass per findings 1,2,10 (privacy.md claim-by-claim corrections);
`uv run pytest` green; demo 17/17; ONE more reviewer gate on the fixes (closure check on the
15 findings); then deploy.
