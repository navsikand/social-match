# Social Match

**Your assistant does all the foreplay: it finds the free slot, proposes it, and waits for your explicit yes.**

Two students' AI assistants find where their free time and interests overlap, negotiate one real thing to do
(a climbing session, a coffee), and hand the humans a single decision: **Confirm**, or not. It is Track 2
(agent-to-agent coordination), and the whole product is built around one rule — *an agent may propose, but a
human is the only thing that can confirm.*

Nobody's calendar, courses, grades or transcript leaves their own account. What crosses the wire is a handful of
time ranges, a few interest tags, a consent block, and a server-templated note of four fixed fields.

---

## The loop

```
1. Tell your assistant when you're free     →  preview card  →  you press Confirm (the click is the provenance)
2. The matcher looks for overlap            →  one card per person, over the LONGEST overlap, 30 min minimum
3. Both assistants negotiate an activity    →  offer → accept / counter / pass(+reason code), 3 rounds max
4. Both humans swipe                         →  a confirmation is a signed-in press; nothing is "on" until both
5. A plan exists                             →  withdrawable at any time from the card
```

Steps 3 and 4 are the interesting part: the agents argue about *what* to do, and the humans keep the veto over
*whether*. A message never says "you're on" until the second side confirms — a test fails the build if that
word appears early.

## What we built

| Piece | Role |
| --- | --- |
| **The chat app** | the interface. Talk to your own assistant; anything touching availability or events comes back as a normalised **preview** you Confirm. |
| **Your assistant** | your agent. Reads your own state, writes through the server's validated path, and — only if you delegate — answers the *other* assistant's offers. |
| **The broker (this server)** | validates every write against an allowlist, decides who overlaps whom, runs the consent grants, feeds the model provider on your behalf, owns every retention clock. |
| **The card / matches pages** | the human half: swipe, counter, withdraw. No assistant can do these for you. |

## Quickstart

```bash
uv sync                                          # Python >= 3.11
cp .env.example .env                             # everything optional; see Configuration
uv run uvicorn server.app:app --reload           # http://localhost:8000
uv run pytest -q                                 # 544 tests, ~10 s
uv run python demo/scenario.py                   # 17 asserted beats over the real HTTP API
```

Open <http://localhost:8000/register>, use the invite code (`REGISTRATION_CODE`, default `socialmatch2026`), tick the
18+ attestation, and upload a photo — **a photo is required before anyone is matchable**, by design.

The demo prints an honest "what crossed the wire" summary and exits non-zero on any failed beat. To film against a
running server: `uv run python demo/scenario.py --against-url http://localhost:8000`.

**Two accounts to see the whole loop:** register two, open `/chat` on both, tell each one *"free weekends"*,
Confirm both preview cards, then open the Dashboard, swipe right on both sides, and watch the plan appear in
**Matches**.

## Configuration

Every value is read at call time (env vars, or `deploy/run.sh`'s `socialmatch.conf`).

| Variable | Default | What it does |
| --- | --- | --- |
| `AI_API_URL` | unset | Full POST URL of an **OpenAI chat-completions** endpoint. Unset → chat disabled (banner + `503 ai_unconfigured`). |
| `AI_API_KEY` | unset | Bearer key for that provider. Never sent to the browser. |
| `AI_MODEL` | `.env.example` ships one | Model name in the request body. |
| `AI_MAX_TOKENS` / `AI_TIMEOUT_SECONDS` / `AI_TTL_DAYS` | `800` / `60` / `7` | Reply cap, per-turn wall clock, transcript retention. |
| `MATCHING_DEFAULT_ON` | off | Demo-box only: signup seeds the `matching` + `publication·card` grants, because a chat turn can never grant anything. |
| `DELEGATION_DEFAULT_ON` | off | Demo-box only: signup seeds the **delegation** grant so assistants negotiate hands-free. Only Settings can change it afterwards. |
| `ENCODER_ENABLED` | `1` | Learned interest similarity; falls back to the hand-made taxonomy when it cannot load. |

The protocol is OpenAI chat-completions **only**. A non-OpenAI shape is a typed `ai_protocol` failure shown to the
student, never a silently empty reply.

### Interest similarity (optional)

`uv sync --extra encoder` installs a sentence encoder that scores interests **on this server**, not at a provider.
The extra pins the **CPU** torch build: the model is 33M parameters and the 34 tags are embedded once at startup, so
a GPU saves milliseconds and costs ~2.5 GB of CUDA wheels. It loads in a background thread into `./.cache` (129 MB);
first start downloads, later starts reuse it, and requests are never blocked on it.

A quality gate decides whether it is used at all: below `ENCODER_MIN_AUC` (0.80) the encoder turns itself off and the
taxonomy keeps scoring. Check it without starting the server:

```bash
uv run python -m server.encoder      # verified on our box: AUC 0.902, cpu, 520 probe pairs
```

## What crosses the wire — and what never does

The product's claim is a schema, not a promise: `server/schema.py` is an allowlist with `extra="forbid"` at every
level, and anything else is `422 payload_forbidden`.

| Shared, after consent | Never leaves the student |
| --- | --- |
| UTC time ranges (+ a tz for display) | Calendar event titles, attendees, locations |
| Interest tags in the student's own words | Course codes, professor names, grades |
| `wants` (`hangout` / `event`) and `busy_load` 0–3 | Photos (except to a matched pair, before either confirms) |
| A boolean consent block | The chat transcript |
| A server-templated note: decision, tag, link, reason code | Anything an assistant decided on its own |

**Interests, honestly:** the card shows the words the other student actually typed ("matcha"), while the matcher
scores the canonical tag ("coffee") so two different words stay comparable. Your raw words are only ever served to
the other member of the same match pair.

**`.ics` upload:** read once, in memory (2 MiB cap, streamed), reduced to intervals — nothing is spooled to disk.
Recurring events (`RRULE`/`RDATE`), `TZID`-qualified times and `PERIOD` values are **rejected with a reason**, never
silently converted into free time. Malformed fixtures (missing `DTEND`, unterminated `VEVENT`, unbalanced components)
are refused too: a broken calendar must never look like an empty one.

## The five consent grants

Everything the server may do with a student's data is one of five grants: versioned, recorded in `consent_log`,
re-checked at the moment they are relied on (never cached), and revocable from Settings.

| Grant | Covers |
| --- | --- |
| **matching** | Your windows and tags may generate candidates, and are re-checked at confirmation. Turning it off deletes your availability and voids open *and* confirmed plans — the UI warns and confirms. |
| **publication · card** | A card and its notification, to matched users only. |
| **publication · name** | Your display name on that card, to matched users only. |
| **reveal** | Pair-scoped messaging once both sides agree — *designed, not in the demo* (the grant row exists, no channel does). |
| **delegation** | "Let my assistant accept, counter or pass offers for me." Off by default, **only Settings can change it**, and the decision re-reads it inside the same transaction that would apply it. |

## Judge hooks

**Agent-to-agent, with a human veto.** The assistants negotiate the *activity*; the humans confirm the *plan*.

| Constraint | What must happen | Proven by |
| --- | --- | --- |
| Overlap pick | One card, over the **longest** overlap (tie → earliest start). | `test_matching.py::test_longest_overlap_wins_tie_earliest_start` |
| No overlap | Genuinely disjoint free time produces **no** card — "no time found" beats a bad suggestion. | `test_matching.py::test_sub_30_minute_overlap_produces_no_candidate` |
| Budget conflict | One assistant's private budget rules out the other's pick, so it counters with another shared interest and a **reason code**. No amount ever leaves the device. | `test_activity.py::test_pass_with_reason_then_counter_then_accept`, demo beat 13 |
| Round cap | After 3 offers the server stops; the students can still confirm the time. | `test_activity.py::test_round_cap_then_time_only_fallback` |
| Two takers | Confirming two plans in one window is `409 slot_conflict`; concurrent confirm/revoke races end revoked, never double-booked. | `test_concurrency.py::test_overlapping_slot_conflict` |
| Stale preview | An older preview cannot be confirmed: the confirm re-checks the state hash and applies nothing. | `test_chat.py` (optimistic-conflict path) |
| Withdrawal mid-turn | A withdrawn delegation is re-read inside the write transaction, so an in-flight decision changes nothing. | `test_chat.py::test_autonomous_core_refuses_when_grant_vanishes_after_authorization` |
| Provider reply cut off | Only a clean `[DONE]` completion may execute actions; a capped or truncated stream is text, never an instruction. | `test_ai_client.py`, `test_chat.py` |

**Rigor, if you want a second signal:** the UI/UX layer went through a five-lane audit (38 findings) and three
independent review gates that blocked on six defects the fixes themselves introduced — including a blank-form Save
that could have emptied a student's availability. The record is in `UX-AUDIT.md` (now historical: everything is
fixed). 544 tests and 17 demo beats are green.

## Repo layout

| Path | What it is |
| --- | --- |
| `server/schema.py` | **The allowlist for structured state** — windows, tags, wants, free_time, consent, and nothing else. |
| `server/statewrite.py` | The one validated write service. `mode="agent"` keeps grant-renew semantics; `mode="chat"` is **grant-preserving** and revalidates liveness + state version inside the transaction. |
| `server/chat.py` | Turn engine (SSE), action protocol, preview store, per-user lock, rate rows. |
| `server/ai.py` | OpenAI chat-completions client: SSE parsing, typed errors, credential redaction, byte cap. |
| `server/schedules.py` | Manual windows + bounded `.ics` → preview → confirm. |
| `server/matching.py`, `server/taxonomy.py`, `server/encoder.py` | Candidate generation; hand-made taxonomy; optional learned similarity. |
| `server/activity.py`, `server/offers.py`, `server/negotiate.py` | Assistant-to-assistant negotiation and the worker that drives it. |
| `server/scheduling.py` | Card, swipe, counter, revoke, leave-matching, delete-account. |
| `server/wording.py` | Message composition and the grant enforcement seam. |
| `server/sweep.py` | Expiry/purge sweep (read-time expiry is enforced in every query; the sweep only materialises it). |
| `server/webui/` | Templates + routes: chat, schedule, dashboard, card, matches, settings, register/login, privacy/terms. |
| `schema.sql` | Authoritative schema (`chat_messages`, `chat_turns`, `chat_pending`, grants, candidates, proposals…). |
| `tests/`, `demo/scenario.py` | 544 tests; 17 asserted beats over the real HTTP API. |
| `docs/` | The privacy policy and terms the server itself serves at `/privacy` and `/terms`. |

## Honest caveats

These are limits, stated plainly. `CONTRACT.md` §16 (the honesty ledger) is binding on this README and on `docs/`.

1. **The phone-side client is not shipped.** A third-party agent can still talk to this broker — same allowlisted
   payload, same pairing codes, same grants — but the calendar-reading client itself is out of scope for this repo.
   The demo posts `agent_computed` payloads against fixed windows. `student_confirmed_at` is **provenance, not
   proof**: it records that a client asserted "the student approved this" (or that you clicked Confirm), and the
   server cannot verify a human read anything.
2. **The invite gate is not identity verification**, and 18+ is self-attestation — the register form records
   `adult_attested_at` because a box was ticked. No ID check, no school affiliation, anywhere.
3. **The assistant is a model.** It can be wrong, including about your own state — which is why the chips and the
   Settings page are the truth and every write is revalidated server-side. The negotiation worker is bounded (one op
   per turn, 3 rounds, a 12-turns-per-10-min autonomy ceiling inside the shared 20/10 budget, two failed attempts
   per offer before it hands the student a manual Accept/Counter/Pass card), but it is still a model deciding.
4. **Links are validated, not vetted.** Activity and event links are checked for shape (https, ASCII, ≤300 chars, real
   domain, no credentials, no fragment, no IP) and never fetched by the server. It cannot tell a public event page
   from one with a personal code in its path. The chat assistant has **no web search**, so a link it invents is a
   string it made up — the honest fix is a vetted link list, and that is not built.
5. **`busy_load` is unverifiable.** It is what you tell the assistant, 0–3. The rule-based matcher ignores it; it waits
   for a ranking model.
6. **The similarity encoder is borrowed, and checked against our own judgments.** A pass means "agrees with us on our
   probe pairs", not that it understands every student's interests. It ranks and explains; it cannot override
   consent, blocks, or the time overlap.
7. **The model provider is a processor we do not control.** A chat turn sends your own state, the last 40 transcript
   messages and your text to whatever `AI_API_URL` names. It may log, retain or train on that. Don't type secrets.
8. **The demo runset is small and invite-only**, run on team devices with fictional students and synthetic calendars.
   It shows the loop closing; it is not a population study or a safety record.
9. **Typed hard constraints are not built.** Budget and distance exist as an *agent's private rule* in the
   negotiation story, not as a server-enforced filter. ML ranks; it never overrides.

## Documentation

- `docs/privacy.md` — what we collect, what we reject, how long we keep it (served at `/privacy`).
- `docs/terms.md` — what this service is and is not (served at `/terms`).
- `CONTRACT.md` §16 — the honesty ledger the above are held to.
- `UX-AUDIT.md` — the UI/UX findings, now historical (all fixed, gated three times).
- `FLOW.md`, `PLAN.md`, `CONTRACT-chat.md` — design history; they describe the wire contract, not shipped client code.

---

Track 2 entry (agent-to-agent coordination). Built by Aradhya Dixit and Navraj Sikand.
