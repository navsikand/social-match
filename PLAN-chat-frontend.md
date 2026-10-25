# Plan v2 — our own chat app (the phone agent out of the loop)

Status: seeking approval. No code written. v1 was reviewed: **FIX-PLAN, 12 findings (1×P0, 9×P1, 2×P2)** —
all are integrated below; §0 summarises the turnaround. Facts re-verified after review corrections:
`MAX_WINDOWS = 20`, `MAX_WINDOW_HOURS = 8`, `WINDOW_HORIZON_DAYS` horizon, session cookie `SameSite=Lax`,
`server/state.py` renews matching (:173–174) and renews-or-withdraws publication (:257–275) as side effects —
the P0.

## Context

the phone agent was the interface: its skill read the calendar on the phone, asked consent, POSTed state. It is
inconvenient and unreliable, so the product face becomes **our own chat app**. The LLM behind it speaks the
**OpenAI chat-completions protocol only**, configured entirely in env. The one demo affordance that replaces
"the phone agent reads my calendar" is a **manual schedule page** (define windows by hand; bounded `.ics` upload).
Matching, photos and inbox stay real; activity negotiation is **explicitly out of the chat's scope** (see §F6).

Probes on 25 Oct: `httpx>=0.27` already a runtime dep; `deploy/run.sh` sources `socialmatch.conf`;
nano-gpt live at `/api/v1/messages` AND `api.nano-gpt.com/v1/chat/completions`; the current shell
`NANOGPT_API_KEY` is **rejected** (`invalid_api_key`, 44 chars) — so no live stream has been captured yet and
**deployment is blocked until one valid-key stream + action round-trip passes**.

## 0. What the review changed (v1 → v2)

| # | v1 said | v2 does |
|---|---|---|
| P0 | "reuse `post_state`'s exact code path" | **Extract a shared grant-preserving state-write service**; `post_state` and chat both call it with different modes; chat mode never creates/renews/restores/withdraws grants, and revalidates liveness *inside* the write transaction |
| 1 | server auto-stamps `events_confirmed_at` from the turn timestamp | **Normalized preview + explicit Confirm** for windows and events; the ack timestamp is the confirm click. Tags/wants/busy apply immediately when requested. No "undo restores withdrawn plans" claims |
| 2 | "nothing partial… already-applied ops survive" (contradictory) | **All-or-nothing**: one parsed, fully validated batch → one final payload → **one atomic write**. Replace semantics for every `set_*`; duplicate field writes rejected |
| 3 | turn limit counted from `chat_messages` | **Independent rate rows** (`chat_turns`), atomically reserved before provider access; clearing history cannot reset the ceiling; retention pruning cannot erase the counter (rows live ≥ the rate window) |
| 4 | per-user in-flight lock only | **Optimistic state-version check** at write time (`canonical_hash` as the version); stale snapshot → typed conflict, nothing applied; account/grant liveness revalidated in the same txn; lock released in `finally`; history-clear serialized with the in-flight turn; single-process caveat stated (`run.sh` reliability model) |
| 5 | ICS: "junk → 422", 2 MB, silently ignore RRULE | **Reject-never-ignore**: RRULE/RDATE/TZID/PERIOD → 422 naming what to fix; UTC-Z + all-day DATE + floating-in-profile-tz only; DST gap/fold times rejected with an explanation; caps (500 events, 2 KB/line, 14-day horizon); sort+merge; split into ≤8 h windows; server-side revalidation at confirm; memory-only upload asserted by FS-untouched tests. *(Reviewer's cut recommendation retained as an explicit fallback: if time runs out, ship manual-only and drop the tab.)* |
| 6 | "08:00–22:00 free intervals", 6-window assumption | Free time is **split into ≤8 h windows**; `MAX_WINDOWS=20` enforced at preview **and** rechecked at confirm |
| 7 | "add an AI-chat paragraph to privacy.md" | **Named claim-by-claim replacement list** (§Docs) — exhaustive-collection preamble, "tags are the only free text", phone-derived busy load, WHAT-WE-NEVER-ACCEPT, phone-only calendar, "we do not use sub-processors" (the model provider IS one), transcripts (user AND assistant text) as stored text, provider retention disclosed as not-ours-to-promise |
| 8 | "activity negotiation stays real" | **Resolved by design (user decision, 25 Oct)**: each student's *own assistant* is their agent. X (A's assistant) and Y (B's assistant) settle offers through the existing `activity` core; when settled, each assistant tells its human "found a match — go swipe." Humans swipe and confirm plans; agents negotiate activities. See §B2/§C2 (delegation grant + negotiation worker)
| 9 | last-fenced-block-wins, chips after stream | Actions parse **only on clean completion** (no timeout/truncation/upstream error); **exactly one trailing JSON fence** required, multiple → no-op with honest note; server bounds accumulated response bytes and action count; failures render honestly |
| 10 | "OpenAI-compatible… swappable URL" | **OpenAI chat-completions protocol only.** Unexpected/empty shape → typed `ai_protocol` error (Anthropic-shaped fixture proves the refusal); switching `AI_API_URL` does not imply compatibility |
| 11 | implicit transport assumptions | JSON `Content-Type` **enforced** (415 otherwise) + same-origin `Origin` check on POST/DELETE + streamed **64 KB byte cap before JSON decode** + provider errors surfaced **redacted** (bounded, control chars stripped, never verbatim URLs/keys); Caddy/CF flush **verified at deploy**, `flush_interval -1` if needed |
| 12 | transcript retention 30 d | **7 days** (`AI_TTL_DAYS=7`, matching the demo's retention table) |

## Env contract (frozen surface)

```bash
# .env.example / socialmatch.conf
AI_API_URL=https://api.nano-gpt.com/v1/chat/completions   # full POST URL, OpenAI chat-completions protocol ONLY
AI_API_KEY=            # provider bearer key (NANOGPT_API_KEY)
AI_MODEL=z-ai/glm-5.3  # provisional until one live verified turn passes
AI_MAX_TOKENS=800
AI_TIMEOUT_SECONDS=60
AI_TTL_DAYS=7          # chat transcript retention (matches the demo retention table)
```

Unset `AI_API_URL` → chat disabled: banner on the page, `503 {"error":"ai_unconfigured"}` from the POST. The
key never reaches the browser; provider errors are redacted before rendering.

## Architecture

```
server/statewrite.py   NEW shared, validated state-write service (post_state refactored onto it)
                       mode="agent"  → today's grant-renew semantics (unchanged behavior)
                       mode="chat"   → GRANT-PRESERVING: no create/renew/restore/withdraw of any grant;
                                       revalidates account liveness + state version INSIDE the write txn
server/ai.py           OpenAI chat-completions client: SSE parser, typed AIError, protocol policing
server/chat.py         chat endpoints, action protocol, pending-preview store, per-user lock
server/negotiate.py     assistant-vs-assistant offer worker (beside sweep.loop()), gated on the delegation grant
server/schedules.py    manual windows + bounded .ics → preview → confirm
server/webui/templates/chat.html, schedule.html
schema.sql             chat_messages, chat_turns, chat_pending             (CREATE TABLE IF NOT EXISTS)
demo/scenario.py       +3 beats
```

### A. `server/ai.py` — provider client (OpenAI protocol only)

- `stream_reply(system, messages) -> AsyncIterator[str]` — POST `{model, messages, stream:true}` with the
  bearer key; **chat-completions SSE** parsing: `data:` lines, `choices[0].delta.content`,
  `data: [DONE]` terminator. Anything else — Anthropic-shaped `content_block_delta`, empty completion,
  non-SSE body — is a typed `AIError("ai_protocol", …)` **surfaced to the caller, not silently empty**.
- `Non-stream shape tolerated only as an explicit test case of failure` — no dual-protocol support.
- Errors: 401/403 → `ai_auth`; non-200 → `ai_http`; timeout → `ai_timeout`; all redacted before display.
- Server-side bounds: accumulated response bytes (256 KB), turn wall time (`AI_TIMEOUT_SECONDS`),
  `AI_MAX_TOKENS` sent as the request cap. **No live network in tests** (MockTransport); the single live
  turn is a deploy gate, not a test.

### B. Action protocol

System prompt: current profile (windows, tags raw+canonical, wants, busy, events), the bounded vocabularies,
the skill's hard rules (never ask for course names/calendar titles; never promise what the server won't
store), and: **end the turn with exactly one ```json block, last thing in the message**.

```json
{"actions": [
  {"op": "set_windows", "windows": [{"start": "...", "end": "...", "tz": "..."}]},
  {"op": "set_events", "events": [{"link": "https://…", "category": "music", "start": "...", "end": "..."}]},
  {"op": "set_interests", "tags": ["matcha", "bouldering"]},
  {"op": "set_wants", "wants": ["hangout"]},
  {"op": "set_busy_load", "value": 2},
  {"op": "none"}
]}
```

- **Executable only on clean completion**: no actions when the stream errored, timed out, was cancelled, or
  hit the cap mid-response. Exactly one trailing fence; zero or 2+ → no apply, honest note appended.
- **Batching**: parse the batch, validate everything (including `_ForbidExtra` on each op; unknown op or
  extra keys → **whole batch rejected**), build one final payload, one atomic `statewrite(mode="chat")`.
  `set_*` ops are **complete replacement** of that field; two ops touching the same field → rejected.
  omitted fields → preserved. All-or-nothing; there is no partial-apply semantics to explain.
- **Preview gates (windows, events)**: these two ops don't write. They produce a normalized **pending
  preview** (server-side: validated, ≤8 h windows, ≤20, horizon-checked; links `?`-stripped) stored as the
  user's single pending item (TTL 10 min, replaced by the next proposal). The assistant message renders the
  normalized list + the UI shows **Confirm / Cancel buttons**. Confirm → `statewrite` with
  `events_confirmed_at = now` for events (the *click* is the provenance, matching the skill's Step 3
  semantics); `free_time.student_confirmed_at` likewise for windows.
- **Batch composition — no partial application, ever** (round-2 finding #3): a batch is either
  **all-immediate** (`set_interests`/`set_wants`/`set_busy_load` only → applied atomically when the user asked
  in the turn) or **one proposal** (containing `set_windows` and/or `set_events` → a single pending preview
  card). Any immediate ops in a proposal batch **ride along and defer with it**: nothing writes until the
  Confirm, and one `statewrite` applies the whole batch together; Cancel, TTL expiry, a superseding proposal,
  or a `conflict` discards the entire batch including the riders. So tags in the same message as windows are
  applied when the windows are confirmed, never before. System prompt tells the model this. Tests: mixed
  windows+tags batch → nothing applied pre-confirm; confirm applies both atomically; cancel/expiry/conflict
  discard everything; tags-only batch applies immediately, as before.
- **B2 Negotiation ops (assistant-only contexts, never user-requested ops)**: a *negotiation turn* is
  system-provoked (the worker, §C2, or an incoming-offer prompt at chat load) and accepts EXACTLY ONE of:
  `{"op":"offer","candidate_id":…,"proposal_version":…,"tag":…,"url":…}` ·
  `{"op":"accept","offer_id":…}` · `{"op":"counter","offer_id":…,"tag":…,"url":…}` ·
  `{"op":"pass","offer_id":…,"reason":"too_expensive|too_far|not_my_vibe|wrong_time"}`.
  They run through the **existing `activity.propose`/`respond` core functions** — version-bound,
  `MAX_ROUNDS=3`, closed reason list, authorization unchanged (they already enforce: only your candidate's
  counterpart, only the latest offer). Anything else in a negotiation turn → no-op, logged honestly in the
  transcript. These ops NEVER apply to user-typed turns; state ops never apply in negotiation turns.
- **Grant firewall (P0 restated)**: no op touches grants, photo, or account; chat mode additionally cannot
  renew them as a side effect (that is what `statewrite` extraction is for). Tests assert every grants row
  byte-identical after each chat path, including a Settings withdrawal racing a turn.
- **Optimistic concurrency**: the system prompt embeds the current `canonical_hash` (state version); the
  apply/confirm transaction rechecks it and account liveness; mismatch → SSE `conflict` event, nothing
  written, honest message ("your settings changed while I was thinking — look and re-ask").
- After any write, SSE emits `state` (fresh summary); UI chips re-render.

### C. Chat endpoints

| | |
|---|---|
| `GET /chat` | page; disabled banner when unconfigured |
| `GET /api/chat/history` | last 200 messages oldest-first + state summary + any pending preview; `no-store` (existing middleware covers /api/*) |
| `POST /api/chat` `{"content": ≤4000 chars}` | SSE: `token`* → (`actions` \| `preview`) → (`state`) → `done` \| `error` \| `conflict` |
| `POST /api/chat/confirm` `{"id": …}` (or `{"cancel":true}`) | applies or drops the single pending preview |
| `DELETE /api/chat` | clear own transcript (serialized with any in-flight turn) |

- Hardening: JSON `Content-Type` enforced (415), **same-origin `Origin`** check on POST/DELETE (deployed
  origin policy; `SameSite=Lax` is already the cookie), streamed request byte cap 64 KB **before** JSON
  decode + the 4,000-char limit after.
- **Per-user in-flight lock** → 409; released in `finally`; single-process assumption stated (matches
  `deploy/run.sh`, which runs exactly one uvicorn).
- **Rate limit: 20 turns / 10 min** from the dedicated `chat_turns` table — **reserved atomically before the
  provider call** (so failures don't subsidise retries), counted per user turn attempt, invisible to
  transcript deletion and retention pruning (rows kept ≥ rate-window regardless of `AI_TTL_DAYS`). → 429.
- **Retention**: sweep purges `chat_messages` older than `AI_TTL_DAYS=7`; **user and assistant text are both
  stored**; account deletion cascades the transcript (existing txn). "Clear history" is a user right, not a
  rate-limit reset.
- **C2 Negotiation worker (`server/negotiate.py`)**: a **singleton** asyncio loop beside `sweep.loop()`
  (enforced with a module-level guard: a second instance fails loudly at startup), tick ~15 s —
  scans `offers.latest_offer(...) == 'offered'`; for each pending offer, if the **addressee's `delegation`
  grant is live** it provokes ONE autonomous negotiation turn (same chat turn engine: system prompt = the
  incoming offer + my profile + remaining rounds + reason glossary; ONE op parsed; one provider call).
  Concurrency: the turn takes the **same per-user in-flight lock as typed turns** — never two turns for one
  person at once, human or autonomous. Provider-failure bound: attempts are persisted per offer per version
  (reusing `chat_turns` rows with a marker); after **2 failed attempts** on the same offer the worker stops
  for that pair and your assistant surfaces the offer as a **manual Accept/Counter/Pass card** — no infinite
  retry cycles. Per-user safety: counted in the same `chat_turns` budget as typed turns (autonomy is not
  free credits), plus a tighter ceiling — no autonomous turn once a user hits 12 turns in the last 10 min
  regardless of the 20 shared budget. Delegation OFF → the worker skips you silently; your assistant shows
  the incoming offer at your next chat load with **Accept / Counter / Pass-with-reason buttons** (manual
  path needs no grant — you clicking is consent, exactly like windows/events previews).
- **C2a Atomicity of the autonomous apply (round-4 P1)**: the delegation check is NOT a pre-flight —
  it happens **inside the same write transaction as the offer op**. Re-read the grant's live row
  (`withdrawn_at IS NULL AND expires_at > now`) plus the account's `deleted_at` at commit; if delegation was
  withdrawn while the provider streamed, or the candidate/proposal version went stale, the op is dropped and
  the transcript records "your delegation was off when this would have applied — nothing happened." A test
  withdraws delegation mid-stream and asserts zero state change.
- **C2b Initial offers (round-4 P1)**: a worker that only reacts to existing offers can never start. The
  bounded trigger: on chat page load / any turn, if you have an **open matched candidate with no offer yet**
  (dashboard read, no write), the assistant may ask — auto-answering this stays on the same delegation grant,
  same lock, same one-op rule, same atomic revalidation — and proposes the initial offer for the counterpart
  assistant to answer. Everything else about the worker is unchanged.
- **C2c Transcript partition (round-4 P1)**: notes to the OTHER user are written by **server-constructed
  allow-listed fields only** — `{'decided': 'counter', 'tag': 'coffee', 'url': ..., 'reason': ...}` rendered
  into a fixed template. Model prose never crosses transcripts; neither do your profile fields (windows,
  wants, busy, raw interests) — the counterpart sees only what `activity.propose/respond` already made
  shareable. Leakage tests: provider emits prose containing another user's tag and an injection string —
  assert the counterpart transcript contains ONLY the structured note.
- **C3 Delegation consent (the fifth grant)**: `grants` gains `kind='delegation'` — **off by default**,
  Settings toggle only ("Let my assistant accept, counter or pass offers for me"), `consent_log` rows on
  every change, **never writable by the chat** (no op exists; `statewrite(mode="chat")` firewalls it like
  matching/publication). Deployed DBs get it via an idempotent table-rebuild migration (SQLite CHECK
  constraints can't be ALTERed): detect the old `CHECK kind IN (…)` in `sqlite_master`, then in one
  `BEGIN IMMEDIATE` create `grants_new` with the extended CHECK → copy → drop → rename; tested fresh,
  existing, repeated, and with two racing initializers.
- SSE transport: `text/event-stream; charset=utf-8`, `X-Accel-Buffering: no`, `no-store`; **deploy step
  verifies Caddy/CF actually flush** (add `flush_interval -1` to the Caddyfile if buffered) — a buffered
  proxy delays tokens but never corrupts a turn.

### D. Schedule page (`server/schedules.py` + `schedule.html`)

- **Manual define**: date + start/end (profile tz) → same window validation (≤8 h, ≤20, horizon). This is
  the guaranteed-shipped path.
- **`.ics` upload (hardened, with a named fallback)**: ≤2 MB streamed in memory (never spooled — a test
  asserts zero FS writes including on error paths); text/calendar or `.ics`; **reject-never-ignore**:
  RRULE/RDATE/TZID/PERIOD on an event → 422 listing what the export must remove (a recurring class is busy
  time we refuse to pretend we understood); floating times → interpreted in the profile tz, all-day DATE
  ends exclusive; DST gap/fold times rejected with an explanation. Caps: 500 events, 2 KB/line unfolded,
  14-day horizon.
  Free-window computation: busy intervals sorted+merged → subtract from 08:00–22:00 local per day →
  **split into ≤8 h windows** (the existing validation cap) → chunks ≥30 min → **preview with checkboxes** →
  confirm stores only ticked windows through `statewrite` (server revalidates count ≤20 and every bound).
  Only intervals ever enter the DB (SUMMARY/LOCATION/DESCRIPTION/ATTENDEES never parsed-forward; DB-dump
  test asserts no calendar strings anywhere).
- **Fallback (reviewer-recommended)**: if the timeline slips, ship manual-only and remove the upload tab for
  the demo — no half-working parser.

### E. UI

`chat.html`: thread + streaming (fetch ReadableStream parsing SSE, vanilla JS), **all model/action/error text
rendered via text nodes (+`\n`→`<br>), never innerHTML**; chip rail (windows, interests, wants, busy) +
matches badge; pending-preview card with Confirm/Cancel buttons; honest error lines (redacted provider
errors); disabled banner when unconfigured. `schedule.html` as §D. Nav in `base.html`. No frameworks. Same
design tokens/fonts as the redesign. `RED-ACTS`: provider error text is truncated to 300 chars, control
characters stripped, URLs shown as plain text — never rendered as markup.

### F. Docs — the claim-by-claim replacement list (load-bearing)

| privacy.md claim today | v2 replacement |
|---|---|
| "Only these things, and only because…" (exhaustive collection) | add chat transcripts (user + assistant text) and the chat/schedule surfaces to the list |
| "Interests … are the only free-text field" | "the only free-text field in *structured state*"; chat text is a new, separate free-text surface with its own retention (7 days) and provider flow |
| "How busy you are … computed on your phone" | busy is computed on the phone if you use the phone-agent skill, or given by you / the assistant in chat |
| "WHAT WE NEVER ACCEPT" (no free text, second exception list) | keep for *structured state*; state plainly that chat accepts free text by design (bounded per turn), and that the .ics upload is parsed server-side with only intervals kept |
| "your the agent reads your calendar on your phone … deletes the rest" | true of the legacy skill path only; the chat path is: you type, or you upload an .ics that is read once in memory and reduced to intervals before anything is stored |
| "We do not use sub-processors" | **false now**: chat turns (system prompt + last 40 messages + your text) are sent to the model provider named in configuration; we do not control provider retention; don't type secrets |
| "your swipes, the activity offers…" deletion list | + chat transcript and pending previews are deleted with the account |
| (new grant) | `delegation` joins matching/publication as a consent you can withdraw: "your assistant may accept, counter or pass offers for you"; transcripts include autonomous negotiation turns; FLOW §2 documents the grant, its default-off posture, and that only Settings can change it (chat never can) |
| inbox wording | unchanged (assistant text goes only to the chat transcript, never the inbox) |

terms.md: you're responsible for what you type; AI output is neither checked nor guaranteed; chat can be
wrong about your own state — the chips and Settings page are the truth.
README/FLOW: **negotiation is assistant-to-assistant, by design** — each student's own assistant settles
what-to-do against the other side's assistant through the existing activity core, then tells its human "found
a match — go swipe." Autonomy is gated on the `delegation` grant (off by default); swiping and plan
confirmation are human-only, always. The phone agent skill is documented as the legacy/alternate client.
`.env.example` gains the AI block.

### G. Tests

| File | Covers |
|---|---|
| `tests/test_statewrite.py` | extraction safety: `post_state` behavior **unchanged** for the agent path (existing suite is the regression proof); chat mode: every grants row byte-identical after every chat path; renewal-side effects absent; Settings-withdrawal racing a turn |
| `tests/test_ai_client.py` | OpenAI SSE happy path; `[DONE]`; empty completion; **Anthropic-shaped stream → typed `ai_protocol` failure**; 401; timeout; mid-stream error; byte-cap enforcement — all MockTransport, zero network |
| `tests/test_chat.py` | action parse (0/1/2+ fences, unknown op, extra keys, duplicate-field ops → whole batch rejected); clean-completion gating (timeout/truncated → nothing applied); replace semantics (matcha→coffee, `?`-strip, ≤8 h/≤20/horizon); preview lifecycle (propose→confirm→events_confirmed_at/provenance stamp; cancel; TTL expiry; second proposal replaces the first); optimistic-conflict path; per-user lock 409 + `finally` release; rate limit from `chat_turns` incl. **POST→DELETE→POST does not reset**, pruning cannot erase; transcript retention; deletion cascade; unconfigured 503; Origin/Content-Type/byte-cap rejections |
| `tests/test_schedules.py` | manual windows (validation paths); ICS fixtures: UTC, floating→profile-tz, all-day DATE (end-exclusive), overlap/merge, **RRULE→422, TZID→422, DST-gap→422**, junk→422, >2 MB→413, 500-event cap; ≤8 h splitting; confirm revalidation (21 ticked → rejected); **no-FS-write assertion across the upload paths incl. errors**; DB-dump: no calendar strings stored |
| `demo/scenario.py` | 3 beats: manual windows; ICS→preview→confirm; chat immediate-apply of tags via the provider monkeypatched → 17 beats |
| `tests/test_negotiate.py` | worker fires only with delegation live; autonomous turn happy path (provider monkeypatched to emit one op each: offer→counter→accept); version-stale offer → core rejects, nothing changes; `MAX_ROUNDS` exhaustion → pass falls back to time-only + announce; no-op output → logged, nothing changes; delegation-off → manual Accept/Counter/Pass buttons at chat load; autonomous turns billed into `chat_turns` incl. the 12/10min ceiling; every step appends to BOTH transcripts; consent_log rows for delegation on/off; **grants table migration** (fresh DB, pre-existing DB, repeated init, two racing initializers — the CHECK cannot be ALTERed, so the rebuild is exercised hard) |
| `tests/test_negotiate.py` (round-4 additions) | delegation withdrawn **mid-stream** → apply txn revalidates, nothing changes, transcript says so; **2-failed-attempts cap** → worker stops trying that offer and posts a manual card (no infinite retry; clock via `_FIXED_NOW`); **transcript partition/leakage** — counterpart transcript contains only the structured note across a full offer→counter→accept dance, even when provider prose mentions the other user's tags or injects markup; **initial-offer path** — matched candidate with no offer, delegation live → offer proposed through the same locks; delegation off → assistant only asks |

### H. Order of work

1. **Phase 0 (integrator, frozen surfaces)**: extract `statewrite` with modes; refactor `post_state` onto it;
   full existing suite stays green (this is the P0 fix — it lands before any chat code). Env readers,
   `.env.example`, the `chat_*` tables, the idempotent `grants`-CHECK rebuild (delegation kind), and
   CONTRACT-chat.md ownership table. `server/negotiate.py` skeleton mounts its loop beside `sweep.loop()`.
2. Parallel lanes: (a) `ai.py` + `chat.py` + tests; (b) `schedules.py` + `schedule.html` + tests;
   (c) `chat.html` + nav; (d) docs lane with the table above.
3. Integrate: full suite + 17 beats.
4. Gate review (expensive) → fixes → re-approve.
5. Deploy gate: rsync; `AI_*` in the box `socialmatch.conf` (**requires the valid key**); **one live
   streamed turn + one action round-trip verified through Caddy/CF before the demo is called done**; Caddy
   flush check; push both repos.

### I. Cut (per review + scope)

Dual-protocol parsing (OpenAI only), chat-written `busy_load`
inference, RRULE expansion (rejected, not implemented), client-side ICS parsing, markdown rendering,
multi-thread history, RAG, per-action confirm for tags/wants/busy (immediate apply), CSRF tokens (Origin +
SameSite=Lax + JSON enforcement instead), dark mode. **ICS upload itself carries a named fallback to
manual-only if the timeline slips.**

### J. Risks handled in-plan

Invalid key → deploy blocked on a live verified turn. Provider shape drift → typed `ai_protocol` failure, never
a silent empty reply. Proxy buffering → deploy-time verification + Caddyfile tweak. Lost updates between
writers → optimistic version + in-txn revalidation. Model prose smuggling actions → single-fence rule +
Autonomous turns burning provider spend or acting while the human is away → delegation default-off + shared `chat_turns` budget with a 12/10min autonomy ceiling + `MAX_ROUNDS` + one op per turn, every step
visible in the transcript.
clean-completion gating + all-or-nothing batching. Prompt injection fishing for grants/photo → the closed op
enum *plus* the grant-preserving write mode (the P0 fix is the structural answer; the enum alone was not).

## K. Open questions

1. **F6/K1 — negotiation posture: ANSWERED (user, 25 Oct)**. Our assistants are the agents: X negotiates on
   A's behalf, Y on B's, through the existing activity core; humans only swipe and confirm. Delegation to
   the assistant is the fifth grant, default OFF. Remaining decision for you: is **default-off** the right
   demo posture (safer, one extra click per user at demo time), or should demo accounts ship with it ON so
   the negotiation segment runs hands-free?
2. **ICS**: keep the hardened upload (v2 D), or take the reviewer's recommendation to ship manual-only for
   the demo? I've kept it in-plan with the explicit fallback.
3. **The key**: deployment needs a working `AI_API_KEY` in `socialmatch.conf` — today's `NANOGPT_API_KEY`
   auth-fails. Who puts the fresh one in the box file (I can't safely take it via chat)?
4. **Model default** `z-ai/glm-5.3` stays provisional until the first live turn confirms the model name is
   valid on nano-gpt.
