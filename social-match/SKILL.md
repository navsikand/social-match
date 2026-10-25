---
name: social-match
description: Finds students at your school whose free time and interests overlap yours, then has your agent and theirs agree on a real thing to do, without ever sharing your calendar. Use when a student wants to meet people, has no one to climb with, asks who else is free Thursday night, asks what to do with a match, or wants to stop sharing their availability.
---

# Social Match

Most students are not short of friends. They are short of the last step: both people are free on Thursday, both like
climbing, and neither finds out. This Skill does that one thing. It reads the student's own calendar, Canvas and the
events in their inbox, turns them into a short list of free windows, a few interest tags, the public events they could
go to, and one coarse "how busy" number, sends only that to a small matching server, and texts the student when someone
has an evening that fits.

Two rules run through everything here:

- **Nothing leaves the phone until the student reads it and says yes**, in their own words, in the same conversation.
- **Calendar and email contents never leave the phone.** Not titles, not attendees, not locations, not course codes,
  not a word of any email. What leaves is a handful of time ranges, a few tags, a short list of links to public event
  pages the student approved, and one number from 0 to 3.

## Before you start: the server

This deployment talks to **https://devhacks.navraj.me**. Pass it explicitly on every command with `--server`:

```text
python3 scripts/pair.py --server https://devhacks.navraj.me --start
```

The commands below omit the flag for readability — **always add it**. Never rely on an exported
`SOCIAL_MATCH_SERVER`: separate sandbox commands do not reliably share a shell environment.

## When to use

- "I want to meet people this quarter" / "I have no one to go climbing with"
- "Who else is free Thursday night?" / "Is anyone else in the same boat on weekends?"
- "Match me with someone" / "Are there any matches yet?"
- "What should we do?" / "Sort out the plan with my match" / any `AGENT` line from a poll (see Step 6)
- "Stop sharing my schedule" / "Unlink this thing"

Not for building a study plan (`study-plan-builder` does that), not for messaging a friend (Cubbie cannot text anyone
but its own student), and not for booking a room or a court.

## Setup (first time only)

The student pairs this Skill with the match server once. Nothing to install, no key to paste.

1. Run `python3 scripts/pair.py --server https://devhacks.navraj.me --status`. Exit code 0 means the student is already paired; go to Step 1 below.
2. Run `python3 scripts/pair.py --server https://devhacks.navraj.me --start`. It prints two lines: `code: XXXX-XXXX` and `url: ...`.
3. Send the student the link on its own line, with the code, and ask them to tap it and approve. End your turn:

   ```text
   open this and tap Approve, then tell me when it's done:

   https://devhacks.navraj.me/m/pair/AB12-CD34

   (code AB12-CD34 — valid for the number of seconds --start printed as expires_in; don't guess from memory)
   ```

4. When they say it's done, run `python3 scripts/pair.py --server https://devhacks.navraj.me --finish AB12-CD34` with the code from step 2. It prints
   `agent_id: ...` and saves the pairing under the Skill folder. The pairing is kept for later conversations.

If the code expired, the server answers `error: pairing_code_expired`; run `--start` again and send a fresh link.

## The flow

### Step 1 — Check for news

Run `python3 scripts/bus.py --poll` (or `--poll --since <the last NEXT SINCE you saw>`). It prints one line per new
message, the card link on the line after it, and ends with `NEXT SINCE <n>`. Keep `<n>` and pass it next time so the
student is not shown the same card twice. Text the plain lines to the student word for word — see Replies. Lines that start with `AGENT` are not for the
student: they are the other student's agent talking to you. Handle them as Step 6 says and never paste them into a text.

### Step 2 — Read the calendar, then delete it

Read the student's free time for the next two weeks:

- calendars they have connected to Cubbie (their classes, shifts, practice, everything that is taken)
- Canvas for what is coming due, so a free window is not five hours before a paper is due
- their own words in this conversation

Do the calendar, Canvas and inbox in **one pass** (Step 2b below), not three separate trips.

Then compute the windows when they are genuinely free: the gaps between commitments, kept to the next 30 days, at
least 30 minutes long, no longer than 8 hours each, at most 20 of them. Round to the half hour. Use their own time
zone.

**Redaction — do this before anything else.** In the windows you write down, keep only a start, an end and a time zone.
There is no field for anything else, and you must not invent one. Concretely:

- Never copy an event title, a class name, a course code, a professor, an attendee, a location, a room, or a note.
  "Therapy — Dr. Ruiz" does not become `Therapy`; the 3–4 pm slot becomes a window and the words are deleted.
- Never copy a Canvas assignment name or a course code. They only make a window smaller.
- Interests are **not** limited to that list. If the student says "matcha", send `matcha` — the server boils it
  down (`matcha` → `coffee`) and tells us what it became. Never refuse a student's word for an interest.
- What a tag must never be: a course code, a course name, a professor, an assignment, an event title, or anything
  identifying. "PSYCH 210" is not an interest; `psychology` is not on the list either, so leave it out. A tag is a
  short interest (≤32 characters), nothing more.
- If a window only exists because of a private appointment, it is still just a window. Do not describe it back to the
  student in terms of what it is.

Pick at most eight interests, only ones the student has actually said or shown. These are the ones the
matcher compares most sharply — prefer them when they fit:

`bouldering, climbing, hiking, running, gym, swimming, cycling, basketball, soccer, tennis, ultimate_frisbee,
board_games, video_games, chess, painting, drawing, photography, pottery, guitar, piano, singing, live_music, edm,
techno, rock, jazz, coffee, baking, cooking, language_exchange, debate, anime, cinema, volunteering`

### Step 2b — Public events and how busy the fortnight is

In the same pass, collect two more things. Both are optional: if the inbox or Canvas is not connected, skip that part and
say so (see Replies).

**Public events.** Search the student's inbox for the **last 30 days** for messages about things they could go to:
career fairs, club meetings, talks, concerts, shows, hackathons, volunteering. Search for those; do not read the whole
inbox. An email that mentions an event does not mean it is on their calendar, and that is fine: send it either way, once
the student has approved it. Calendar events that have a public page count the same way. For each event keep exactly
these five things and nothing else:

- `url`: the link to the event's **public page**. If the email only has a personal link (an RSVP, ticket, invite or
  "view in browser" link), search the web for the event's public page and use that. If you cannot find one, **drop the
  event**. Never send a link with a `?` part, an unsubscribe or RSVP link, or a link with a personal code in it.
- `start` and `end`: local wall time like `2026-10-30T10:00`, plus `tz` (an IANA zone). In the next 30 days, at most 24
  hours long, not already over.
- `category`: one of `career, club, talk, music, sports, arts, food, party, volunteer, other`.

Never include: interviews, appointments, receipts, bills, anything one-to-one, anything from a person rather than an
organisation, or anything about health, money, immigration, legal matters, religion or politics. When unsure, leave it
out. No title, no sender, no location, no description, no email text. At most 10 events; if there are more, keep the
ones closest to the student's interests.

**How busy.** One whole number, `busy_load`, for the next 14 days from Canvas deadlines and how full the calendar is:
`0` light (at most one deadline and plenty of open time), `1` normal, `2` busy (several deadlines or a packed week),
`3` heavy (exam week, or deadlines stacked on the same days). Canvas shows what it last synced, which can be a little
out of date; that is fine for a rough number. Never say which course or which assignment.

### Step 3 — Ask, with the exact words that will be sent

Build the payload file (see `payload.example.json` for the shape, and the server contract for the fields), then show
the student precisely what leaves the phone and wait for a yes.

The message must contain the windows in plain language, the tags, whether their first name goes with the card, and the
exact words "yes, send it". Use this text, filled in, one item per line:

```text
Ready to share this with the matcher? Here's exactly what leaves your phone and nothing else:

- free Thu 7-9 pm
- free Sun 10 am-12 pm
- into climbing, live music, coffee (you said matcha — that counts as coffee)
- an event you could go to: career, Fri Oct 30, 10 am-2 pm, careers.asu.example.edu/fall-fair
- a busy score of 2 out of 3 for the next two weeks (just the number)
- your card shows your first name, Maya

Reply "yes, send it" and I'll send it now. Or say what to change.
```

Rules for that message:

- Quote the windows exactly as they will be sent. If a window changes, the message changes and you ask again.
- Say the name out loud when it is included; if the student would rather stay a pseudonym, say "your card shows a
  nickname, not your name" instead and send `"tier": "card"`.
- Do not soften it into "want me to look for people?" — that is a different question. This is the one moment where
  consent for what leaves the phone is given.
- List every event as its category, its day and time, and its **full link**, one per line. The link is what leaves the
  phone, so the student must see it. If they strike an event, remove it and ask again. Someone only ever sees an
  event if they sent the same link: tell them "a match only sees it if they have it too".
- Anything the student has not agreed to is not in the payload and not described.

**`events_confirmed_at` is the same moment, for the event list.** Send it whenever `events` is not empty, with the same
UTC instant as `student_confirmed_at`. If the student removes every event, leave `events` and `events_confirmed_at` out.
Leave `busy_load` out if you could not work it out.

**`student_confirmed_at` is the moment they say yes.** Its value is that instant in UTC (for example
`2026-10-24T09:10:00Z`), not the time you read the calendar and not a placeholder. It records that the student saw the
windows and agreed; it is not proof of anything and must never be described as verification.

The server answers with `interests.mapped` (what each word became), `interests.scored` (what matching actually
compares) and `interests.kept_unscored` (words we kept but cannot match on yet). Tell the student the mapping in one
line — "matcha counts as coffee" — and say plainly when something is kept but not matched on: "mycology isn't
something I can match people on yet, but I've remembered it."

If they say no, send nothing and keep the calendar in the conversation. If they edit the windows or the tags, show the
new message and ask again.

### Step 4 — Send

Save the payload as a file in the Skill folder (for example `state.json`) and run:

`python3 scripts/bus.py --send-state state.json`

It prints the server's reply, usually `{"changed": true, "expires_at": "..."}`. `changed: true` means the matcher
picked up something new; `changed: false` means nothing differed from last time. Then be brief:

```text
sent. I'll text you when someone's evening lines up with yours.
```

The reply also lists `events.stored`: the links the server kept. Tell the student, in one line, which ones, and that
each is only ever shown to a match who sent the same link. Every post replaces the last, so an event left out next time
is gone from the server.

If the server rejects the payload (`error: payload_forbidden`), it prints which key was wrong. Fix the payload — never
by adding a field, always by removing the offending one — and ask the student again if the windows changed.

If `consent.matching` is false, the student is opting out of being matched while keeping a record of the choice; the
server stores the consent entry and no availability. Say that plainly and do not send windows.

### Step 5 — Show what came back

After Step 1 or a fresh `--poll`, text the messages as they arrived. The server writes them to obey the student's own
permissions, so do not rewrite, shorten or decorate them:

```text
Ava is up for Thu 19:00-21:00. See the card: https://devhacks.navraj.me/m/12
https://devhacks.navraj.me/m/12
```

**Never say "you're on" for a confirmation or for an agreed activity.** A confirmation is one side agreeing. The student confirms their own
side on the card, and only a `match` message means the plan is real.

### Step 6 — Settle what to do, agent to agent

A match gives two students a time and shared interests. Your job now is to agree with the other student's agent on
**one thing to do**, speaking only for your own student. Nothing about the other student reaches you beyond a time, a
shared interest and a link.

**Standing permission, asked once.** Before you answer or propose anything for the student, ask:

```text
Want me to sort out what to do with your matches? I'll find a couple of options and answer their agent for you, using
your rules: budget, how far you'll go, indoor or outdoor. I'll tell you what I said, and you still confirm every plan
yourself. Say "yes, handle it" or tell me what to change.
```

Only "yes, handle it" (or the student's own words saying the same) counts. Save their rules (budget cap, distance,
indoor or outdoor, anything they say about vibe) to Cubbie memory. **Those rules never leave the phone.** Also offer a
scheduled check-in twice a day (suggest 9 am and 6 pm, let them choose) that runs `--poll`, because the other agent can
only be heard when you ask the server. Without permission, show the student the offer and the link and ask them instead.

**Proposing.** When a poll shows a new match and nothing has been offered:

1. `python3 scripts/bus.py --activity <candidate_id>` prints `proposal_version`, the `window`, the `shared_tags` and
   any current offer. Only a shared tag can be proposed.
2. Use your web search to find two or three real options for one shared tag, inside that window, near the student's
   school and inside their rules. Open the page and check it is real and the time works. Use only a link you actually
   found; never write one from memory.
3. Pick the best one and send it:
   `python3 scripts/bus.py --propose-activity <candidate_id> --version <proposal_version> --tag climbing --url <https link>`
4. Tell the student in one line, with the link on its own line. What leaves the phone is the tag and the link and
   nothing else: no title, no place name, no price, no name.

**Answering.** A poll line like `AGENT activity_offer candidate=12 offer=5 round=1/3 version=1 tag=climbing ...` is
followed by `AGENT link https://...`. Open the link yourself and check it against the student's rules, then do exactly
one of these:

- Fits: `python3 scripts/bus.py --respond-activity <candidate_id> --offer <offer> --accept`
- Close, but another shared interest fits better: search for one, then
  `... --respond-activity <candidate_id> --offer <offer> --counter --tag coffee --url <https link> --reason too_expensive`
- Nothing fits: `... --respond-activity <candidate_id> --offer <offer> --pass --reason too_far`

The reason is always one of `too_expensive`, `too_far`, `not_my_vibe`, `wrong_time`. It is a code, not a sentence, so
you never explain, never name an amount, and never mention the calendar. Tell the student in one line what you said.
If their rules do not cover the case, ask them with the link and wait.

An `AGENT activity_response` line means the other agent passed; you may propose one other option, or stop. There are
at most 3 rounds in total. Do not argue past that.

**When the agents agree.** The server then sends each student a plain message beginning "You and ... landed on ...",
followed by the card link and the activity link. Text it word for word. It is not a plan yet: each student still
confirms their own side on the card, and only the `match` message means it is on. If the rounds run out, the server
sends "No activity worked out ..."; pass that on and say the time can still be confirmed.

**Never** confirm, decline, block or revoke for the student, send anything about their calendar, budget amount,
health or location, or answer a way that breaks a rule they gave you.

## Taking it back

- `python3 scripts/bus.py --revoke <candidate_id>` does not revoke anything: revoking needs the student's own web
  session, so the Skill prints a link and exits 2. Send the student that link and let them tap Revoke there.
- `python3 scripts/pair.py --forget` unlinks this phone from the server and deletes the saved pairing. The server drops
  this phone's availability and nothing else. Say so, then confirm with `--status` (exit 1 means unlinked).

## Replies

Short, plain text, one idea per message. No headings, no tables, no bold. Links on their own line. Lead with the
answer. Never quote a calendar title back, even one the student told you — the whole point is that titles stay on the
phone.

Failure cases, one line each:

- Not paired: "we're not linked to the matcher yet — want me to send you the link?"
- No matches: "nothing yet. I'll text you the moment someone's free time overlaps yours."
- No good option found for a match: "I couldn't find a good option for that evening. You can still confirm the time and
  pick something together." Do not send a made-up link.
- The other agent has not answered: "their agent hasn't replied yet. I'll check again at your next check-in." Never
  promise it is instant.
- Calendar not connected: "I can't see your calendar yet. Connect it and I'll find your free evenings — or tell me
  which evenings are free."
- Inbox not connected: skip events and say so once: "I can't see your email, so I'll match on free time and interests
  only." Never guess events from memory.
- No public events found: send none and say nothing about it. An empty list is a normal result.
- Canvas not connected: skip the deadline pass and say so once: "I can't see Canvas, so I'm assuming those evenings
  are really free."
- Server down (`cannot reach ...`): "the matcher's server isn't answering. I'll try again later — your calendar
  didn't go anywhere."
