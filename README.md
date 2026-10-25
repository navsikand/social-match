# Social Match — Track 2 hackathon entry

Two students' AI agents find where their free time and interests overlap, then **agree on one real thing to do**
(a climbing session, a coffee) through our broker server. Neither student's calendar, courses or
grades ever leave their own phone. What crosses the wire is a handful of time ranges, a few tags from a closed
vocabulary, and answers to four consent questions.

The division of labour is the whole idea:

| Piece | Role |
| --- | --- |
| **Cubbie** | sensor + mouth. It reads the student's calendar and Canvas, computes free windows, redacts them, asks the student in plain words, sends only the redacted payload, and texts back what came out. |
| **Our server** | the broker. It validates the payload against an allowlist, decides who overlaps whom, runs the consent grants, and owns every piece of state and every retention clock. |
| **Our website** | where a card, a swipe and a confirm live. The web session is what makes a confirmation a confirmation — the agent cannot swipe or revoke on the student's behalf. |

The product rationale is in `FLOW.md`. The implementation contract, section by section, is `CONTRACT.md` (its §16
"honesty ledger" is binding on this README and on everything under `docs/`).

## Quickstart

```bash
uv sync                                   # install the server (Python >= 3.11)
cp .env.example .env                      # every value optional; defaults shown in the file
uv run uvicorn server.app:app --reload    # http://localhost:8000
uv run pytest -q                          # 211 tests, a few seconds
```

Then open <http://localhost:8000/register>, enter the invite code (`REGISTRATION_CODE`, default `cubbie2026`), tick the
18+ attestation, and you get the dashboard, the card and swipe pages, settings, and the inbox the Skill polls.

> Run the end-to-end demo with `uv run python demo/scenario.py` — fourteen asserted beats over the
> real HTTP API (register → pair → state → match → card → confirm → idempotent re-confirm →
> revoke → counter → no-overlap → block/report → therapy-payload rejection), ending with the
> honest "what crossed the wire" summary. For filming against a live server:
> `uv run python demo/scenario.py --against-url http://localhost:8000`.

## Repo layout

| Path | What it is |
| --- | --- |
| `server/app.py` | FastAPI app: mounts the routers, starts the sweep loop. |
| `server/schema.py` | **The allowlist.** `windows`, `tags`, `wants`, `free_time`, `consent` — and nothing else. Unknown keys at any level → 422 `payload_forbidden`. |
| `server/state.py` | `POST /api/state`: the one write path for availability, tags, grants and matching generation, in a single transaction. |
| `server/matching.py` | Candidate generation: overlap length, shared tags, `both_want_event`, score, reasons. |
| `server/activity.py`, `server/offers.py` | **Agent-to-agent negotiation (Track 2).** `POST /api/agent/activity/propose` and `/respond`, `GET /api/agent/activity/{id}`: an offer is a shared tag plus an https link; the answer is accept, counter or pass with a reason code; 3 rounds at most. |
| `server/scheduling.py` | Card, swipe (confirm/decline/block/report), counter, revoke, leave-matching, delete-account. |
| `server/wording.py` | Message composition + the grant enforcement seam (`live_grant`, `name_visible`, `label`, pseudonyms). |
| `server/inbox.py` | `GET /api/inbox` cursor; the inbox is a log, not a live view. |
| `server/pairing.py` | Pairing codes, token rotation, `/pair/forget`. |
| `server/sweep.py` | Expiry/purge sweep (read-time expiry is enforced in every query; the sweep only materialises it). |
| `server/sessions.py`, `server/auth.py`, `server/passwords.py` | Session cookie, request identity, password hashing. |
| `server/config.py` | Every tunable number, read at call time. |
| `server/db.py` | SQLite connection helpers, `utcnow`, `immediate` transactions, `user_hash`. |
| `server/webui/` | Templates + routes for register, login, dashboard, card, matches, settings, privacy, terms, pair-approve. |
| `schema.sql` | Authoritative schema (SQLite). |
| `social-match/` | The Cubbie Skill: `SKILL.md`, `scripts/pair.py`, `scripts/bus.py`, `payload.example.json`. |
| `tests/` | 211 tests: payload allowlist, matching, public-event signals, activity negotiation, concurrency, pairing, inbox/sweep, web UI, review regressions. |
| `references/` | Cubbie platform docs used to shape the Skill. |
| `docs/` | This repo's privacy policy and terms, which the server itself serves at `/privacy` and `/terms`. |

## Installing the Cubbie skill

A Skill is a folder with a `SKILL.md`. Copy `social-match/` into the student's Cubbie Skill folder and that is the entire
install — no key to paste, no package to add, standard library only.

**`social-match/SKILL.md` is the operative instruction file.** It is what the agent reads and follows: the triggers, the
redaction rules, the exact wording of the consent question, and the "never say *you're on*" wording rule.

`social-match/cubbie.yaml` ships, but **Cubbie ignores it**: the kit's own structure check calls a leftover
manifest "not needed, ignored", and the kit documents no schema for one. It is self-documentation only — nothing
depends on it. `SKILL.md` is the operative file, and `SKILL.md` plus `social-match/scripts/` is the whole install.

The Skill's own hard rules, quoted because they are the product:

- **Nothing leaves the phone until the student reads it and says yes**, in their own words, in the same conversation.
- **Calendar contents never leave the phone** — not titles, not attendees, not locations, not course codes.

## The four consent grants

Everything the server is allowed to do with a student's data is one of four grants, all server-enforced, all versioned,
all recorded in `consent_log`, all re-checked at the moment they are relied on (never cached), and all revocable from
the settings page or the Skill: **matching** (your windows and tags may be used to generate candidates, and again at
confirmation — withdrawing it stops generation and voids in-flight plans); **publication · card** (a pseudonym, a preset
avatar and your hobby tags may be shown to people you match with); **publication · name** (your display name goes on
that card and into the notification text, to matched users only — never public, never searchable, and withdrawing it
reverts *future* cards and texts to the pseudonym, because texts already sent cannot be recalled); and **reveal** (a
pair-scoped way to message someone, once both sides say yes — *designed, not in the demo*, the grant row exists and no
channel does). Publication grants expire 30 days after they are granted; every grant is a row with a version, so the
version that was approved is the version that gets served.

## Judge hooks

**Prototype + scenario script.** The prototype is the running app: register → pair a Skill → send a state payload →
get a card → agents settle an activity → both students confirm → counter or revoke. `demo/scenario.py` runs fourteen
asserted beats over the real HTTP API; beat 13 is the agent negotiation.

**Conflicting constraints** — the interesting beats, and where each is proven:

| Beat | What must happen | Proven by |
| --- | --- | --- |
| Overlap pick | Two students with several shared free windows get exactly one card, over the **longest** overlap (tie → earliest start). | `tests/test_matching.py::test_longest_overlap_wins_tie_earliest_start` |
| Counter | A second round of negotiation: the old proposal is voided, a new version is proposed, and the counterpart is told. | `tests/test_concurrency.py::test_counter_voids_old_and_bumps_version`, `::test_counter_outside_free_time_422` |
| No-overlap refusal | Genuinely disjoint free time produces **no** card — the honest answer is "no time found", not a bad suggestion. | `tests/test_matching.py::test_sub_30_minute_overlap_produces_no_candidate` |
| Budget conflict | One agent's private budget rules out the other's pick, so it counters with another shared interest and a reason **code**; the first agent accepts. No amount ever leaves the phone. | `tests/test_activity.py::test_pass_with_reason_then_counter_then_accept`, demo beat 13 |
| Round cap | After 3 offers the server stops; the students are told no activity worked out and can still confirm the time. | `tests/test_activity.py::test_round_cap_then_time_only_fallback` |
| Two takers | One student confirming two plans in the same window is a `409 slot_conflict`; concurrent confirm/revoke races end revoked, never double-booked. | `tests/test_concurrency.py::test_overlapping_slot_conflict`, `::test_race_confirm_vs_revoke_real_threads` |

**What agents share vs. what they never share.** Across the wire, and only after consent: UTC time ranges (+ a tz for
display), interests in the student's own words (canonicalised server-side onto a 34-word vocabulary, so "matcha"
scores as `coffee`; unmatched interests are kept but never scored or shown), `wants` (`hangout` / `event`), a boolean
consent block, and — after the
student's ack — a `student_confirmed_at` timestamp. Never: calendar titles, attendees, locations, course codes,
professor names, grades, or any free text. That is not a convention, it is `server/schema.py`: `extra="forbid"` at every
level, closed tag list, and a 422 `payload_forbidden` for anything else (see
`tests/test_payload.py::test_therapy_titles_never_accepted` and `::test_free_text_tag_rejected`).

**How the agents negotiate.** Either agent may offer an activity for a match; the other accepts, counters, or passes. An offer is
`{candidate_id, proposal_version, activity_tag, activity_link}`: the tag must be an interest **both** students already have, and
the link must be https (`server/offers.py` checks it). An answer is `accept`, `pass` + a reason code, or `counter` + a new
tag and link. Nothing else is accepted (`extra="forbid"`, test `test_extra_fields_rejected_so_no_title_or_place_can_ride_along`).
Cubbie does the searching and applies the student's private rules on the phone (`social-match/SKILL.md`, Step 6);
the server only holds the rounds. Until an offer is accepted the students cannot confirm (`409 activity_pending`).

**When agents ask.** Ask-before-send: `SKILL.md` Step 3 requires the agent to show the student the exact windows, tags
and name choice, and wait for "yes, send it" before anything is sent. Confirm-your-side: a `right` swipe is a
version-bound confirmation of one side only, and no message ever says the plan is on until a second side confirms — the
confirmation text says "confirm your side", and `tests/test_inbox.py::test_confirmation_text_and_no_premature_youre_on`
fails the build if "you're on" appears early.

## HONEST CAVEATS

These are limits of what we built, stated plainly. `CONTRACT.md` §16 makes keeping them exact a requirement.

1. **The calendar-derived pipeline awaits organizer confirmation.** The student→agent half is real code (`social-match/`), but
   the *server* half of the pipeline is demonstrated on synthetic fixtures: the demo posts `cubbie_computed` payloads
   against fixed windows. And `student_confirmed_at` is **provenance, not proof** — the client asserts "the student saw
   and approved this", and the server cannot verify that a human read anything. It is validated as an ISO timestamp and
   stored; it is never treated as verification, and it must never be described as one.
2. **The invite gate is not identity verification.** `REGISTRATION_CODE` keeps signup closed to people we handed the
   code to; it does not prove who anyone is. **18+ is self-attestation** — the register form records
   `adult_attested_at` because the student ticked a box (`403 attest_required` if they do not), and nothing verifies it.
   There is no ID check and no verification of school affiliation anywhere.
3. **The demo's agents are stand-ins.** Beat 13 and the tests use scripted agents with a hard-coded budget rule. In
   production the searching and the choosing are done by each student's Cubbie following `SKILL.md`, which this repo
   cannot run; the server cannot verify that an agent applied its student's rules. Agents only hear each other when they
   poll (pull, not push), so an answer can take until the next check-in.
4. **Links are the free-form strings.** The activity link and the public-event link are validated (https, ASCII, ≤300 chars, real domain, no
   credentials, no fragment, no IP address) and never fetched by the server, but it is still a string a bad agent could
   choose. The Skill tells the agent to use only links it found in a public search.
5. **Event links are validated, not vetted.** A student's agent finds public event pages and the student approves the
   list, but the server only checks the link's shape and drops everything after `?`. It cannot tell a public page from a
   page with a personal code in its path, and it never opens the link. Two students match on an event only when their
   links are the same page after cleaning; a different URL for the same event will not match.
6. **`busy_load` is a rough number the agent chose.** It comes from Canvas's last sync and the calendar, 0 to 3, and the
   server cannot check it. The rule-based matcher ignores it, so it does nothing yet except wait for a ranking model.
7. **The demo runset is invite-only and small.** It was run on team devices, with fictional students and synthetic
   calendars. That is enough to show the loop closing; it is not a population, a load test, or a safety record.

## Documentation

- `docs/privacy.md` — what we collect, what we reject, how long we keep it (also served at `/privacy`).
- `docs/terms.md` — what this service is and is not (also served at `/terms`).
- `CONTRACT.md` §16 — the honesty ledger these documents are held to.
