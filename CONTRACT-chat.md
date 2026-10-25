# Build contract — chat app (PLAN-chat-frontend.md, approved)

Interface authority for every lane. Where this file and the PLAN disagree, the PLAN wins on
behaviour, this file wins on names and signatures. Same discipline as CONTRACT-photos.md.

Tools: `uv run …` only. NEVER `uv sync|add|lock` (deps frozen: fastapi, uvicorn, jinja2,
itsdangerous, httpx, pillow). Suites must stay green at every handoff.

## Ownership

| Files | Lane |
|---|---|
| `server/state.py` (thin to delegate), `server/statewrite.py` (may patch), `tests/test_statewrite.py` | **S** |
| `schema.sql`, `server/db.py` (grants migration), `server/config.py`, `.env.example`, `tests/test_migrations.py` | **I** |
| `docs/privacy.md`, `docs/terms.md`, `README.md`, `FLOW.md` | **D** |
| `server/ai.py`, `tests/test_ai_client.py` | **A** |
| `server/chat.py`, `tests/test_chat.py` | **B** |
| `server/schedules.py`, `server/webui/templates/schedule.html`, `tests/test_schedules.py` | **C** |
| `server/webui/router.py` (settings grants delegation + /chat/ /schedule/ pages + registration seeding), `server/webui/templates/chat.html`, `settings.html`, `base.html` (nav), `tests/test_chat_ui.py` | **F** |
| `server/negotiate.py`, `tests/test_negotiate.py` | **N** (wave 2 — after B lands) |
| Frozen (integrator): `server/app.py`, `server/statewrite.py`'s `write_state` signature, `pyproject.toml`, `demo/scenario.py` (integrator adds beats at the end) | — |

Never edit another lane's files. If a seam feels wrong, report it — do not patch across ownership.

## 1. Config (lane I adds; everyone reads through `config`)

```python
ai_api_url() -> str | None       # unset → chat DISABLED, not an error
ai_api_key() -> str | None
ai_model() -> str
ai_max_tokens() -> int           # default 800
ai_timeout_seconds() -> float    # default 60
chat_ttl_days() -> int           # default 7 (AI_TTL_DAYS)
delegation_default_on() -> bool  # env DELEGATION_DEFAULT_ON=1 — demo accounts seed the grant
```

`.env.example` gains the whole AI block from PLAN §Env + `DELEGATION_DEFAULT_ON=1` (commented as
demo-box only, default absent = off).

## 2. `server/ai.py` (lane A) — OpenAI chat-completions protocol ONLY

```python
class AIError(Exception):
    code: str      # ai_unconfigured | ai_auth | ai_http | ai_timeout | ai_protocol
    detail: str    # safe-to-display, already redacted

async def stream_reply(system: str, messages: list[dict]) -> AsyncIterator[str]
    # POST {ai_model, [{"role":"system",...}, *messages], stream:True, max_tokens}
    # to config.ai_api_url() with Bearer config.ai_api_key() (httpx.AsyncClient).
    # Parses ONLY OpenAI SSE: `data: {…}` lines, choices[0].delta.content, `data: [DONE]`.
    # Any other shape (Anthropic content_block_delta, empty completion, non-SSE body)
    # → raise AIError("ai_protocol", …). 401/403 → ai_auth; non-200 → ai_http;
    # timeout → ai_timeout. Cap the accumulated response at 256 KiB; stop reading past it
    # and finish what was already accumulated (treat cap-hit as a CLEAN completion for
    # display, but the caller still decides executability).

def redact(text: str) -> str
    # display-safe: strip control chars, cap 300 chars. Provider errors and any AIError
    # detail pass through here before reaching a user.
```

Zero network in tests: httpx.MockTransport handler injected via a module-level
`transport=None` kwarg on stream_reply (or monkeypatch the client factory — A's choice; pin it
in the test file header so B/N monkeypatch the same seam the same way).

## 3. `server/chat.py` (lane B) — pages, SSE, protocol, pending store

Router is module-level `router` (mounted at `/api` prefix like state's; page routes live in
webui per existing style — B exposes an `include_page`-free API only; F renders).

```python
class Turn:              # one engine call, shared by user turns and negotiation turns
    kind: str            # "user" | "negotiation"
    user_text: str | None
    system_extra: str     # negotiation context block ("" for user turns)
TurnResult = dict        # {tokens: str, action_parsed: bool, events: list[...]} — pin exact keys in code

async def run_turn(conn, uid: int, turn: Turn) -> AsyncIterator[dict]
    # The single LLM entrypoint EVERYTHING uses (lane N imports this — stability critical).
    # Persists the user message (kind="user"), streams tokens (event {"type":"token"}),
    # parses/executes the trailing JSON action block per PLAN §B (state ops) or §B2
    # (negotiation ops, kind="negotiation": exactly one offer/accept/counter/pass op through
    # the EXISTING activity.propose/respond cores), then emits
    # {"type":"actions"|"preview"|"note"} → {"type":"state"} → {"type":"done"}.
    # Errors: {"type":"error","code":AIError.code,"detail":redacted} + honest system note.

def build_current_payload(conn, uid: int) -> StatePayload
def current_state_hash(conn, uid: int) -> str      # statewrite.canonical_hash(build_current_payload(...))
def reserve_turn(conn, uid: int, kind: str) -> None   # INSERT chat_turns; raises ChatRateLimited
def build_system_prompt(conn, uid: int, turn: Turn) -> str
def state_summary(conn, uid: int) -> dict             # chips payload for the UI
```

REST (all session auth; JSON content-type enforced with 415; same-origin `Origin` header
check on POST/DELETE vs configured BASE_URL origin; 64 KiB streamed cap before JSON decode):

- `GET /api/chat/history` → `{"messages":[{role,content,created_at}…≤200], "state": state_summary, "pending": pending|None}`
- `POST /api/chat` `{"content": ≤4000}` → `text/event-stream` (X-Accel-Buffering: no) —
  reserve_turn first (429 `rate_limited`), per-user in-flight lock (409 `turn_busy`),
  then run_turn streamed as SSE events; per-user lock released in `finally`.
- `POST /api/chat/confirm` `{"id":…}` | `{"id":…,"cancel":true}` → apply or drop `chat_pending`
- `DELETE /api/chat` → clear own transcript (serialized with the in-flight lock)
- Chat disabled (no ai_api_url) → POST 503 `{"error":"ai_unconfigured"}`; page shows the banner (F).

Apply flow for state ops (PLAN §B): inside ONE `db.immediate`, in order: re-read
`users.deleted_at`; `current_state_hash() == turn's captured hash` else `conflict` event,
nothing applied; then batch-validate ALL ops (unknown op / extra keys / duplicate field /
mixed-ridealong rules per PLAN §B); `statewrite.write_state(conn, uid, payload, mode="chat")`
exactly once. Windows/events ops → normalize + `chat_pending` row + `preview` event (TTL 10 min,
single pending per user, superseded by the next proposal). Tags/wants/busy in an all-immediate
batch apply now; in a proposal batch they ride along into the pending item (applied at confirm).

`chat_pending` row (lane I creates the table; B owns the usage): `id, user_id, created_at,
expires_at, kind ('windows'|'events'|'ics'), payload_json (the normalized, ALREADY-VALIDATED
items + rider ops), state_hash (for the optimistic confirm check)`. Confirm re-validates
count ≤ MAX_WINDOWS and every window/event bound, re-checks hash → apply (windows get
`free_time.student_confirmed_at=now`, events get `events_confirmed_at=now` — the click is the
provenance), then delete the pending row. Cancel/TTL-expiry/supersede → delete, nothing applied.

Persistence (lane I creates `chat_messages(id, user_id, role 'user'|'assistant'|'note',
content, created_at)`; B owns usage): user message saved BEFORE the provider call; final
assistant text on clean completion; system `note` rows for applied actions / refusals /
conflicts / negotiation steps (server-templated FACTS only — never provider prose).
Account-deletion cascade + sweep purge older than `chat_ttl_days()` — B adds the sweep step
function `purge_chat(conn, now) -> int` for I to mount? No: B owns the purge function and
edits `server/sweep.py`? sweep.py is NOT in B's list — B exposes `purge_chat(conn, now)`;
the INTEGRATOR mounts it. Report the seam.

State-hash capture point: `run_turn` captures `current_state_hash()` when building the system
prompt; every apply/confirm path re-checks against a freshly computed hash.

## 4. `server/schedules.py` (lane C) — the demo affordance

- `POST /api/schedule/windows` `{"windows":[{start,end,tz}≤20]}` → merge onto current payload
  + `statewrite(mode="chat")` + optimistic hash check (same as B's apply; import B's helpers,
  do not fork them). Manual path — no pending step, full replace of the windows field per
  PLAN §B semantics (the POST body IS the complete new window list).
- `POST /api/schedule/ics` (raw body, ≤2 MiB streamed in memory, never spooled to disk) →
  parse VEVENTs: reject-never-ignore (RRULE/RDATE/TZID/PERIOD → 422 listing them), floating
  times = profile tz, all-day DATE end-exclusive, DST gap/fold → 422; caps 500 events,
  2 KiB/line, 14-day horizon; busy sort+merge → free from 08:00–22:00 local per day →
  chunks ≥30 min → SPLIT into ≤8h windows (MAX_WINDOW_HOURS) → `chat_pending` row kind='ics'
  + `preview` payload of normalized candidate windows. Response: the preview list (id'd).
- `POST /api/schedule/ics/confirm` `{"id":…, "pick":[indices]}` → filter, re-validate ≤20 and
  all bounds server-side, hash check, apply via statewrite, delete pending.
- Page `schedule.html`: manual form + upload + preview checkboxes (vanilla JS, existing
  style). C owns the template AND its script glue; F owns only the nav link.

`config.upload_dir()` is NOT used here — nothing about ICS ever touches disk. Test assets:
in-memory fixtures; a no-FS-write assertion across upload paths including error paths.

## 5. Settings + registration (lane F)

- `GrantsBody` in webui/router.py gains `delegation: bool | None = None`; the grants endpoint
  extends the existing pattern: `true` → `_upsert_grant(conn, uid, "delegation")` (no tier),
  `false` → withdraw + `consent_log` via the existing helpers. The grants constraint allows
  this (lane I's migration); the grants_tier HTTP shape gains `"delegation"` in its echo.
- Registration: when `config.delegation_default_on()`, seed the delegation grant (version 1)
  + consent_log row in the existing registration transaction — hands-free demo accounts.
- Settings page: the toggle ("Let my assistant accept, counter or pass offers for me") wired
  to the grants endpoint, styled like the existing switches.
- `chat.html`: thread + streaming via fetch ReadableStream parsing SSE (vanilla JS — no
  frameworks), ALL text via text nodes (escape → `createTextNode`, `\n`→`<br>`), chip rail
  (windows count, interests, wants, busy), pending-preview card with Confirm/Cancel, honest
  error lines (`redact` happens server-side; client also escapes), rate-limited/turn-busy
  states, unconfigured banner when `state.ai` false. Nav in `base.html`: Chat (primary),
  Schedule, Dashboard, Matches, Settings.
- `/chat` + `/schedule` page routes in `webui/router.py` (F), rendering F's and C's templates.

## 6. `server/negotiate.py` (lane N, wave 2) — assistant-vs-assistant offers

Singleton loop (module-level `_STARTED` flag; a second `start_loop()` raises), mounted by the
integrator in `app.py` lifespan beside `sweep.loop()`; tick ~15 s:

- Scan `offers.latest_offer(...) == 'offered'` + open matched candidates with NO offer and
  exactly one round so far used? (initial offers: see PLAN §C2b — proposal trigger fires from
  B's turn engine on chat load; the FIRST offer of a pair comes from the assistant whose user
  has the candidate open. N only drives RESPONSES to existing offers + the takeover of an
  initial proposal. Do NOT invent an initial-offer scanner.)
- For each pending offer where the ADDRESSEE has live delegation:
  - attempts: count `chat_turns` rows `kind='negotiation'` AND `ref = f"offer:{offer_id}"`;
    ≥2 → skip and (once) post the manual-path note.
  - `chat.run_turn(kind="negotiation", system_extra=<offer context>)` with the SHARED
    per-user in-flight lock and `reserve_turn` (kind='negotiation', ref=...) — 429/409 mean try
    next tick, not failure.
  - Parse result per §B2: exactly ONE op. Apply inside ONE `db.immediate` that re-reads
    delegation (`withdrawn_at IS NULL AND expires_at > now`) + `users.deleted_at` + the
    offer's current version — withdrawn/stale → drop with an honest `note`, no state change.
  - Transcripts: the acting user's rows per B's persistence; the OTHER user gets ONE
    server-templated `note` row built ONLY from `{'decided', 'tag', 'url', 'reason'}` —
    never provider prose, never the actor's profile fields.
- Everything provider-facing goes through `ai.stream_reply` — N never builds its own HTTP.

## 7. Test-map

Exactly PLAN §G. Cross-cutting invariants every lane asserts in its own file:

- grants tables BYTE-IDENTICAL after every chat-mode write (dump rows before/after).
- no preset-avatars/publication/matching/delegation row is created, renewed, restored or
  withdrawn by any chat-path code.
- account deletion cascades: chat_messages + chat_pending + chat_turns cleared.
- `db._FIXED_NOW` for all time-sensitive tests; zero sleeps; zero network.

## 8. Integration gate (integrator, after lanes)

`uv run pytest` green (target: 273 + ~40 new), `uv run python demo/scenario.py` all beats,
then the expensive reviewer, fixes, final gate, deploy (rsync; `AI_*` + `DELEGATION_DEFAULT_ON=1`
into the box `socialmatch.conf` by the user), one live verified turn + action round-trip
through Cloudflare/Caddy, push both repos.
