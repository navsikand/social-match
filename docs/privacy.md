Study Match — privacy policy

This is a hackathon demo server. It runs on a teammate's laptop, it stores data for a class project, and it is not run
by a company. It has no age verification and no identity verification: signing up means you ticked a box saying you are
18 or older, and nothing checks that. Treat everything here as a demo, not as a production social product.

WHAT WE COLLECT

Only these things, and only because matching does not work without them:

- Your account: email address, password (we store a hash, never the password itself), display name, and a preset avatar
  id you pick from a fixed list.
- Your photo, if you publish one: one image per account, JPEG or PNG, up to 5 MB. The server decodes it, applies the
  camera's rotation, shrinks it so the long edge is at most 1600 pixels, and stores a fresh copy built from those
  pixels. The file you chose does not survive that step: no GPS location, no camera details, no colour profile, no
  comments, no hidden text. All of that is gone before anything is stored.
- Availability: your free windows as start and end times in UTC, plus the time zone they were given in so we can print
  them back in your own local time.
- Interests: at most eight short tags (32 characters each), in your own words — "matcha", "sourdough", "bouldering".
  These are the only free-text field in the system, and they are tightly bounded: no line breaks, no control
  characters, no paragraph. The server translates each one onto its matching vocabulary ("matcha" counts as coffee).
  Your raw words stay attached to your own account and are shown back to you; **only the canonical tag ever appears
  on a card**, so your phrasing is never shown to another student. An interest nothing matches is kept for you and
  never scored.
- Wants: zero to two words from `hangout` and `event` — whether you would rather meet up or go to an
  event. Nothing else fits the field, so nothing else can be written into it.
- Consent log: one row per grant, publication or withdrawal, recording the action, its scope and when it happened.
- Inbox messages: the notifications we compose for you (a new card, a counter-offer, a confirmation, a revocation).
- A pairing record for each device your Cubbie agent runs on, so the agent can authenticate. The token is stored hashed.

WHAT WE NEVER ACCEPT

The server rejects these outright, with HTTP 422 `payload_forbidden`. They are not "not stored" by policy — there is no
field for them and the validator refuses unknown fields at every level of the payload:

- calendar event titles, attendees, or locations;
- course codes, course names, professor names, or assignment names;
- grades, of any kind, anywhere;
- free text of any description — with one bounded exception: interests (the tags above), which are capped at 32
  characters each and at most eight of them, and are canonicalised before they are used for anything.

That is the point of the design: your Cubbie agent reads your calendar on your phone, turns it into time ranges plus
tags, and deletes the rest. What reaches this server is a handful of intervals and a few words from a closed list.

YOUR PHOTO, IF YOU PUBLISH ONE

A photo is off until you press the publish button on your own settings page in a web session. Sending your availability
from your Cubbie agent neither publishes it nor removes it, and sending your availability again leaves it exactly as it
was. Publishing a different image later is a new decision, not an edit, because it is a different picture about to be
shown to people you have not met.

Who can see it: signed-in students who are holding your live card — that is, people you have been matched with, **before**
either of you confirms anything. Nobody else. It is not public and it is not indexed by search engines. Its address is
predictable, but knowing or sharing that address does not grant access: every fetch is authorised against your
signed-in session and re-checked against the same rules the card uses. Responses carry `no-store`, which instructs
browsers and caches not to keep a copy — that is an instruction to the client, not a promise about what any
particular browser retains.

Removing it takes effect at once: the record that allows access is cleared before anything else happens, so the image
stops loading for everyone, including your own preview, and the stored file is deleted.

What this cannot do — the honest part:

- The picture itself can identify you. A face, a name tag on a lanyard, a room, a poster on the wall. We strip the
  metadata, not the content.
- There is **no automated moderation**. Nothing checks what an image shows before it is served. If you report the pair,
  the card and the photo stop being shown immediately, both directions, and a named team member looks at it and
  responds.
- Screenshots cannot be prevented. Anything shown on a screen can be captured and kept.
- A photo someone has already loaded cannot be taken back from them. Removing it stops all future fetches; it does not
  reach into a copy that was already viewed or saved.

HOW LONG WE KEEP IT

There is one rule per kind of data, and the server enforces each one:

- Availability expires 7 days after it was observed. Expired availability is never read, even before it is deleted.
- Closed matches, swipes and match reasons are kept 30 days.
- The consent log is kept while your account exists. When you delete your account it is stripped down to a pseudonymous
  id (a salted hash, not your account id or email), the action, its scope, and the timestamp — the detail payload is
  blanked to `{}` — and it is purged after 90 days.
- Blocks are pseudonymous from the moment they are created: they store salted hashes, never your account id, and they
  expire after 12 months.
- Your photo is kept while you leave it published. Remove it and the stored file is deleted; publish a new one and the
  previous file is deleted too. Deleting your account deletes it with everything else.
- Deleted rows persist in encrypted backups until they expire, within 14 days.

We do not use sub-processors. This is a self-hosted demo: one FastAPI process and one SQLite file. There is no
analytics vendor, no email vendor, no cloud queue.

YOUR RIGHTS, AS BUTTONS

- Every grant has a toggle in settings. Turning one off withdraws the live grant, and the withdrawal is recorded.
- "Leave matching" withdraws your matching grant, deletes your live availability, and voids every in-flight plan you are
  part of — the other side is told the plan is voided, without being told why.
- Your photo has its own button: publish, or remove. Removing is not a toggle you have to remember — the image stops
  being served the moment you press it.
- "Delete my account" runs the whole cascade in one transaction. Your account row is stripped (email, display name and
  password hash blanked, `deleted_at` set, so the session dies). Your availability and its windows, your wants, your hobby tags,
  your swipes, your inbox items and the confirmations on your proposals are deleted. Your photo is deleted and the
  stored image file unlinked. Your grants are withdrawn, your
  pairing records are revoked, and your candidates and live proposals are voided — so the other side's card or plan
  stops being served, without being told why. What survives is deliberately narrow: your block hashes (salted, so a
  block keeps working), the report rows (a report is not erased by deleting the reporter), and the stripped consent rows
  described above on their 90-day clock.
- Block and report: one report hides the pair immediately, both directions. A block does the same and additionally
  prevents any future card between the two of you, both directions.

To ask a question about any of this, ask whoever handed you the invite code — there is no support desk. To raise
something about a card or a photo, report the pair: that is the path that reaches the named team member who responds.
