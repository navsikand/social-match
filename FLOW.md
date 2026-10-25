# Social Match — end-to-end flow

Track 2 entry: two students' assistants negotiate one real plan, through a server that knows who everyone is and tells
nobody anything they haven't published.

The chat app in this repo is the interface: the student signs in, talks to their own assistant, and keeps the card, the
swipe and the confirmation on the same site, because those need a signed-in session. The Cubbie skill is the
**legacy / alternate client** — it reads the calendar on the phone and POSTs the same redacted payload, and its
endpoints and pairing flow are unchanged. Where the two paths differ, this document says so.

Where this document and `PLAN.md` disagree, this one wins: it is newer. `PLAN.md` remains correct on pairing, tokens
and `/inbox`. `PLAN-chat-frontend.md` (approved) wins over this file for the chat app: §1c, §2 (delegation), §7 (chat
writes), §10 (chat tables) and §12 (new demo beats) are reconciled to it.

## One open question, stated up front

Whether a Cubbie Skill may send **calendar-derived** availability to a host that is not on Cubbie's sub-processor list.
Cubbie's Privacy Policy restricts transfers to its listed sub-processors, and our server is not one of them. This is the
**legacy skill path only**: the chat app takes windows you type, or an `.ics` you upload yourself, and neither involves
Cubbie or a Cubbie-side transfer.

Cubbie computes free time locally, reduces it to ranges, and reads those back to the student before anything leaves.
That reduces what is exposed and puts the student in control of the decision. It does **not** change who performs the
transfer, and it is **not** an exception to the policy — nothing in the Terms or the Privacy Policy says a student's
instruction overrides the sub-processor restriction. The gate stands: **ask the organizers on Discord before shipping
calendar-derived availability.** Until then the demo runs on synthetic fixtures.

---

## 1. The flow — legacy client path

```text
 ┌─ 0. PRECONDITION — every invocation, not once in a linear script ─────────────┐
 │  Paired? token valid? If not: pair (PLAN.md › Auth mapping).                  │
 │  Nothing can wake a student's Cubbie from outside. Everything below is pull.  │
 └──────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
 1. Trigger       A scheduled check-in (self-contained, explicit about polling),
        │          or the student texts first.
        ▼
 2. /inbox        Idempotent status: "1 up for Thursday", "Ava confirmed".
        │          Never a decision that already happened; never a name the sender
        │          hasn't published to matched users.
        ▼
 3. State check   GET /me → expired rows are treated as void and swept. Window is
        │          STATE_TTL_DAYS (default 7).
        ▼
 4. Collect       Cubbie (the legacy client) works out the free time itself: Canvas deadlines, class
        │          schedule, and the free-time script it already ships. Then it
        │          reads the result back before anything leaves — "you're free
        │          Thu 7-9 and Sat 6-11, and I've got climbing + live music.
        │          Share those?" Only ranges and tags leave: never a title, an
        │          attendee, or a course code. The server rejects anything else.
        ▼
 5. Store         One availability row per user, a content hash for change
        │          detection, observed_at, expires_at. No version history.
        ▼
 6. Match         Deterministic score, reasons logged, no popularity term; no
        │          candidate that is expired, blocked, or already swiped.
        ▼
 7. Filter        Intersect live windows only. Expiry re-checked at read time.
        ▼
 8. Propose       A versioned time proposal is created. Nothing is "scheduled" yet.
        ▼
 9. Notify        /inbox, with the name the other student chose to publish:
        │            "Ava's up for Thursday 7-9 — here's who"
        │            https://social.example.com/m/2b6c
        │          With only the card tier granted it says "climber-7f2a" instead.
        ▼
10. Discover      The link opens the card on our site, signed in. The card carries
        │          what the publication grants allow. Every swipe POSTs to the
        │          server, which queues the update into the *other* student's
        │          /inbox for their next poll.
        ▼
11. Confirm       Both confirm the same proposal version, atomically revalidated.
                   Whatever was published is already on both cards, before anyone
                   confirmed. Reveal (a contact channel) is designed but not in the
                   demo: the demo ends at both_confirmed, with a confirmed time.
```

## 1b. The loop between the two clocks (legacy client)

The link in step 9 is not decoration — it is how a decision gets **back into Cubbie**, which cannot be called from
outside.

```text
our server ──► student's Cubbie ──► text with name + link ──► tap ──► our website (signed in)
     ▲                                                                      │
     │◄──── POST /swipes {candidate_id, proposal_version, direction} ─────────┘
     │
     └──► queued into the OTHER student's /inbox ──► their next Cubbie poll
```

The website never pushes to Cubbie. It writes a row; the other agent reads it when it next polls. That is the only
direction the integration can run, and it is why step 11 is a state machine rather than a message.

**A right swipe is a version-bound confirmation, not a separate thing.** It carries the `proposal_version` the card was
rendered from, and the server treats it as that student's confirmation of exactly that version, under the same locks and
the same revalidation as any other confirmation. A swipe against a stale version is rejected and the page refreshes:
interest in a proposal that no longer exists is not consent to the one that replaced it.

**The loop closes at the speed of the slower poll.** A swipe is visible to the other web session at once, and to their
phone on their next check-in. Say that out loud rather than implying realtime.

---

## 1c. The chat path, which is the default now

The same server, with the student talking to their own assistant instead of to Cubbie:

```text
1. Sign in     /chat. No pairing, no phone. Disabled banner if the deployment has no AI_API_URL.
2. Talk        "I'm free Thu 7-9 and Sat after 6, and I'm into climbing and matcha."
               -> the assistant streams a reply and ends with ONE json action block, last.
3. Preview     set_windows / set_events do NOT write: the server normalises them (<=8 h each, <=20 total,
               horizon-checked) and stores ONE pending preview. The UI shows it with Confirm / Cancel.
4. Confirm     the click is the provenance. statewrite(mode="chat") writes the payload once; the chips
               re-render and the transcript gets a server-templated note.
5. Immediate   set_interests / set_wants / set_busy_load apply at once when they are the whole batch.
6. Schedule    or skip the chat: /schedule has a manual form and a bounded .ics upload -> preview with
               checkboxes -> confirm. Only intervals are ever stored; the file is read once in memory.
7. Match       unchanged: §1 step 6 onward. The card, the swipe and the confirmation are still the website.
8. Negotiate   if both students delegate, their assistants settle the activity against each other (§2,
               delegation) and each is told "found a match — go swipe". Swiping and confirming stay human.
```

Two invariants hold on every one of these paths. Chat-mode writes **never** touch a grant — the grant-preserving
`statewrite(mode="chat")` exists for exactly that. And every apply re-checks the account's liveness and the state hash
captured when the system prompt was built: a stale snapshot is a typed `conflict` with nothing written, and the
transcript says so.

---

## 2. Consent: five grants, and one photo publication

Server-enforced, recorded in `consent_log`, revocable, and re-checked at the moment each one is relied on.

| Grant | Asks | Grants | Checked when | Withdrawal means |
| --- | --- | --- | --- | --- |
| **Matching** | "Look for people who want to do what you want, this week?" | Your windows and hobby tags, used to generate candidates | Candidate generation, and again at confirmation | Stop generating; in-flight proposals are voided |
| **Publication · card** | "Show a card to people you match with?" | A pseudonym, a preset avatar, your hobby tags | Serving a card, and again at confirmation | Card stops being served; served cards withdrawn |
| **Publication · name** | "Put your first name on that card and in the text, or stay a pseudonym?" | Display name, to **matched users only** — never public, never searchable | Serving a card, **and composing the notification** | Name reverts to the pseudonym on future cards and texts; texts already sent are not recallable |
| **Reveal** | "Open a way to message this person, once you both say yes?" — **designed, not in the demo** | A contact channel — a pair-scoped thread — **not** the name | At the reveal step, for that pair only | Channel closed; the match keeps whatever was already published |
| **Delegation** | "Let my assistant accept, counter or pass offers for me?" — **off by default** | Accept/counter/pass on an activity offer for a match you already hold — never a swipe, never a plan confirmation | At each autonomous negotiation decision, inside the write transaction | Stop deciding; in-flight decisions are dropped, nothing applied; matches, cards and plans are untouched |

**Delegation is the fifth grant, and its only runtime writer is the Settings page.** `kind='delegation'` is **off by
default**: a real deployment gets it only when the student flips the toggle in Settings. No chat operation
can create, renew, restore or withdraw it — no such op exists in the action protocol, and `statewrite(mode="chat")`
firewalls grants exactly as it firewalls matching and publication. The demo box seeds it at registration via
`DELEGATION_DEFAULT_ON=1` (version 1 plus a `consent_log` row in the registration transaction) so the negotiation
segment runs hands-free. Withdrawal is a Settings act and it takes effect at the decision itself: the apply transaction
re-reads `withdrawn_at IS NULL AND expires_at > now`, so a withdrawal racing a streaming turn drops the op and writes an
honest note. It never covers swiping or confirming a plan.

```jsonc
// publication_grant — the version that was approved is the version that is served
{
  "tier": "card",                                               // "card" | "card+name"
  "fields": ["pseudonym", "preset_avatar_id", "hobby_tags"],    // enumerated, no wildcard
  "name_field": null,                                           // "display_name" only in card+name
  "audience": "matched_users_only",                             // no "everyone" value exists
  "granted_at": "2026-10-24T09:12:00Z",
  "expires_at": "2026-11-24T09:12:00Z",
  "version": 3
}
```

**Publishing a photo is a sixth consent act, and deliberately not a sixth row.** The upload control on the student's
own settings page *is* the consent, and it is bound to one revision of one image rather than to a `grants` tier:

- absent photo = off; nothing is published implicitly;
- a replacement image is a **new** publication decision, because it is a different picture about to be shown to people
  they have not met — `photo_revision` increments and the old filename stops being addressable;
- it is **never implied by `card` or `card+name`**, and it never displaces them (the publication upsert withdraws the
  previous publication row, so photo permission is stored on `users`, not in `grants`);
- it is **web-only**: only the website can show the preview and the audience sentence that make the consent informed, so
  `POST /api/state` **neither grants nor withdraws** it, and a state resend **preserves** whatever is currently
  published;
- removal clears `photo_published_at` first and unlinks the file after, so access dies even if the unlink fails.

**The name is a publication choice, not a prize for confirming.** A student who grants `card+name` gets "Ava's up for
Thursday" in the text and "Ava" on the card, before anyone confirms anything — which is where a name is actually useful.
A student who grants only `card` is `climber-7f2a` in both places.

**The authorization rule, precisely.** A card, a message about student X, or X's photo is served to student Y only when
all of these hold at the moment of use — never cached:

| Requirement | Whose grants | When |
| --- | --- | --- |
| matching grant live | X **and** Y | Always: no matching, no card |
| publication · card live | **X** | Always: this is what makes a card exist at all |
| publication · name live | **X** | **Only when the card or the text contains X's name** |
| not blocked, either direction | — | Always |
| availability not expired | X and Y | Always |
| photo published (`photo_published_at` non-null, file present) | **X** | **Only when serving X's photo.** Applies to the owner's own preview too |

A photo fetch that fails any of these — including the owner's own preview after they remove it — is **`404`**, never
`403`, so the route cannot be used to probe who has a photo or a card.

So the pseudonymous path needs two grants and the named path needs three. Delegation is orthogonal to that count: it
gates negotiation, never what a card shows. A missing name grant never blocks a card — it
demotes it to the pseudonym. If X edits their card, `version` increments and the old grant stops qualifying: that is a
re-approval, not a silent republication.

**An 18+ self-attestation is not age verification.** Say so rather than implying otherwise.

---

## 3. Retention and deletion: four operations

| Operation | Student says | Deletes | Keeps |
| --- | --- | --- | --- |
| **Revoke agent** | "disconnect this Cubbie" | this pairing's token; availability rows this agent wrote | other pairings; the account and its grants |
| **Leave matching** | "stop matching me" | matching grant; live availability; candidates for and against them; open proposals; served cards and any existing match's discovery state | account and publication grants, inert without matching |
| **Clear chat** | "clear my history" | your chat transcript, and any pending preview | the rate-limit rows — clearing history is a user right, not a way to buy a fresh turn allowance |
| **Delete account** | "delete everything" | account row, profile, card, all grants, availability, windows, tags, swipes, candidates, matches, proposals, pairings, the chat transcript, pending previews, chat turn rows, and every `consent_log` row whose payload is personal | a signed-deletion record; pseudonymous `blocks`; audit rows stripped to `{hash, action, timestamp}` |

Three details that have to be true rather than claimed:

- **`consent_log` is append-only for writes, not exempt from erasure.** Deleting an account strips personal payloads and
  keeps only a hashed id, an action and a timestamp, for abuse defence, expiring after 90 days. That residue is
  disclosed in our Privacy Policy with its purpose and its clock.
- **`blocks` outlive the account that made them.** "Permanent" means permanent *for the pair while the block exists*;
  the row itself is retained 12 months and holds two pseudonymous ids and nothing else.
- **Backups.** Deleted rows persist in encrypted backups until they age out, about 14 days. Say that, rather than
  letting "we delete it" imply otherwise.

### Ordinary retention, so §3 is an inventory and not a boast

| Data | Kept | Why |
| --- | --- | --- |
| Availability and windows | `STATE_TTL_DAYS` (7) from `observed_at` | Matching only |
| Hobby tags | While the account is live | The profile the student built |
| Match reasons and scores | 30 days after the candidate closes | Debugging and the fairness check |
| Swipes | 30 days | Prevents re-showing someone you passed on |
| Closed matches | 30 days | Enough to dispute or report |
| `consent_log` | While the account is live; 90 days stripped on deletion | Proving what was agreed, then just that something was |
| Chat transcript | `AI_TTL_DAYS` (7) from each message, then purged | The assistant needs context; a week is enough to read back |
| Pending preview | 10 minutes, or until confirmed, cancelled or superseded | A proposal you have not agreed to is not data worth keeping |
| Chat turn rows | Longer than the 10-minute rate window they serve; timestamps and a kind, no message text | Rate limiting that transcript deletion cannot reset |

---

## 4. Expiry that holds when nobody logs in

- **Read-time voiding.** Expiry happens the instant the clock passes, and no transaction runs at that instant. So every
  read treats an expired `availability` row and everything derived from it as void immediately, whether or not a sweep
  has run.
- **Cron materialises it.** An hourly server job deletes expired rows and stamps derived candidates and proposals
  `voided`. A server cron is fine; it just cannot wake a Cubbie. The two facts are different.
- **Explicit deletion voids in the same transaction** that deletes the availability row.
- **The promise is testable:** "we never use, serve or match data older than `STATE_TTL_DAYS`, and delete it no later
  than the next sweep." Not "we delete at exactly 7 days", which nothing can guarantee.

---

## 5. Scheduling: proposals, versions, atomic confirmation

Both students have two clocks — an iMessage thread and a signed-in web session — so "scheduled" is a state transition,
not a message.

```text
proposals (id, candidate_id, window_start, window_end, version, state, expires_at)
states: proposed → a_confirmed → both_confirmed → closed
                        └→ voided (superseded | expired | revoked | grant withdrawn)
```

- Every proposal carries a **version**. A counter-offer creates version + 1 and voids the previous one.
- **`/inbox` is a status update, never an authority.** It names the other student only if they granted `card+name`.
- **No wording ahead of the state.** The text never says "you're on" before `both_confirmed`, and never names someone
  who granted only `card`.

### The concurrency contract

**Lock order is fixed and deterministic:** both `user_id` rows in **ascending id order**, then the proposal row. Both
sides of a pair therefore take the same locks in the same order, which is what prevents a reversed-pair deadlock.

**Revocation precedence is a guarantee about the end state, not about who grabs the lock first.** A lock decides who
goes first, not who wins — so don't claim otherwise. The enforceable rule is a revocation intent:

- `revoke()` writes a `revoke_intents(user_id, created_at)` row as its **first statement, before taking any lock**;
- every confirmation, inside its lock, refuses if either user has an intent newer than the proposal;
- a confirmation that serialised *before* the intent row commits normally, and the revoke then voids the confirmed match
  in the same transaction that records the intent, notifying both sides.

Whichever order the two operations interleave, **the state after both complete is `revoked`, never confirmed**. A brief
window where a session renders a match that is already being voided is acceptable; a match that survives a revocation
is not.

Rules that fall out of the same locks:

- a confirmed slot may not overlap another confirmed slot for either user;
- each user's confirmation is idempotent via `UNIQUE(proposal_id, user_id)`, so a double-tap is a no-op;
- replacing availability re-checks every open proposal that depends on it and voids any whose window is no longer
  covered.

Tests: counter-offer vs confirm, revoke vs confirm in both interleavings, duplicate confirm, two users racing one slot,
and a reversed-pair deadlock attempt.

---

## 6. The payload contract

A trusted Skill is not a security boundary, so the claim is about what the server **accepts**:

```jsonc
{
  "windows": [{"start": "2026-10-29T19:00", "end": "2026-10-29T21:00", "tz": "America/Los_Angeles"}],
  "tags": ["climbing", "live_music", "matcha"],  // free text, max 8; matcher canonicalises
  "wants": ["hangout", "event"],
  "free_time": {"source": "cubbie_computed", "student_confirmed_at": "2026-10-24T09:10:00Z"},
  "consent": {"matching": true, "publication": {"tier": "card+name", "version": 3}}
}
```

- Unknown keys are **rejected with 422**, not ignored — a Skill that tries to send more fails loudly in testing.
- `tags` are free text in the student's own words, and they are the only free-text field *in structured state*: at most
  8, at most 32 characters each, no control characters or line breaks. The server canonicalises each one onto the closed
  vocabulary and rejects anything outside those bounds; the raw words never reach another student.
- Windows are bounded: at most 20 per payload, ≤8 hours each, within 30 days, with an explicit timezone.
- There is **no delegation field** in this payload. `consent` carries matching and publication only; delegation is
  written by the Settings page alone and `statewrite(mode="chat")` never creates or renews it.
- `student_confirmed_at` is a **client-supplied acknowledgement, not proof.** It is recorded in `consent_log` bound to
  the accepted payload hash and the pairing, so we can show *what the agent said the student agreed to*. It does not
  verify that a human read it — the server cannot verify an utterance, as `PLAN.md` says. Provenance, not verification.
- **Serialization test, part of the deliverable:** build a calendar containing `"Therapy — Dr. Ruiz"`, run the collector,
  and assert the request body bytes contain no such string.
- **The limit, stated plainly:** this constrains what our server accepts. It cannot prove trusted Skill code never sends
  data anywhere else. The Skill is four files and auditable; that is the whole mitigation.

⚠ `week-balance/scripts/free_time.py` takes event summaries as input today. Any reuse here needs summary-free input or
a redaction pass before the request is built.

---

## 7. Safety surface

| Concern | Position |
| --- | --- |
| Who can be discovered | **Invite-only, consenting adults**, on the team's own devices. Not open signup |
| Identity | **A preset avatar, or one published photo, and no free-text bio.** The photo is optional, re-encoded from pixels with all metadata stripped before storage, and deletable in one press. A chat transcript is not a bio: it never appears on a card or in a notification |
| Photos | One per account, JPEG/PNG, ≤5 MB upload, long edge 1600 px after resize. Shown only to signed-in holders of the live card, **before either side confirms**; `no-store`; **404 for every refusal**, owner's own preview included. No automated moderation: a report hides the pair at once and a named team member responds |
| Chat | **The assistant chat is the interface**, and it is bounded: 4,000 characters per typed turn, a 200-message history, 7 days of retention, 20 turns per 10 minutes. It is addressed to your own assistant and is never another student's feed. A turn sends your text, the last 40 messages and a system prompt of your own state to the model provider named in the configuration — see the privacy policy; we do not control that provider's retention. Reveal (a pair-scoped thread between two students) is still deferred: talking happens off-platform |
| Reporting | One report → auto-hide both directions immediately, plus a named moderator on the team with takedown |
| Blocking | Bilateral, checked before candidate generation, so a blocked person cannot reappear |
| Repeat accounts | Out of scope for the demo, and said so |
| Screenshots | Cannot be prevented. Stated in the card UI and in our policy |
| Age | 18+ self-attestation, labelled as attestation |
| Moderator access | Reports only; per-user browsing logged and limited |
| Chat writes | Every chat and schedule write goes through `statewrite(mode="chat")` — **grant-preserving**: no grant is created, renewed, restored or withdrawn, and account liveness plus the state hash are revalidated inside the write transaction |
| Pending previews | Windows and events never write from a stream. A **Confirm click** is required and it stamps `free_time.student_confirmed_at` / `events_confirmed_at`; a stale state hash refuses with nothing applied. Tags, wants and busy apply immediately when they are the whole batch |
| Negotiation ops | Assistant-context only — a typed turn can never emit offer/accept/counter/pass. One op per turn, applied with the delegation grant, account liveness and offer version revalidated in-transaction; 3 rounds at most, and 2 failed provider attempts per offer before the assistant posts a manual Accept/Counter/Pass card |
| Transcript partition | The counterpart gets one server-templated note of fixed fields (decision, tag, link, reason code) — never provider prose, never your profile fields |

None of this makes the layer safe in the sense a real product needs. It makes it honest.

---

## 8. Events: designed, not demoed

Event discovery dilutes a negotiation story that is already strong, and adds a scraping surface. It is out of the demo
and kept here so the answer exists if a judge asks.

- Events are searched only inside the shared, live window; the whole event fits inside it, with travel at both ends.
- A listing whose `found_at` is older than 3 days is treated as unconfirmed and not shown. A cancelled rave is worse
  than no rave.
- **Nothing is ever booked by us** — not the server, not the Skill. Link only; any payment is the student's own browser
  and card.
- "Scheduled" never means a registration or a calendar write. It means both sides confirmed a time.

---

## 9. Policy matrix

### What the policies require

| Source | Says | Consequence |
| --- | --- | --- |
| Privacy › Limited Use | *"We do not sell Google user data, and we do not transfer it to others except to the sub-processors listed below acting on our instructions, to comply with applicable law, or as part of a merger or acquisition…"* | We are not listed. **Open** — see the top of this document |
| Privacy › Sharing | *"We share personal data only with service providers who process it on our behalf, with affiliates, in connection with a business transfer, or where required by law."* | Our server is outside that list |
| Terms › Your Content | The licence runs to Cubbie's sub-processors, *"solely to operate the Service"* | Cubbie's licence is not our permission; the student's instruction is |
| Terms › Eligibility | *"You must be at least 16 years old"* | Our discovery gate at 18 is a **voluntary product restriction**, stricter than required, and labelled as ours |
| Terms › Acceptable Use | No account sharing; no scraping; no circumventing rate limits | One pairing per agent; integration only through the Skill; no calls to Cubbie internals |
| Terms › Acceptable Use | *"Resell the Service, or use it to build a competing product, without our written permission"* | We are a Skill plus our own site, not a Cubbie competitor. Conduct settles this, not branding |
| SMS Terms | Consent covers requested one-time passcodes | Never reused. Our notifications travel through the student's own Cubbie check-in |

### Our own design measures, which are not compliance certificates

| Measure | Why |
| --- | --- |
| Ranges only; never titles, attendees, locations | Minimisation. A range is not a life |
| No course codes | A minimisation choice, not a policy prohibition. The enrollment leak is not worth the signal |
| Token hashing, revocation, agent ids | Basic hygiene |
| The one outside processor is named in our own policy | Chat turns go to the model provider in the configuration; we ask the same transparency of ourselves, and we say plainly that we do not control its retention |
| 30-day closed-match expiry, 90-day audit residue | Retention we can defend out loud |

### Documents we must write

Cubbie's terms describe Cubbie. Every question that matters in the demo — who can see my schedule, who can see my
name, how do I get out — is answered by **our** Privacy Policy and **our** ToS. Two pages, plain language, and a
working delete button.

---

## 10. Data model

```sql
users        (id, email, display_name, preset_avatar_id, adult_attested_at, created_at, deleted_at,
              photo_path, photo_revision, photo_published_at)
             -- photo_path: '{uid}-{revision}.jpg|png' under UPLOAD_DIR; photo_published_at NULL = withdrawn
pairings     (id, user_id, agent_id, token_hash, label, created_at, last_seen_at, revoked_at)

grants       (id, user_id, kind, tier, version, fields_json, audience,
              granted_at, expires_at, withdrawn_at)
consent_log  (id, user_id_hash, action, scope, detail_json, created_at)

availability (user_id, written_by_agent_id, payload_hash, observed_at,
              expires_at, source)                     -- source: 'student' | 'cubbie_computed'
availability_windows (user_id, starts_at, ends_at, tz)
hobby_tags   (user_id, tag)                           -- canonical vocabulary (scored + shown)
interest_raw  (user_id, raw, canonical)               -- the student's own words; never shown

candidates   (id, user_a, user_b, score, reasons_json, created_at, expires_at, state)
proposals    (id, candidate_id, window_start, window_end, version, state, expires_at)
proposal_confirmations (proposal_id, user_id, confirmed_at)   -- UNIQUE(proposal_id, user_id)
revoke_intents (user_id, created_at)                  -- written before any lock is taken

swipes       (user_id, candidate_id, direction, created_at)   -- right | left | block
blocks       (user_id_hash, blocked_user_hash, created_at, expires_at)

chat_messages (id, user_id, role, content, created_at)   -- role: 'user' | 'assistant' | 'note'
chat_pending  (id, user_id, created_at, expires_at, kind, payload_json, state_hash)
              -- kind: 'windows' | 'events' | 'ics'; one row per user; payload already validated
chat_turns    (id, user_id, kind, ref, at)               -- rate rows; kind 'chat' | 'negotiation', ref = 'offer:42'
```

- **Availability is keyed per user but records the writing agent**, so "revoke this agent" means something. Newest
  observation wins; `source` says whether Cubbie computed it or the student typed it.
- **The chat tables have their own clocks.** `chat_messages` holds the student's text, the assistant's text and
  server-templated notes; the sweep purges rows older than `AI_TTL_DAYS` (7), and account deletion cascades all three
  chat tables in the existing transaction. `chat_pending` is at most one row per user with a 10-minute TTL, deleted on
  confirm, cancel or supersede. `chat_turns` stores no message text and outlives transcript deletion, which is what
  makes "clear history" not a rate-limit reset.
- **No availability version history** is kept — that is the whole claim. What it does not claim: tags, match reasons,
  swipes and closed matches are retained and *are* a profile of interests. §3's retention table is the honest inventory
  of that, with a clock on each.

---

## 11. What we deliberately defer

| Deferred | Why it's safe to defer |
| --- | --- |
| Public discovery, open signup | Invite-only proves the mechanism; a public launch needs policy work we haven't done |
| Events and any booking path | Out of the demo — see §8 |
| Automated moderation of photos | Nothing scans an image before it is served. A report hides the pair at once and a named team member responds; a real classifier needs training data and an appeals path we don't have |
| Automated moderation of chat and provider content | Nothing scans a chat turn or a model reply before the student sees it. The bounds (per-turn length, turn rate, one op per turn, a closed op enum, a grant-preserving write mode) limit the blast radius, not the content. A real filter needs training data and an appeals path we don't have |
| Repeat-account prevention | Needs identity verification, which needs a product decision |
| Automated audit and backup machinery | A **policy commitment**, not a claim of implementation |
| Multi-agent conflict beyond revoke | One pairing per demo user; `written_by_agent_id` exists so revoke is meaningful |
| **Reveal / in-app messaging** | Designed as a grant, out of the demo: a thread needs its own authorization model and a moderation surface we aren't building. Talking happens off-platform |

**Not deferred**, because they are the demo: consent checks, revocation, deletion cascades, scheduling serialisation.

---

## 12. Demo script (~2½ minutes)

| Time | Shot |
| --- | --- |
| 0:00 | Sunday check-in on Ava's phone. She says yes to planning |
| 0:10 | Cubbie reads her week back: "free Thu 7-9 and Sat 6-11", plus her two hobbies — then asks before sharing |
| 0:25 | **The request body on screen, byte for byte.** Two ranges, two tags, one confirmation timestamp, no titles |
| 0:40 | Server log: `matched: shared tag=climbing, overlap 120min` — a reason, not a score |
| 0:55 | Ava's phone: "Ben's up for Thursday 7-9 — here's who" with the link |
| 1:10 | Ava opens the link, sees the card, swipes right |
| 1:25 | Ben's phone, next check-in: "Ava confirmed — open the link to confirm your side" (not "you're on") |
| 1:40 | Ben confirms: both see the same time. Then Ava revokes — her web session updates at once, Ben's phone says so on his next check-in |
| 1:48 | Ava's settings: *Publish this photo on my card*. Ben's open card shows the photo; the response is `no-store`. She presses remove — the same URL is a 404 and the preset avatar is back |
| 1:55 | The card: *"What our server accepted: 2 time ranges, 2 tags, one confirmation, and a name she chose to publish, plus a photo she published from her own settings page. What it rejects with a 422: classes, grades, calendar titles, and any photo an agent tries to send."* |
| 2:05 | **Chat, immediate apply** (`/chat`): "I'm free Tue 6-8 and Thu 7-9, into climbing." The reply streams; the tag op applies as the message lands and the chip rail flips — the words are a paraphrase, the chips are the record |
| 2:15 | **Chat, preview** (same page): ask for the windows. The assistant shows a normalised list and a **Confirm** card; the chips have not moved. Press Confirm — the chips change and a server-templated note says what was applied. Cancel a second proposal to show nothing writes |
| 2:25 | **Schedule** (`/schedule`): upload an `.ics`. The preview lists the computed ≤8 h windows; tick two, confirm. Re-upload the same file with an `RRULE` line → `422` naming the line, not silently free time. Point out the DB has no title, location or attendee in it |

Both demo students grant `card+name`, which is the point of the shot: a name before confirmation. Granting only `card`
would produce the same demo with `climber-7f2a` in place of every name — worth filming as the second take if there's
time, because it's the honest demonstration of the default.

**Delegation segment (both demo accounts seeded via `DELEGATION_DEFAULT_ON=1`).** Cut to the two assistants: X
(A's assistant) proposes an activity through `activity.propose`; Y (B's assistant) counters with another shared tag and a
reason code; X accepts. Each step appears in its own student's chat as a short structured note — decision, tag, link,
reason — and neither student's prose crosses. Then the humans: each gets "found a match — go swipe", swipes right, and
confirms. Say out loud what did **not** happen: no assistant swiped and no assistant confirmed a plan. Withdraw
delegation in Settings mid-segment and show the next counter dropped with an honest note, nothing applied.

The revocation beat at 1:40 is the part most teams won't have, and it answers "what happens when someone changes their
mind". The payload card answers "why should I trust you".

## 12b. A deadline note

Track 1 carries the "first 10 hours" rule. Track 2 has no announced build limit of that kind — its constraint is its own
closing time, read live with `python3 scripts/hackathon.py status`. Don't plan this build against Track 1's clock.

---

## 13. Open questions

1. **Calendar-derived availability** needs written confirmation from the Cubbie organizers before it ships. Until then,
   the demo uses synthetic fixtures and availability is labelled `source: 'cubbie_computed'` with a recorded student
   confirmation. This is the **legacy skill path only**: typed windows and a self-uploaded `.ics` never involve Cubbie,
   and the chat deployment does not depend on that gate.
2. **Does discovery survive at all?** It doubles the policy surface for a feature the judges didn't ask for. Keep the
   invite-only version for the demo; put public profiles on the "next" slide.
3. **Who moderates?** A named person on the team for the demo. A real answer is a support rota, which we don't have.
4. **Our Privacy Policy and ToS** — owner: whoever isn't coding the week before submission.
5. **One pairing per demo user.** Multi-agent ownership is modelled (`written_by_agent_id`) but not exercised.
