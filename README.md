# Social Match — Track 2 hackathon entry

Sign in, talk to your own AI assistant, and let two assistants **find where your free time and interests overlap and
agree on one real thing to do** (a climbing session, a coffee) through our broker server. Neither student's calendar,
courses or grades is stored anywhere but their own device: the chat path takes windows you type, or an `.ics` you upload
that the server reads once in memory and reduces to intervals before storing anything. What crosses the wire is a handful
of time ranges, a few interest tags (canonicalised on the server), answers to five consent questions, and — if you use
the chat — the text you type to your own assistant.

The division of labour is the whole idea:

| Piece | Role |
| --- | --- |
| **Our chat app** | the interface. You tell your assistant when you are free (or upload an `.ics`), it shows you a normalised preview, you press Confirm, and the chips and Settings page show what actually changed. |
| **Your assistant** | your agent. It reads your own state, applies changes through the server's validated write path, and — if you delegate — accepts, counters or passes offers against the other student's assistant. |
| **Our server** | the broker. It validates every write against an allowlist, decides who overlaps whom, runs the five consent grants, feeds the model provider on your behalf, and owns every piece of state and every retention clock. |
| **The website (same app)** | where the card, the swipe and the confirm live. A confirmation is a signed-in press; no assistant can swipe or confirm for you. |
| **The Cubbie skill** | the **legacy / alternate client**. Endpoints and the pairing flow are unchanged: it still computes free time on the phone and POSTs a redacted payload. The chat app does the same job without it. |

The product rationale is in `FLOW.md`. The implementation contract, section by section, is `CONTRACT.md` (its §16
"honesty ledger" is binding on this README and on everything under `docs/`); the chat surfaces are specified in
`CONTRACT-chat.md` and `PLAN-chat-frontend.md`.

## Quickstart

```bash
uv sync                                   # install the server (Python >= 3.11)
cp .env.example .env                      # every value optional; defaults shown in the file
uv run uvicorn server.app:app --reload    # http://localhost:8000
uv run pytest -q                          # 273 tests before the chat lanes, a few seconds
```

Then open <http://localhost:8000/register>, enter the invite code (`REGISTRATION_CODE`, default `cubbie2026`), tick the
18+ attestation, and you land on the chat page: talk to your assistant, define your schedule by hand or by `.ics`, and
the dashboard, card, swipe, matches, settings and legacy inbox pages are all there.

> Run the end-to-end demo with `uv run python demo/scenario.py` — seventeen asserted beats over the
> real HTTP API (register → pair → state → match → card → confirm → idempotent re-confirm →
> revoke → counter → no-overlap → block/report → therapy-payload rejection → manual windows →
> `.ics` preview → confirm → chat tag apply), ending with the
> honest "what crossed the wire" summary. For filming against a live server:
> `uv run python demo/scenario.py --against-url http://localhost:8000`.

## Configuration

Every value is read at call time (env vars, or `deploy/run.sh`'s `socialmatch.conf`). The chat block:

| Variable | Default | What it does |
| --- | --- | --- |
| `AI_API_URL` | unset | Full POST URL of an **OpenAI chat-completions** endpoint. Unset → chat disabled: the page shows a banner and the POST returns `503 {"error":"ai_unconfigured"}`. |
| `AI_API_KEY` | unset | Bearer key for that provider. Never sent to the browser. |
| `AI_MODEL` | no code default; `.env.example` ships `z-ai/glm-5.3`, provisional until one live turn confirms the name | Model name in the request body. |
| `AI_MAX_TOKENS` | `800` | Reply cap sent to the provider. |
| `AI_TIMEOUT_SECONDS` | `60` | Wall-clock limit for one turn. |
| `AI_TTL_DAYS` | `7` | Chat-transcript retention. Matches the demo retention table; the rate-limit rows outlive it. |
| `DELEGATION_DEFAULT_ON` | off when absent; `.env.example` ships `1` for the demo box | `1` seeds the **delegation** grant (version 1 + consent-log row) inside the registration transaction, so demo accounts negotiate hands-free. Leave it unset in a real deployment; it is demo-box only, and only Settings can change the grant afterwards. |

The protocol is OpenAI chat-completions **only**. A non-OpenAI shape (Anthropic `content_block_delta`, an empty
completion, a non-SSE body) is a typed `ai_protocol` failure shown to you, never a silently empty reply. Switching
`AI_API_URL` does not imply compatibility with whatever you point it at.

## Repo layout

| Path | What it is |
| --- | --- |
| `server/app.py` | FastAPI app: mounts the routers, starts the sweep loop and the negotiation worker. |
| `server/schema.py` | **The allowlist for structured state.** `windows`, `tags`, `wants`, `free_time`, `consent` — and nothing else. Unknown keys at any level → 422 `payload_forbidden`. |
| `server/statewrite.py` | The one validated state-write service. `mode="agent"` keeps today's grant-renew semantics for the legacy client; `mode="chat"` is **grant-preserving** — it never creates, renews, restores or withdraws a grant, and revalidates liveness and state version inside the write transaction. |
| `server/state.py` | `POST /api/state`: the legacy client's write path, now a thin call into `statewrite`. |
| `server/ai.py` | OpenAI chat-completions client: SSE parsing, typed errors, redaction, response byte cap. |
| `server/chat.py` | Chat endpoints, action protocol, pending-preview store, per-user lock, rate rows. |
| `server/schedules.py` | Manual windows + bounded `.ics` upload → preview → confirm. |
| `server/negotiate.py` | Assistant-vs-assistant offer worker, gated on the delegation grant. |
| `server/matching.py` | Candidate generation: overlap length, shared tags, `both_want_event`, score, reasons. |
| `server/activity.py`, `server/offers.py` | **Assistant-to-assistant negotiation (Track 2).** `POST /api/agent/activity/propose` and `/respond`, `GET /api/agent/activity/{id}`: an offer is a shared tag plus an https link; the answer is accept, counter or pass with a reason code; 3 rounds at most. |
| `server/scheduling.py` | Card, swipe (confirm/decline/block/report), counter, revoke, leave-matching, delete-account. |
| `server/wording.py` | Message composition + the grant enforcement seam (`live_grant`, `name_visible`, `label`, pseudonyms). |
| `server/inbox.py` | `GET /api/inbox` cursor; the inbox is a log, not a live view. |
| `server/pairing.py` | Pairing codes, token rotation, `/pair/forget`. |
| `server/sweep.py` | Expiry/purge sweep, including the 7-day chat transcript purge (read-time expiry is enforced in every query; the sweep only materialises it). |
| `server/sessions.py`, `server/auth.py`, `server/passwords.py` | Session cookie, request identity, password hashing. |
| `server/config.py` | Every tunable number, read at call time. |
| `server/db.py` | SQLite connection helpers, `utcnow`, `immediate` transactions, `user_hash`. |
| `server/webui/` | Templates + routes for register, login, chat, schedule, dashboard, card, matches, settings, privacy, terms, pair-approve. |
| `schema.sql` | Authoritative schema (SQLite), including `chat_messages`, `chat_turns`, `chat_pending`. |
| `social-match/` | The Cubbie Skill (legacy client): `SKILL.md`, `scripts/pair.py`, `scripts/bus.py`, `payload.example.json`. |
| `tests/` | The suite (273 before the chat lanes): payload allowlist, matching, public-event signals, activity negotiation, concurrency, pairing, inbox/sweep, web UI, photos, review regressions, and the chat and schedule lanes. |
| `references/` | Cubbie platform docs used to shape the Skill. |
| `docs/` | This repo's privacy policy and terms, which the server itself serves at `/privacy` and `/terms`. |

## Installing the Cubbie skill (legacy / alternate client)

A Skill is a folder with a `SKILL.md`. Copy `social-match/` into the student's Cubbie Skill folder and that is the entire
install — no key to paste, no package to add, standard library only. The endpoints and the pairing flow are unchanged by
the chat app; a deployment can use both, and what a skill writes lands in the same state the chat reads.

**`social-match/SKILL.md` is the operative instruction file.** It is what the agent reads and follows: the triggers, the
redaction rules, the exact wording of the consent question, and the "never say *you're on*" wording rule.

`social-match/cubbie.yaml` ships, but **Cubbie ignores it**: the kit's own structure check calls a leftover
manifest "not needed, ignored", and the kit documents no schema for one. It is self-documentation only — nothing
depends on it. `SKILL.md` is the operative file, and `SKILL.md` plus `social-match/scripts/` is the whole install.

The Skill's own hard rules, quoted because they are the product:

- **Nothing leaves the phone until the student reads it and says yes**, in their own words, in the same conversation.
- **Calendar contents never leave the phone** — not titles, not attendees, not locations, not course codes.

The chat path reaches the same guarantee differently: you type your free time (or upload an `.ics` that the server reads
once in memory), you see a normalised preview, and only the windows you tick are stored. The calendar file is never
spooled to disk, and titles, locations and attendees are never stored or logged.

## The five consent grants

Everything the server is allowed to do with a student's data is one of five grants, all server-enforced, all versioned,
all recorded in `consent_log`, all re-checked at the moment they are relied on (never cached), and all revocable from
the settings page: **matching** (your windows and tags may be used to generate candidates, and again at
confirmation — withdrawing it stops generation and voids in-flight plans); **publication · card** (a pseudonym, a preset
avatar and your hobby tags may be shown to people you match with); **publication · name** (your display name goes on
that card and into the notification text, to matched users only — never public, never searchable, and withdrawing it
reverts *future* cards and texts to the pseudonym, because texts already sent cannot be recalled); **reveal** (a
pair-scoped way to message someone, once both sides say yes — *designed, not in the demo*, the grant row exists and no
channel does); and **delegation** (*"Let my assistant accept, counter or pass offers for me"* — off by default, **only
Settings can change it**, the demo box seeds it via `DELEGATION_DEFAULT_ON=1`, and no chat operation can ever touch it).
Publication grants expire 30 days after they are granted; every grant is a row with a version, so the
version that was approved is the version that gets served.

## Judge hooks

**Prototype + scenario script.** The prototype is the running app: sign in → talk to your assistant → define your
schedule by hand or by `.ics` → the assistant applies state changes behind a preview and Confirm → assistants negotiate
an offer when both sides delegate → humans swipe and confirm. `demo/scenario.py` runs seventeen asserted beats over the
real HTTP API; the last three are the chat-era beats (manual windows, `.ics` preview + confirm, chat tag apply), and the
activity negotiation is the standing agent beat.

**Conflicting constraints** — the interesting beats, and where each is proven:

| Beat | What must happen | Proven by |
| --- | --- | --- |
| Overlap pick | Two students with several shared free windows get exactly one card, over the **longest** overlap (tie → earliest start). | `tests/test_matching.py::test_longest_overlap_wins_tie_earliest_start` |
| Counter | A second round of negotiation: the old proposal is voided, a new version is proposed, and the counterpart is told. | `tests/test_concurrency.py::test_counter_voids_old_and_bumps_version`, `::test_counter_outside_free_time_422` |
| No-overlap refusal | Genuinely disjoint free time produces **no** card — the honest answer is "no time found", not a bad suggestion. | `tests/test_matching.py::test_sub_30_minute_overlap_produces_no_candidate` |
| Budget conflict | One assistant's private budget rules out the other's pick, so it counters with another shared interest and a reason **code**; the first assistant accepts. No amount ever leaves the phone. | `tests/test_activity.py::test_pass_with_reason_then_counter_then_accept`, demo beat 13 |
| Round cap | After 3 offers the server stops; the students are told no activity worked out and can still confirm the time. | `tests/test_activity.py::test_round_cap_then_time_only_fallback` |
| Two takers | One student confirming two plans in the same window is a `409 slot_conflict`; concurrent confirm/revoke races end revoked, never double-booked. | `tests/test_concurrency.py::test_overlapping_slot_conflict`, `::test_race_confirm_vs_revoke_real_threads` |
| Stale preview | A pending preview from an older state version cannot be confirmed: the confirm re-checks the state hash and refuses, applying nothing. | `tests/test_chat.py` (optimistic-conflict path) |

**What your assistant shares vs. what never leaves.** Across the wire, and only after consent: UTC time ranges (+ a tz
for display), interests in the student's own words (canonicalised server-side onto a 34-word vocabulary, so "matcha"
scores as `coffee`; unmatched interests are kept but never scored or shown), `wants` (`hangout` / `event`), a boolean
consent block, and — after the
student's ack — a `student_confirmed_at` timestamp. Never in structured state: calendar titles, attendees, locations,
course codes, professor names, grades, or free text. That is not a convention, it is `server/schema.py`:
`extra="forbid"` at every level, closed tag list, and a 422 `payload_forbidden` for anything else (see
`tests/test_payload.py::test_therapy_titles_never_accepted` and `::test_free_text_tag_rejected`).

The chat box is the one deliberate free-text surface, and it is bounded: 4,000 characters per typed turn, a 200-message
history window, 7-day retention, and 20 turns per 10 minutes. It goes to **your own assistant**, and only through the
model provider named in the configuration; it is never another student's feed and it is never a bio. Assistant prose
never enters your structured state, and only a server-templated note of a few fixed fields (decision, tag, link, reason
code) crosses into the other student's transcript.

**How the assistants negotiate.** Either assistant may offer an activity for a match; the other accepts, counters, or
passes. An offer is
`{candidate_id, proposal_version, activity_tag, activity_link}`: the tag must be an interest **both** students already
have, and the link must be https (`server/offers.py` checks it). An answer is `accept`, `pass` + a reason code, or
`counter` + a new
tag and link. Nothing else is accepted (`extra="forbid"`, test `test_extra_fields_rejected_so_no_title_or_place_can_ride_along`).
The searching is the student's own rules — the legacy client applies them on the phone per `social-match/SKILL.md`
Step 6, and the chat assistant applies your stated intent; the server only holds the rounds. Until an offer is accepted
the students cannot confirm (`409 activity_pending`). Negotiation turns are assistant-context only: a typed chat message
can never emit an offer/accept/counter/pass op, and the apply transaction re-reads the delegation grant, account
liveness and offer version, so withdrawing delegation mid-turn changes nothing.

**When assistants ask, and when humans press.** In the legacy path `SKILL.md` Step 3 still requires the agent to show
the student the exact windows, tags and name choice, and wait for "yes, send it". In the chat path the same gate is a
pending preview: windows and events do not write until you press **Confirm**, which stamps `free_time.student_confirmed_at`
(or `events_confirmed_at`) — the click is the provenance. Tags, wants and busy apply immediately when you ask for them.
Confirm-your-side: a `right` swipe is a version-bound confirmation of one side only, and no message ever says the plan is
on until a second side confirms — the confirmation text says "confirm your side", and
`tests/test_inbox.py::test_confirmation_text_and_no_premature_youre_on` fails the build if "you're on" appears early.

## HONEST CAVEATS

These are limits of what we built, stated plainly. `CONTRACT.md` §16 makes keeping them exact a requirement.

1. **The calendar-derived pipeline still awaits organizer confirmation.** The student→phone half of the legacy path is
   real code (`social-match/`), but that pipeline is demonstrated on synthetic fixtures: the demo posts
   `cubbie_computed` payloads against fixed windows. The chat path sidesteps the question — you type the time or upload
   your own export — but `student_confirmed_at` remains **provenance, not proof**: it records that the client asserted
   "the student saw and approved this" (or that you clicked Confirm), and the server cannot verify that a human read
   anything. It is validated as an ISO timestamp and stored; it is never treated as verification, and it must never be
   described as one.
2. **The invite gate is not identity verification.** `REGISTRATION_CODE` keeps signup closed to people we handed the
   code to; it does not prove who anyone is. **18+ is self-attestation** — the register form records
   `adult_attested_at` because the student ticked a box (`403 attest_required` if they do not), and nothing verifies it.
   There is no ID check and no verification of school affiliation anywhere.
3. **The assistants are models, and the demo does not make a live provider call.** The chat assistant is a language model
   reached over the OpenAI chat-completions protocol; it can be wrong, including about your own state, which is why the
   chips and the Settings page are the truth and every write is revalidated server-side. The autonomous negotiation
   worker is bounded (one op per turn, 3 rounds, a 12-turns-per-10-minutes autonomy ceiling inside the shared 20/10 min
   budget, two failed provider attempts per offer before it falls back to a manual Accept/Counter/Pass card) but it is
   still a model deciding. In `demo/scenario.py` the provider is monkeypatched and beat 13's agents are scripted
   stand-ins with a hard-coded budget rule, so the scenario proves the server-side loop, not a live model's judgement.
   `tests/test_negotiate.py` and `tests/test_chat.py` pin the bounds; a production system would want more.
4. **Links are the free-form strings.** The activity link and the public-event link are validated (https, ASCII, ≤300 chars, real domain, no
   credentials, no fragment, no IP address) and never fetched by the server, but it is still a string a bad client could
   choose. The Skill tells the agent to use only links it found in a public search.
5. **Event links are validated, not vetted.** A student's agent finds public event pages and the student approves the
   list, but the server only checks the link's shape and drops everything after `?`. It cannot tell a public page from a
   page with a personal code in its path, and it never opens the link. Two students match on an event only when their
   links are the same page after cleaning; a different URL for the same event will not match.
6. **`busy_load` is a rough number, and now sometimes a typed one.** It comes from Canvas's last sync and the calendar in
   the legacy path, or from what you tell your assistant in chat, 0 to 3; the server cannot check it. The rule-based
   matcher ignores it, so it does nothing yet except wait for a ranking model.
7. **The demo runset is invite-only and small.** It was run on team devices, with fictional students and synthetic
   calendars. That is enough to show the loop closing; it is not a population, a load test, or a safety record.
8. **The model provider is a processor we do not control.** A chat turn sends the system prompt (your own state), the
   last 40 transcript messages and your text to the provider configured in `AI_API_URL`. That provider may log, retain
   or train on what it receives, and we cannot promise otherwise, delete it on your behalf, or tell you where it is.
   Do not type secrets. The privacy policy states this in the same terms; nothing here promises provider-side deletion
   or anonymity.

## Documentation

- `docs/privacy.md` — what we collect, what we reject, how long we keep it (also served at `/privacy`).
- `docs/terms.md` — what this service is and is not (also served at `/terms`).
- `CONTRACT.md` §16 — the honesty ledger these documents are held to.
- `PLAN-chat-frontend.md` — the approved chat design; `CONTRACT-chat.md` — its names and seams.
