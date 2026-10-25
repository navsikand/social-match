---
name: social-match
description: Finds students at your school whose free time and interests actually overlap yours, and sets up a real hangout — without ever sharing what is on your calendar. Use when a student wants to meet people, says they have no one to climb with, asks who else is free on a Thursday night, wants to be introduced to someone in their classes, or wants to stop sharing their availability.
---

# Social Match

Most students are not short of friends. They are short of the last step: both people are free on Thursday, both like
climbing, and neither finds out. This Skill does that one thing. It reads the student's own calendar and Canvas,
turns them into a short list of free windows plus a few interest tags, sends only that to a small matching server, and
texts the student when someone has an evening that fits.

Two rules run through everything here:

- **Nothing leaves the phone until the student reads it and says yes**, in their own words, in the same conversation.
- **Calendar contents never leave the phone.** Not titles, not attendees, not locations, not course codes. What leaves
  is a handful of time ranges and a few tags from a fixed list.

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
student is not shown the same card twice. Text the messages to the student word for word — see Replies.

### Step 2 — Read the calendar, then delete it

Read the student's free time for the next two weeks:

- calendars they have connected to Cubbie (their classes, shifts, practice, everything that is taken)
- Canvas for what is coming due, so a free window is not five hours before a paper is due
- their own words in this conversation

Then compute the windows when they are genuinely free: the gaps between commitments, kept to the next 30 days, at
least 30 minutes long, no longer than 8 hours each, at most 20 of them. Round to the half hour. Use their own time
zone.

**Redaction — do this before anything else.** In the windows you write down, keep only a start, an end and a time zone.
There is no field for anything else, and you must not invent one. Concretely:

- Never copy an event title, a class name, a course code, a professor, an attendee, a location, a room, or a note.
  "Therapy — Dr. Ruiz" does not become `Therapy`; the 3–4 pm slot becomes a window and the words are deleted.
- Never copy a Canvas assignment name or a course code. They only make a window smaller.
- Never write a tag that is not on the list below, and never turn a course name into a tag ("PSYCH 210" is not
  `psychology` and does not appear anywhere in the payload).
- If a window only exists because of a private appointment, it is still just a window. Do not describe it back to the
  student in terms of what it is.

The list of interests is closed. Pick at most five, only ones the student has actually said or shown:

`bouldering, climbing, hiking, running, gym, swimming, cycling, basketball, soccer, tennis, ultimate_frisbee,
board_games, video_games, chess, painting, drawing, photography, pottery, guitar, piano, singing, live_music, edm,
techno, rock, jazz, coffee, baking, cooking, language_exchange, debate, anime, cinema, volunteering`

### Step 3 — Ask, with the exact words that will be sent

Build the payload file (see `payload.example.json` for the shape, and the server contract for the fields), then show
the student precisely what leaves the phone and wait for a yes.

The message must contain the windows in plain language, the tags, whether their first name goes with the card, and the
exact words "yes, send it". Use this text, filled in, one item per line:

```text
Ready to share this with the matcher? Here's exactly what leaves your phone and nothing else:

- free Thu 7-9 pm
- free Sun 10 am-12 pm
- into climbing, live music, coffee
- your card shows your first name, Maya

Reply "yes, send it" and I'll send it now. Or say what to change.
```

Rules for that message:

- Quote the windows exactly as they will be sent. If a window changes, the message changes and you ask again.
- Say the name out loud when it is included; if the student would rather stay a pseudonym, say "your card shows a
  nickname, not your name" instead and send `"tier": "card"`.
- Do not soften it into "want me to look for people?" — that is a different question. This is the one moment where
  consent for what leaves the phone is given.
- Anything the student has not agreed to is not in the payload and not described.

**`student_confirmed_at` is the moment they say yes.** Its value is that instant in UTC (for example
`2026-10-24T09:10:00Z`), not the time you read the calendar and not a placeholder. It records that the student saw the
windows and agreed; it is not proof of anything and must never be described as verification.

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

**Never say "you're on" for a confirmation.** A confirmation is one side agreeing. The student confirms their own
side on the card, and only a `match` message means the plan is real.

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
- Calendar not connected: "I can't see your calendar yet. Connect it and I'll find your free evenings — or tell me
  which evenings are free."
- Canvas not connected: skip the deadline pass and say so once: "I can't see Canvas, so I'm assuming those evenings
  are really free."
- Server down (`cannot reach ...`): "the matcher's server isn't answering. I'll try again later — your calendar
  didn't go anywhere."
