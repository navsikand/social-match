# Track 2 plan — Study Match

> **Read `FLOW.md` first — it is newer.** The identity-release rule below (identity only after mutual confirmation) is
> correct for the anonymous study-match protocol and **wrong for the discovery card flow**, where a card is shown
> before anyone confirms. `FLOW.md` replaces it with four grants, and separates *publishing* a name from *disclosing*
> one. Pairing, tokens and `/inbox` here remain correct.

**Agent-to-Agent Coordination.** Personal agents for different students that negotiate a study session, through a
server that knows who everyone is and tells nobody.

> Gate: Track 2 only opens once the team's Track 1 entry is submitted — the database refuses otherwise. This plan is
> written to be buildable in a few hours after that lands.

## The scenario

It's Tuesday. Ava has a stats midterm Thursday and is shaky on it. Ben is solid at stats and has a free evening. They
sit two rows apart and have never spoken. The information needed to connect them exists — in their heads, in their
calendars, in their grades — and no system on earth will surface it.

The shared outcome: **two agents find a time both students can actually make, and neither student has to admit
anything publicly or be ranked against anyone.**

## The one design rule

**An agent may only make claims about its own student.**

| Pattern | Example |
| --- | --- |
| ✅ Agent sends | "My student is comfortable with statistics and is free Thursday evening." |
| ❌ Agent sends | "My student got 54% on the stats midterm." |
| ❌ Agent asks | "Who in BIO 201 has a good grade?" |

Grades are the *input* to a private local judgement — should I offer help or ask for it? — never the payload. And the
downgrade happens **inside the student's own the phone agent**: Canvas grades never reach our server at all. That's not a policy
we have to defend, it's an architectural fact anyone can check — `grep -r "grade" server/` returns nothing.

## Two constraints that shape everything

the phone agent is the sensor and the mouth: it has the Canvas and Google Calendar integrations we are not going to rebuild.
That decision costs us two things.

### 1. Nothing can push into the phone agent

No webhook, no inbound port, no "the server wakes the agent". A the phone-agent Skill runs only when **the student texts** or
**a scheduled check-in fires**. So this is **pull, not push**, and the check-in cadence is the event loop.

| Path | Latency |
| --- | --- |
| Student sends anything that triggers the Skill | instant — and the Skill polls `/inbox` on the way past |
| Scheduled check-in (twice a day is sensible) | minutes to hours, per the phone agent's scheduler and notification settings |

Two consequences you must design around:

- **Every invocation of this Skill is a poll opportunity** — and only the invocations that actually fire. The kit does
  not guarantee that an unrelated message invokes this Skill, so the heartbeat cannot rely on that: it relies on an
  explicitly scheduled, self-contained check-in, and on polling `/inbox` at the start of whatever invocation does
  happen. Delivery latency is not guaranteed either; say so rather than implying realtime.
- **Never promise realtime.** "Ava asks Tuesday, Ben's phone buzzes Wednesday morning, both confirm" is honest and
  still a good demo.

### 2. the phone agent tells a Skill nothing about who the student is

No user id, no email, no stable handle — just a sandbox and a conversation. The mapping **cannot be inferred, only
established once**. That's the auth section below, and it is the load-bearing piece.

## Architecture

```text
   the phone agent (Ava)                      OUR SERVER                     the phone agent (Ben)
┌───────────────────┐                                        ┌───────────────────┐
│ Skill: study-match │   HTTPS 443, one host, no inbound      │ Skill: study-match │
│  scripts/bus.py    │ ─────────────►┌──────────────────┐◄───────────────        │
│  auth.json (600)   │               │ /pair   /inbox   │                        │
└───────────────────┘               │ /requests /offers│                        │
                                    │ /confirm /revoke │                        │
 Canvas + Calendar                  │ matcher, consent │      Canvas + Calendar │
 (the phone agent does this)                 │ ledger, audit    │      (the phone agent does this)│
                                    └──────────────────┘
```

Hub-and-spoke, not peer-to-peer. Three things fall out for free:

- **Clients never see each other's data.** Only the broker holds the join, so a match is negotiated over opaque ids
  and Ava never learns Ben's email address.
- **Matching criteria live server-side.** Change the matcher without shipping a new Skill version, and without
  trusting a client to run it.
- **Consent is enforceable in one place.** Every write endpoint refuses without a recorded grant; revoking kills a
  pending match once, everywhere.

## Auth mapping

One mechanism: a **pairing code exchanged once for a token bound to an agent id.**

### The token

```python
import secrets
token = secrets.token_urlsafe(24)   # exactly 32 chars, 192 bits
```

Store only `sha256(token)`. The plaintext exists in two places: this response, and the student's sandbox. Never in a
URL — URLs land in access logs, and that's how this design usually dies.

### Pairing

| Step | Link-first (default) | Typed code (fallback) |
| --- | --- | --- |
| Skill runs `pair.py --start` | server returns `{code, url, expires_in}` | same |
| the phone agent texts | the link | — |
| Student | taps it, already signed in on their phone, taps **Approve** | reads the code on our website, types it to the phone agent |
| Skill runs `pair.py --finish --code …` | code → token | same |
| Secret typed by a human? | never | the code, 5-minute TTL, single use |

Use link-first. It costs one Approve button, removes every copy-paste failure, and the approval rides on the session we
already trust. Keep the typed code for the student who's at a laptop.

The code: 8 chars of Crockford base32 (no `I L O U`), shown as `AB12-CD34`, hashed at rest, single use. It's a claim
ticket, not a credential.

### The mapping, in four columns

```sql
users          (id, email, created_at)                       -- our site, unchanged
pairings       (id, user_id, agent_id, token_hash, label,
                created_at, last_seen_at, revoked_at)
pairing_codes  (code_hash, user_id, kind, expires_at, used_at)  -- kind: 'link' | 'typed'
```

- `agent_id` — minted at pairing, returned beside the token. Distinguishes *this* the phone agent from a reinstall, and lets
  one student hold a phone pairing and a laptop pairing separately.
- `last_seen_at` — bumped on every `/inbox` poll. Our liveness signal, and the health metric for the demo.
- `revoked_at` — set, never deleted, so the audit trail survives.

Every request reduces to:

```text
Authorization: Bearer <32 chars>
   → sha256 → pairings WHERE token_hash = ? AND revoked_at IS NULL
   → user_id, agent_id
```

### Endpoints

| Method | Path | Auth | Returns |
| --- | --- | --- | --- |
| `POST` | `/pair/start` | none | `{code, url, expires_in}` |
| `POST` | `/pair/approve` | site session | `{ok}` — binds the code to the signed-in user |
| `POST` | `/pair/finish` | none | `{token, agent_id}` |
| `GET` | `/inbox` | token | pending offers, matches, revocations; bumps `last_seen_at` |
| `POST` | `/requests` `/offers` `/confirm` `/revoke` | token | protocol writes |
| `POST` | `/pair/rotate` | token | new token, old valid 60s for grace |
| `POST` | `/pair/forget` | token | revoke this agent, delete pending rows |
| `GET`/`DELETE` | `/pairings` | site session | list and kill bindings from the web UI |

`/pair/finish` must be one transaction, or two agents racing a code both win:

```sql
UPDATE pairing_codes SET used_at = now()
 WHERE code_hash = $1 AND used_at IS NULL AND expires_at > now()
 RETURNING user_id;
```

Zero rows means bad, expired or already-used. Never read-then-write.

### What the Skill keeps

`$SKILL_DATA_DIR/auth.json`, mode `600`:

```json
{
  "agent_id": "…",
  "token": "…",
  "paired_at": "2026-10-24T09:12:00Z",
  "server": "https://study-match.example.com"
}
```

HOME persists across restarts and reinstalls: pair once, never again.

### Failure modes we handle

| Failure | Handling |
| --- | --- |
| Revoked token still used | `401` with a body the Skill turns into one line: "your link to Study Match expired — reconnect?" Never retry silently |
| Two the phone agents, one user | Allowed, told apart by `agent_id`, capped at 3 so a stuck reinstall can't leak pairings |
| Code guessed in its TTL | 40 bits, 5-minute TTL, single use, rate-limited `/pair/finish`. Forwarding is the real risk, not guessing |
| Token leaked into a log | Only ever in the header; never in a query string, never in a message body |

### Why conversational consent is enough

The student says "share X", the phone agent shares X. The server cannot verify that utterance, so don't build as if it can. The
real protection is **schema shape** — accept only a fixed, narrow payload:

```json
{"subject_area": "statistics", "band": "shaky", "windows": ["2026-10-29T19:00/21:00"]}
```

No free-text field, no arbitrary blob. Even a misbehaving client can then only lie about its own band — which hurts
only the liar — instead of pushing a transcript, a grade or an email address into our database. That is a much better
guarantee than a checkbox, and it's one line of Pydantic.

**Never key identity on:** a phone number (the phone agent doesn't expose it), an email typed in chat (spoofable, and it lands in
the transcript), or anything ambient (there is nothing ambient).

## Protocol

Four messages, JSON over HTTPS polling.

```jsonc
// 1. A student's agent announces a need. Identity is an opaque token, not a name.
{"type": "request",
 "request_id": "r-8f21",
 "subject_area": "statistics",                   // coarse: never a course code or professor
 "need": "shaky",                                // shaky | stuck | curious
 "windows": ["2026-10-29T19:00/21:00", "2026-10-30T18:00/20:00"],
 "expires": "2026-10-29T12:00",
 "from": "tok-4c1a"}                             // opaque; unlinkable to a person by the receiver

// 2. Another agent offers help, with its own student's consent already recorded locally.
{"type": "offer",
 "request_id": "r-8f21",
 "offer_id": "o-77bd",
 "band": "comfortable",                          // the band, never the score
 "windows": ["2026-10-29T19:00/21:00"],          // intersection with the request's windows
 "from": "tok-9ee0"}

// 3. Mutual confirmation, one per side. A match exists only when both land.
{"type": "confirm", "match_id": "m-2b6c", "window": "2026-10-29T19:00/21:00", "from": "tok-4c1a"}

// 4. Withdrawal, valid at any point up to the session.
{"type": "revoke", "match_id": "m-2b6c", "from": "tok-9ee0", "reason": "student changed plans"}
```

Two guarantees, both cheap and both demoable:

- **Identity releases only on mutual confirm.** Until both sides confirm, each agent holds an opaque id. Ava never
  learns Ben declined; Ben never learns who asked until he's in.
- **A revoke is always accepted and always cancels.** No negotiation, no "are you sure" from the other side.

## Consent state machine

Each agent holds one state per counterpart, and every outbound message asserts a local grant. The agent refuses to
send anything it has no grant for — that refusal is a feature, and it should be visible on screen in the demo.

```text
IDLE
 └─ student says "find me help with stats" ............ ASKING        (outbound: request)
      ├─ student says "never mind" ..................... IDLE          (outbound: revoke)
      └─ inbound offer arrives (next /inbox poll)
           → agent asks its student "Ben offered Thursday 7-9. OK?"
                ├─ yes ................................ OFFERING     (outbound: offer / confirm)
                └─ no ................................. IDLE         (outbound: decline)

OFFERING
 └─ both confirms recorded ............................. MATCHED
      ├─ either student says stop ...................... REVOKED     (outbound: revoke, both notified)
      └─ window passes ................................. CLOSED

Any state ── student asks to be forgotten ──► WIPED (local profile, auth.json, all tokens unlinked)
```

The demo moment is the middle: **the agent pausing to ask**, with the request quoted back. That's the thing judges are
told to look for.

## Conflicting constraints — where the marks are

1. **No overlap.** Ava is free Thursday, Ben Friday. Agents counter-propose from the other side's list once, then give
   up cleanly: *"No time that works this week — want me to keep the request open?"*
2. **Same slot, two takers.** Ben and Cleo both offer Thursday. Ava's agent presents both with bands only; she picks;
   the other is released **with a reason**. No silent ghosting.
3. **The chain.** Ben is comfortable with stats but shaky at organic chemistry; Ava is the reverse. Two matches,
   opposite directions. The closing shot: *nobody tutors, everybody improves, no grades involved.*

## What we build

Small on purpose. "Any messaging app, any open-source framework" — so pick boring, reliable things.

```text
study-match/
├── server/
│   ├── app.py            FastAPI: pair, inbox, protocol writes
│   ├── auth.py           token hash lookup, revocation, rate limits
│   ├── match.py          the criteria, server-side and swappable
│   └── schema.py         the narrow Pydantic payloads (no free text)
├── skill/                the phone-agent Skill, installed per student
│   ├── SKILL.md
│   ├── phone agent.yaml       network.runtime + the login command
│   └── scripts/
│       ├── pair.py       --start / --finish / --forget
│       └── bus.py        --poll / --request / --offer / --confirm / --revoke
└── demo/
    └── scenario.py       the three constraint cases as a repeatable script
```

**Stack:** FastAPI + SQLite, deployed on any HTTPS host on 443. The Skill folder is four files, all standard library.

**Deliberate cuts:** no room booking, no calendar writes, no LLM in the loop. The interesting thing is the negotiation
and the consent; every hour on bookings is an hour not spent making case 2 and 3 work.

**the phone agent specifics that matter:** declare the host in `phone agent.yaml` under `network.runtime` (HTTPS 443, hostname not
IP). The Skill has the phone agent read Canvas and the calendar itself and pass only bands and windows to `bus.py` — scripts
cannot call the phone agent's abilities, so the data has to arrive as arguments or a file.

## Demo video (~100 seconds)

| Time | Shot |
| --- | --- |
| 0:00 | Ava texts her agent: "i'm gonna fail stats" — agent asks which evenings work, then sends the request |
| 0:15 | The pairing beat, sped up: she taps a link, Approve, "connected" |
| 0:25 | Ben's phone buzzes *the next morning*: *"Someone's shaky on stats and free Thursday 7-9. Want to help?"* → yes |
| 0:45 | Ava's phone: *"Ben's comfortable with stats and free Thursday 7-9. Book it?"* → she confirms |
| 1:00 | Both get the same confirmation with the same time. Two phones, one match |
| 1:10 | The conflict case: two offers, one slot, one released politely |
| 1:25 | The chain: two matches running in opposite directions |
| 1:35 | Over black: *"No grades were shared. Each student only ever described themselves."* |

The "next morning" card is deliberate: it's honest about the heartbeat, and saying it out loud makes the design look
considered rather than slow.

## Judging hooks, mapped

| What Track 2 asks for | Where it lives |
| --- | --- |
| A working prototype tied to a real campus scenario | Two students, one midterm, one free evening |
| How agents handle conflicting constraints | The three scripted cases, shown not claimed |
| What they share | Bands and windows; never scores, never course codes |
| When they ask their students to confirm | The pause, visible on both phones, twice per match |
| A clear reason students would say "We need this" | "i'm gonna fail stats" is a text every student has sent |

## Risks

- **Consent theatre.** A checkbox at signup isn't consent. The demo must show a *fresh* ask per offer and a revoke
  working mid-flight. Skip revocation and a judge will ask a question with no good answer.
- **The server is the trusted party.** We hold the only table that joins "who" to "what they're struggling with". Keep
  it defensible: bands and windows only, no grades or course codes, purge closed requests, append-only audit log, no
  human browsing per-user rows, one-tap forget. Say this on stage before it's asked.
- **The same-course trap.** Never let two students in the same section see each other's bands for that course — that's
  a leaderboard for people who sit together. Scope to subject *areas* and prefer cross-section matches.
- **Latency honesty.** If the pitch implies realtime, the demo's own "next morning" undermines it. Frame the heartbeat
  as a feature: nobody's phone is a firehose.
- **Scope creep.** Group sessions, bookings, three-way matches: all fine for the "next" slide, none of them demoed.
- **Track 1 first.** Nothing here is submittable until the Track 1 entry is in.

## Order of work

1. `server/` with `/pair/start|approve|finish` and `GET /inbox`; two terminals pair and poll (about an hour).
2. `auth.py` right: hashed tokens, revocation, `RETURNING` single-use codes, rate limit.
3. `bus.py` + `match.py`: case 1 end-to-end through the server.
4. Cases 2 and 3 scripted in `demo/scenario.py`.
5. Skill installed on two real the phone agents; check-ins set twice a day; film.
6. Submit with the repo. A website only if there's time to spare.
