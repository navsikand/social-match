# Build contract — authoritative

This file is the single source of truth for every module boundary, endpoint shape, and test in this repo. If code
and this file disagree, this file wins; if you think this file is wrong, STOP and report in your final message
instead of deviating. `FLOW.md` and `PLAN.md` explain *why*; this file says *what to build*.

## 0. Ownership (do not touch files you don't own)

Shared, FROZEN (owner: integrator): `pyproject.toml`, `.python-version`, `.env.example`, `CONTRACT.md`, `schema.sql`,
`server/__init__.py`, `server/config.py`, `server/db.py`, `server/passwords.py`, `server/sessions.py`,
`server/wording.py`, `server/app.py`, `server/sweep.py`, `tests/conftest.py`, `tests/util.py`, `tests/test_smoke.py`.

Owned by agents (click into file for further rules):

| Files | Agent phase |
| --- | --- |
| `server/auth.py`, `server/pairing.py`, `tests/test_pairing.py` | P1a |
| `server/schema.py`, `server/state.py`, `tests/test_payload.py` | P1b |
| `skill/**` (all of it) | P1c |
| `server/webui/__init__.py`, `server/webui/router.py`, `server/webui/templates/**`, `server/webui/static/**` | P1d |
| `server/matching.py`, `tests/test_matching.py` | P2a |
| `server/scheduling.py`, `tests/test_concurrency.py` | P2b |
| `server/inbox.py`, `tests/test_inbox.py` | P2c |
| `demo/scenario.py` | P3a |
| `README.md`, `docs/privacy.md`, `docs/terms.md` | P3b |

Never modify another agent's file. Never add dependencies — Python stdlib plus what `pyproject.toml` already lists.

## 1. Commands

```bash
cd /home/nav/active-code/devhacks3/track2-study-match
uv sync                                   # create/refresh .venv (integrator does this; do NOT re-lock)
uv run pytest tests/test_<yours>.py -q    # YOUR tests only are your responsibility
uv run uvicorn server.app:app --reload    # run the server (integration, integrator)
```

Use `uv run pytest`, never `pytest`. Do not run `uv sync`/`uv add`/`uv lock` — the venv is prepared.

## 2. Conventions

- **Time**: all DB timestamps are UTC strings `YYYY-MM-DDTHH:MM:SSZ` via `db.utcnow(days=0)`. Lexicographic compare
  is chronological compare. Windows are stored as UTC start/end plus the original `tz` string for display.
- **JSON in / out**: all bodies and responses JSON. Errors: `{"error": "<code>", "detail": "<human text>"}`.
- **Conns**: `db.connect()` per request (FastAPI dep `db.conn_dep` yields a fresh connection, closes it). All
  mutations that must be serialized use `with db.immediate(conn):` (BEGIN IMMEDIATE … COMMIT). Reads outside
  transactions are fine.
- **Config**: read via `server/config.py` functions at call time (never cache at import; tests swap env vars).
- **IDs**: sqlite INTEGER rowids. `agent_id` = `"ag_" + token_urlsafe(6)`. Cards/links use candidate_id.
- **Auth header**: `Authorization: Bearer <token>`; token is 32 chars `secrets.token_urlsafe(24)`, stored only as
  `sha256(token)` hex in `pairings.token_hash`.
- **Error codes**: `401 {"error":"token_invalid"|"session_invalid"|"token_revoked"}`, `403`, `404`, `409
  {"error":"stale_version"|"slot_conflict"|"intent_present"}`, `410 {"error":"code_expired"|"code_used"}`,
  `422 {"error":"payload_forbidden", "detail": ...}`.

## 3. Data model

`schema.sql` is authoritative. Notables:

- `users` — site users; `password_hash`, `preset_avatar_id` (1–8), `adult_attested_at`, `deleted_at`.
- `pairing_codes(code_hash PK, user_id NULL until approve, kind, expires_at, used_at)` — single use.
- `pairings(id, user_id, agent_id UNIQUE, token_hash UNIQUE, prev_token_hash, prev_rotate_expires, label,
  created_at, last_seen_at, revoked_at)`.
- `grants(id, user_id, kind IN('matching','publication','reveal'), tier IN('card','card+name') [publication only],
  version, fields_json, audience='matched_users_only', granted_at, expires_at, withdrawn_at)` — one live row per
  (user, kind); a new version supersedes the old (write old `withdrawn_at`, insert new row, version+1).
- `consent_log(id, user_id_hash, action, scope, detail_json, created_at, purge_after NULL)`.
- `availability(user_id PK, written_by_agent_id, payload_hash, observed_at, expires_at, source IN
  ('student','cubbie_computed'))` + `availability_windows(user_id, starts_at, ends_at, tz)` (cascade on user_id).
- `hobby_tags(user_id, tag)` closed vocab (§5).
- `candidates(id, user_a, user_b, score, reasons_json, created_at, expires_at, state IN
  ('open','declined','voided','hidden','matched','closed'))`; one open row per unordered user pair.
- `proposals(id, candidate_id, window_start, window_end, version, state IN
  ('proposed','a_confirmed','both_confirmed','closed','voided'), expires_at, superseded_by)`.
- `proposal_confirmations(proposal_id, user_id PK pair, confirmed_at)` — UNIQUE = idempotent confirms.
- `revoke_intents(user_id, created_at)` — written AUTOCOMMIT before any lock (§8).
- `swipes(user_id, candidate_id, direction IN('right','left','block','report'), created_at, UNIQUE(user_id,candidate_id))`.
- `blocks(user_hash, blocked_user_hash, created_at, expires_at PK pair)` — both directions inserted; 12-month TTL;
  hash = sha256(f"{BLOCK_SALT}:{user_id}").
- `reports(reporter_id, candidate_id, created_at, state)`.
- `inbox_items(seq AUTOINCREMENT PK, user_id, kind, payload_json, created_at)` — user-relative queue; `GET /api/inbox`
  cursor `since` = max seq already seen.

## 4. Pairing (P1a)

- `POST /api/pair/start` body `{}` or `{"label": "phone"}` → `201 {"code": "AB12-CD34", "url":
  f"{BASE_URL}/m/pair/{code}", "expires_in": CODE_TTL}`. Code = 8 chars of Crockford alphabet
  `0123456789ABCDEFGHJKMNPQRSTVWXYZ` (no I L O U), formatted `XXXX-XXXX`; store sha256 of the dash-stripped
  uppercase string. Insert `pairing_codes(code_hash, user_id=NULL, kind='link', expires_at=now+TTL, used_at=NULL)`.
- `POST /api/pair/approve` (session cookie) body `{"code"}` → in `immediate` txn: find row, not used, not expired →
  `UPDATE pairing_codes SET user_id=?`; `{"ok": true}`; else 404/410 as above.
- `POST /api/pair/finish` body `{"code"}` → in ONE `immediate` txn:
  `UPDATE pairing_codes SET used_at=? WHERE code_hash=? AND used_at IS NULL AND expires_at > ?` … `RETURNING
  user_id` (must include `AND user_id IS NOT NULL`); zero rows → 404/410. Mint token+agent_id, insert pairings,
  return `{"token","agent_id"}`. NEVER read-then-write.
- `POST /api/pair/rotate` (bearer) → new token; keep `prev_token_hash` + `prev_rotate_expires = now+60s`;
  `{"token","agent_id","grace_expires_at"}`. Bearer lookup must accept prev hash inside grace.
- `POST /api/pair/forget` (bearer) → in one `immediate` txn: set `revoked_at` on this pairing; DELETE availability
  rows with `written_by_agent_id = this agent` (and their windows); do NOT touch other pairings or grants.
- `GET /api/pairings` (session) → `{"pairings": [{agent_id,label,created_at,last_seen_at,revoked_at}]}`.
- `DELETE /api/pairings/{agent_id}` (session) → same effect as forget, for that pairing.
- `server/auth.py` exposes `get_identity` (FastAPI dependency): parses bearer, returns `Identity(user_id, agent_id)`:
  unknown token → 401 `token_invalid`; revoked → 401 `token_revoked`. Also `conn_dep` style imports from db.
- `GET /m/pair/{code}` is a webui PAGE (P1d) that shows the code + an Approve button POSTing `/api/pair/approve`.

## 5. State payload (P1b)

`POST /api/state` (bearer). Pydantic v2, `extra="forbid"` on every model (recursive). Available via `server/schema.py`
as `StatePayload`. Exact shape:

```jsonc
{
  "windows": [ {"start": "2026-10-29T19:00", "end": "2026-10-29T21:00", "tz": "America/Los_Angeles"} ],  // 0..20
  "tags": ["climbing", "live_music"],      // 0..5, from TAG_VOCAB exactly, closed
  "wants": ["hangout", "event"],           // subset of hangout|event
  "free_time": {"source": "student"} | {"source": "cubbie_computed", "student_confirmed_at": "2026-10-24T09:10:00Z"},
  "consent": {"matching": true, "publication": {"tier": "card+name"}},  // tier: card | card+name
  // Pairing signals (Track 2), all optional, replaced on every post:
  "events": [ {"url": "https://careers.asu.example.edu/fall-fair", "start": "2026-10-30T10:00",
               "end": "2026-10-30T14:00", "tz": "America/Phoenix", "category": "career"} ],   // 0..10
  "events_confirmed_at": "2026-10-24T09:10:00Z",   // REQUIRED when events is non-empty (provenance, not proof)
  "busy_load": 2                                   // whole number 0..3, nothing else
}
```

Validation (422 `payload_forbidden` w/ detail on violation):
- unknown keys anywhere → 422; windows: ≤20; each start<end; duration ≤8h; within next 30 days; `tz` must load via
  `zoneinfo.ZoneInfo`; tags ⊆ TAG_VOCAB and ≤5 (dedup ok); wants ⊆ {hangout,event}.
- `free_time.source == "cubbie_computed"` REQUIRES `student_confirmed_at` (client-supplied acknowledgement:
  provenance, not proof — never call it verification).
- `consent.matching false` is valid: store nothing but a consent_log entry.
- `events[]` (server/events.py): `extra="forbid"`; `url` passes the same checks as an activity link and is stored
  canonical as `https://host/path` (host lower-cased, `:443`, trailing `/` and everything after `?` dropped);
  `category` in `career|club|talk|music|sports|arts|food|party|volunteer|other`; start<end, duration ≤24h, not already
  over, starts within 30 days; ≤10 events. `busy_load` is a strict integer 0..3 (`true`, `"2"`, `1.5` are refused).
  Events and `busy_load` live in `user_events` / `user_load`, which cascade from `availability`, so expiry, leave-matching,
  delete-account and pairing revoke remove them with it. Matching adds 4 points per link both students sent and lists
  `shared_events: [{url, category}]` (the intersection only) in the card reasons; `refresh_events` re-derives that part on
  open cards when either side posts again. The rule baseline ignores `busy_load` on purpose (a ranking model is compared
  against it).

Write path (one `immediate` txn): delete existing windows+tags for user; convert window (start,end,tz) local→UTC;
upsert `availability` (`payload_hash`=sha256 canonical json, `observed_at=now`, `expires_at=now+STATE_TTL_DAYS`,
`written_by_agent_id`, `source`); insert windows/tags; upsert grants per §7; append `consent_log` rows
(`action: "state:received"`, detail has payload_hash + ack timestamp); then call
`matching.generate_for_user(conn, user_id)` (ignore result). Response `201 {"changed": bool, "expires_at": iso}` —
`changed` = payload_hash differs from previous row.

`TAG_VOCAB` lives in `server/schema.py` as a module constant:
`["bouldering","climbing","hiking","running","gym","swimming","cycling","basketball","soccer","tennis",
"ultimate_frisbee","board_games","video_games","chess","painting","drawing","photography","pottery","guitar",
"piano","singing","live_music","edm","techno","rock","jazz","coffee","baking","cooking","language_exchange",
"debate","anime","cinema","volunteering"]`.

## 6. Matching (P2a)

`matching.generate_for_user(conn, user_id)` (no router): after a state write, find all OTHER users with live
availability (expiry read-checked), not blocked (blocks table, both directions via hashed ids), no existing
non-void/hidden candidate for the unordered pair (`user_a < user_b` store ordered), no swipes row for the pair.
Score = `interest_points + 2*overlap_hours + 1*(both want event)` (+4 per shared public event, see §5);
`reasons_json = {"shared_tags": [...], "overlap_hours": N, "both_want_event": bool}` plus `related` (same-domain
tag pairs) and `shared_events` when present. `interest_points` comes from `server/taxonomy.py`: tag similarity is
1.0 for the same tag, 0.5 for the same domain, 0.2 for compatible domains, else 0; each tag takes its best match on
the other side (max, never a cross-product), weighted by rarity (`2*ln((N+1)/(n+1))/ln(N+1)`, bounded to
[0.5, 2.0], and exactly 1.0 while fewer than 8 users are live), averaged over both directions, ×3, capped at 9.
`SIMILARITY_FLOOR` (default 0) can require a best-pair similarity before any card exists. `shared_tags` stays the
exact intersection, because the activity offer needs a tag both students really have. Needs at least one overlap window ≥30 minutes; pick the longest
such intersection and create `candidates(state='open', expires_at=min both expires_at)` plus proposal
`version=1, state='proposed', window_start/end=the intersection, expires_at=candidate.expires_at`. Push inbox
items to BOTH users (kind `candidate`) via `db.push_inbox` with payload composed through `server/wording.py`
(see §9). Return list of new candidate ids. If `user_a == user_b` impossible; users without consent grants are
skipped entirely (no matching grant for EITHER side → no candidate on that pair).

## 7. Grants & authorization (all phases must obey)

One live row per (user,kind): upsert = withdraw old (set `withdrawn_at`), insert new with `version=old+1` (start 1),
`expires_at = now+30d`, log to `consent_log` (action `grant:<kind>` / `withdraw:<kind>`).

The authorization rule, enforced AT THE MOMENT OF USE (never cached) by whoever serves a card or composes a
notification about user X to user Y:

The authorization rule per FLOW.md, enforced AT THE MOMENT OF USE (never cached):
- **matching grant live for X and Y** — always (a card exists only between matched users).
- **publication grant (`card` or `card+name`) live for X** — always; `card+name` subsumes `card`.
- **`card+name` live for X** — REQUIRED only if the card/text contains X's display name; a bare `card` grant
  demotes to pseudonym, never blocks.
- **not blocked either direction** — always.
- **X and Y availability unexpired** — always, read-time checked.

`server/wording.py` provides the enforcement seam (FROZEN): `live_grant(conn,user_id,kind,tier=None)`,
`name_visible(conn, about_uid)` (True iff publication tier `card+name` live), `label(conn, about_uid)`
(display name if allowed else `pseudonym_for`), `pseudonym_for(conn, uid)` = first tag + "-" + sha256(uid)[:4].

## 8. Scheduling & concurrency (P2b)

Endpoints (session auth): `GET /api/card/{candidate_id}`, `POST /api/swipe` `{candidate_id, proposal_version,
direction}`, `POST /api/counter` `{candidate_id, window_start, window_end, tz}` (local), `POST /api/revoke`
`{candidate_id}`, `POST /api/leave-matching`, `POST /api/delete-account` `{"confirm": true}`.

- **Card** (applicant = session user; must be candidate counterpart, else 404): JSON per contract §9.2. Never
  cached; serve-time grant check; blocked or hidden → 404; expired → 404 `{"error":"expired"}`.
- **Swipe semantics**:
  - `right` — a VERSION-BOUND CONFIRMATION. In ONE `immediate` txn, in this check order: (1) revoke_intents for
    either user >= proposal.created_at → 409 `intent_present`; (2) both matching grants live; (3) X publication
    grant live (for continued card serving — a withdrawn card grant does NOT block an existing confirmation, but
    suppresses new cards); (4) not blocked; (5) `proposal_version` matches → else 409 `stale_version`; (6) both
    availabilities unexpired and STILL COVER the whole window (a covering availability window per user, read-time);
    (7) no overlap with any other proposal this user has already confirmed/both_confirmed/closed → else 409
    `slot_conflict`; (8) insert confirmation (idempotent on PK); second confirmation → proposal `both_confirmed`,
    candidate → `matched`. Push inbox (§9). If the candidate is `state` in declined/voided/hidden/matched → 404.
  - `left` — candidate `declined`; no inbox. Unique(user,candidate) guard (repeated swipe replaces nothing: 409).
  - `block` — blocks rows BOTH directions (hashed), candidate `voided`, inbox revocation to counterpart only.
  - `report` — candidate `hidden` both directions + `reports` row; NO blocks row (report ≠ block; moderation may).
- **Counter** — inside txn: window must be inside a live covering window for BOTH users; old proposal `voided,
  superseded_by=new`, new proposal `version+1, state='proposed'`; confirmations do NOT carry over; inbox `counter`
  to counterpart.
- **Revoke** — FIRST write `revoke_intents(user_id, now)` AUTOCOMMIT (outside/`before` BEGIN IMMEDIATE) —
  intent exists before contending. THEN in `immediate` txn: proposal → `voided` (state `revoked` not a column — use
  voided), candidate → `voided`, push `revocation` inbox to both. End-state guarantee: whichever interleaving with
  a concurrent confirm, final state is revoked/voided; a confirmation that committed first is then voided by the
  revoke txn.
- **Leave-matching** — withdraw matching grant; delete live availability; candidates for/against → `voided`;
  open proposals → `voided`; revocation inbox to living counterparts; grants publication/reveal untouched.
- **Delete-account** — one txn: strip user (email='', display_name='deleted user', password_hash='',
  deleted_at=now); delete windows (availability row + windows), tags, swipes, grants (all craft rows withdrawn),
  candidates for/against, proposals + confirmations, pairings (revoked), inbox_items; strip `consent_log` detail
  for user_id_hash: set detail_json='{}' and `purge_after=now+90d`. Keep: blocks rows (pseudonymous), report rows,
  stripped consent rows.

Also add: `GET /api/dashboard` (session; P1d owns? no — P2b): list of THIS user's open candidates whose counterpart
side hasn't declined: card_url + ids. P1d builds the page from this endpoint shape:
`{"candidates": [{"candidate_id": int, "card_url": str, "state": str}]}`.

## 9. Inbox & wording (P2c)

`GET /api/inbox` (bearer): `?since=<seq>` default 0. Bump `last_seen_at`. In one read: all items seq > since for
user, ordered; response `{"items": [{"seq": n, "kind": "...", "payload": {...}, "created_at": iso}], "now": iso}`.
`payload` ALWAYS includes `"message"` (the exact text Cubbie reads) and `"card_url"` when relevant.

Exact message texts (compose via `wording.label` so names obey grants; these strings are the contract):

- `candidate`: `"{label} is up for {w}. See the card: {url}"` → e.g. `Ava is up for Thu 19:00–21:00. See the card:
  http://localhost:8000/m/12`
- `confirmation`: `"{label} confirmed {w} — confirm your side: {url}"` — NEVER "you're on".
- `match` (both_confirmed, to both): `"It's on: {w} with {label}."`
- `revocation` (to both): `"The plan for {w} was withdrawn."`
- `counter`: `"{label} suggested {w} instead. See the card: {url}"`

`w` = `wording.fmt_window(start_utc, end_utc, tz)` → `"Thu 19:00–21:00"` (weekday short in the window's stored tz;
see tests/util for fixtures). Card JSON (for `/api/card` + page): `{candidate_id, proposal_id, proposal_version,
about: {pseudonym, display_name|null, preset_avatar_id, hobby_tags:[]}, window: {start_utc, end_utc, tz, display},
reasons: {...}, card_url, state}`.

## 10. Expiry & sweep (P2c)

- Read-time: every query touching availability/candidates/proposals filters `expires_at > db.utcnow()` and treats
  expired rows as void. **This is mandatory in every module, not just the sweep.**
- `server/sweep.py` (FROZEN interface): `run_sweep(conn)` — DELETE expired availability (cascade windows); VOID
  derived candidates/proposals; close proposals whose window has passed (`state='closed'` if both_confirmed, else
  voided); purge blocks older than 12 months; purge consent_log rows with purge_after < now; `loop()` interval
  `POLL_SWEEP_SECONDS` — integrator wires into app lifespan (already done in `server/app.py`).

## 11. Web UI (P1d)

Session cookie `smsession` (itsdangerous, `server/sessions.py` is FROZEN). Pages (Jinja2 templates under
`server/webui/templates`, one `static/style.css`, vanilla JS fetch — NO node build, NO extra deps):

- `GET /register`, `GET /login` (forms POST to JSON endpoints below)
- `POST /api/register` `{email, password, display_name, avatar_id, adult_attested, registration_code}` — wrong
  code → 403 `invite_required`; attestation false → 403 `attest_required`; email exists → 409. On success the
  session cookie is set (auto-login).
- `POST /api/login` `{email, password}` → sets cookie; wrong → 401.
- `POST /api/logout` → clears cookie.
- `GET /` dashboard: if session → list of candidates via `GET /api/dashboard` (shape above; P2b implements — P1d
  renders from a placeholder fixture in templates and does not test this against P2b), plus standing "share
  availability" explainer linking the skill instructions. If not session → redirect /login.
- `GET /settings` + `POST /api/settings/grants` `{matching?: bool, publication?: {"tier": "card"|"card+name"|"off"}}`
  ("off" = withdraw publication grant). Upserts per §7, logs consent, idempotently returns current grants.
- `GET /m/{candidate_id}` — card page rendering `/api/card/{candidate_id}` JSON; buttons: Yes (swipe right),
  No (left), Suggest another time (counter, time `<input type="datetime-local">` + tz select), Block, Report,
  Withdraw from this match (revoke).
- `GET /matches` — own candidates state matched with window display.
- `GET /privacy`, `GET /terms` — render `docs/privacy.md`, `docs/terms.md` via markdownish `<pre>` fallback or
  simple wrap (docs arrive later — link "full policy coming" placeholder if file missing).
- `GET /m/pair/{code}` — approves the pairing code for the logged-in user: shows the code + Approve button POSTing
  `/api/pair/approve`.
- Settings page also shows a "Delete my account" button (POST /api/delete-account, confirm via JS dialog) —
  endpoint owned by P2b; page just needs the button.
- Passwords via `server/passwords.py` (FROZEN): `hash_password(pw)`, `verify(pw, stored)` (pbkdf2
  sha256, 200k iterations, format `salt$hex`).

Tests you own: register/login/logout flow, invite code 403, attestation 403, settings grant toggle upserts correctly
(check grants table), unauthenticated access to settings API → 401, duplicate email → 409.

## 12. Cubbie skill (P1c)

`skill/` package, stdlib only (no third-party imports, no uv):

- `skill/cubbie.yaml` — REMOVED post-final-gate (2026-10-25): the kit's validator itself warns "cubbie.yaml is not
  needed and Cubbie ignores it", so the file shipped only as self-documentation and is now gone; `SKILL.md` is the
  operative instruction file. The historical spec text below is kept for provenance
  `/home/nav/active-code/devhacks3/cubbie-hackathon-kit/skills/cubbie-skill-creator/references/cubbie-capabilities.md`,
  `cubbie-auth.md`, and `schemas.md`; mirror the structure/fields the references document (network host declaration
  pointing at `http://localhost:8000` as the default server, invoked commands, entry points for
  `scripts/pair.py` and `scripts/bus.py`). If references conflict, document the conflict in your report and follow
  the more conservative reading.
- `skill/scripts/pair.py` — `--start` (POST /api/pair/start, print JSON to stdout), `--finish <code>`
  (POST /api/pair/finish, store `{token, agent_id}` in `auth.json` under `--data-dir` (default
  `./skill_data`, mode 600), print result), `--status` (print stored identity + server), `--forget` (POST
  /api/pair/forget with bearer, delete auth.json). Server from env `SOCIAL_MATCH_SERVER` (default
  `http://localhost:8000`).
- `skill/scripts/bus.py` — `--poll [--since N]` (GET /api/inbox; print each item message text + card_url lines),
  `--send-state <payload.json>` (POST /api/state with validated minimal payload file), `--revoke <candidate_id>`
  (POST .../api/revoke is session-web-only**; NOTE the revoke is web-session-only — for the skill print instruct
  text and exit code 2; do NOT invent a bearer revoke path), `--whoami`. `auth.json` shared with pair.py. Stdlib
  `urllib.request` only.
- `skill/SKILL.md` — instructions oriented to the Cubbie agent: when triggered, poll inbox; how to compute free
  windows from the student's calendar WITHOUT ever copying event titles (delete titles, keep ranges — a redaction
  step: ranges and tags only), build payload JSON per CONTRACT §5, ask the student BEFORE sending
  (student_confirmed_at = that moment), then `bus.py --send-state`; show messages from `--poll` verbatim.
- `skill/payload.example.json` — a valid example payload.
- Self-test via a `--selftest` flag on bus.py (argument parsing, no server calls) + a tiny `skill/README.md`.

## 13. Test matrix (owned tests must be green with only phase-0 stubs in place)

- `tests/test_smoke.py` (frozen): /health 200.
- P1a: pair start→approve→finish happy path; code single-use (second finish 410); expired code; approve without
  session 401; rotate invalidates bearer after grace; forget revokes + removes only that agent's availability;
  unknown vs revoked token distinct 401 bodies.
- P1b: 422 on unknown top-level key; 422 on unknown window key; ≤20 windows; 8h bound; bad tz; free-text tag; 6
  tags; wants whitelist; cubbie_computed without ack → 422; happy path writes availability+windows+tags and
  grants; changed flag flips only on real change; THE THERAPY TEST: build payload with storage objects carrying
  `"Therapy — Dr. Ruiz"` and POST it (calling /api/state with extra field `calendar_titles`), assert 422 AND
  assert no accepted request body contains `b"Therapy"`.
- P1d: §11 tests list.
- P2a: scoring arithmetic; overlap picks longest ≥30min; blocked pair excluded; swiped pair excluded; expired
  counterpart skipped (seed expires_at in past); duplicate pair not regenerated; inbox candidate pushed to both.
- P2b: revoke→confirm and confirm→revoke interleavings BOTH end revoked/voided (threads racing a contiguous
  window, two connections); duplicate confirm idempotent 200 single row; slot_conflict on overlapping confirmed
  windows; stale_version on old version swipe; block suppresses future card 404; report hides candidate; counter
  voids old proposal and bumps version; card for non-counterpart → 404; delete-account cascades (assert users
  stripped, windows gone, consent detail stripped with purge_after set, blocks retained); leave-matching cascades.
- P2c: since cursor monotonic; bump of last_seen; wording: candidate message uses display name when card+name,
  pseudonym when card; confirmation text contains "confirm your side" and never "you're on" (assert absent);
  run_sweep voids expired candidates and cleans blocks >12mo.
- P3a: scenario (§14) — asserts through the HTTP API layer only.
- Seed helpers for 2nd-phase tests: `tests/util.py` (frozen): `mk_user(email, name)` direct SQL row + returns id;
  `grant(uid[, kind][, tier])`; `windows(uid, start_end_pairs, tz)`; `mk_candidate(a, b, window)` (+ grant+windows
  defaults); `confirm(client, uid, candidate_id, version)`; `login(client, email, pw)`; `direct_session(client,
  uid)` (sets cookie without webui); `fixed_now` monkeypatch notes.

## 14. Demo scenario (P3a)

`demo/scenario.py`, runnable `uv run python demo/scenario.py [--against-url http://…]` (default spins TestClient
in-process; `--live` uses httpx against a running uvicorn for filming). Twelve scripted, asserted beats:

1. Ava registers (card+name), Ben registers (card). pair both via API; print link codes.
2. Ava Cubbie-path state (cubbie_computed + ack), Ben student-path state — same window Thu 19–21.
3. Matching creates candidate+proposal; assert the ASYMMETRY in inbox texts: the message to Ava is about Ben
   (pseudonym, because Ben granted only `card`); the message to Ben is about Ava (display name, `card+name`).
5. Ava right-swipes v1. Ben inbox: "confirmed — confirm your side" containing Ava name.
6. Ben right-swipes v1 → both_confirmed; both inbox "It's on"; statuses matched.
7. Duplicate confirm by Ben → 200 idempotent, single confirmation row.
8. Race: new candidate pair or reset — Ava+Ben revocations? Beat: replay with second pair (Ben2?) — simpler:
   revoke beat — Ava revokes; proposal voided; inbox revocation both; then Ava re-swipes → 409.
9. Counter beat: fresh pair, disjoint suggests counter → old voided, new version 2, inbox text.
10. No-overlap beat: fresh pair disjoint windows → no candidate; "tell the student honestly, no time found".
11. Block beat: block future generation; report event hides pair.
12. Payload/422 beat: bad payload AND therapy title absent; safe text: card payload never contains Ben's tags beyond vocab. Print PASS per beat + final summary + "what crossed the wire" line.

## 15. Out of scope (do not build)

Reveal/thread endpoints (grant row may exist, no channel), events discovery, bookings, photo upload, repeat-account
prevention, E2E email, moderation UI. Deferred items stay absent from code, not stubbed halfway.

## 16. Honesty ledger — every doc/page/test must keep these claims exact

- "student_confirmed_at" is PROVENANCE not proof; server cannot verify a human read anything.
- Availability expires `STATE_TTL_DAYS` (7) after observation; never read expired rows.
- Closed matches/swipes/reasons retained 30 days; `consent_log` keeps `{hash, action, timestamp}` 90 days after
  account deletion; blocks 12 months; backups-age caveat goes in docs verbatim as: "Deleted rows persist in
  encrypted backups until they expire, within 14 days."
- 18+ is self-attestation, not age verification.
- Activity offers (Track 2): an offer is a tag from the closed vocabulary plus an https link and nothing else; the link is
  the only free-form string on an offer and it is validated, never fetched (public-event links get the same checks, see below). Reason codes are a closed list of
  four. The demo's agents are scripted stand-ins: in production the choosing is done by each student's Cubbie following
  `social-match/SKILL.md`, and the server cannot verify that an agent applied its student's rules.
- Agents agreeing on an activity is NOT the plan being on: each student still confirms their own side, and wording must
  never say "it's on" or "you're on" before both have.
- Public events (Track 2): an event is a link to a public page, a time and one category from a closed list, and nothing
  else; no title, sender, location or email text is accepted. The student approves the list on their phone
  (`events_confirmed_at`, provenance not proof). The server validates the link's shape, drops the `?query`, never opens it,
  and cannot tell a public page from one with a personal code in its path. A match sees an event only if both students
  sent the same page. `busy_load` is one agent-computed number the server cannot verify.
- Interest similarity (`server/taxonomy.py`): the domains and the compatible-domain pairs are our own assumptions, and
  the rarity weights come from the few live users, so they are noisy and are exactly 1.0 until 8 users are live. The
  score is a fixed formula, not a trained model, and it never overrides consent, blocks or the 30-minute overlap.
- The server accepts allowlisted payloads AND REJECTS everything else with 422; we do NOT claim trusted Skill code
  cannot send data elsewhere.
