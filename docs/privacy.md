Social Match — privacy policy

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
  These are the only free-text field in your *structured state*, and they are tightly bounded: no line breaks, no control
  characters, no paragraph. The server translates each one onto its matching vocabulary ("matcha" counts as coffee).
  Your raw words stay attached to your own account. **A card shows the words the other student actually typed**
  ("matcha"), because that is what a person wants to read; the canonical tag ("coffee") is only how the matcher makes
  two different words comparable. Raw interests are bounded on write (at most eight, 32 characters each, no control
  characters or line breaks) and are only ever served to the other member of the same match pair. An interest nothing
  matches is kept for you and never scored. (The chat box is a second, separate free-text surface. It is described
  below, and it is not structured state.)
- Wants: zero to two words from `hangout` and `event` — whether you would rather meet up or go to an
  event. Nothing else fits the field, so nothing else can be written into it.
- Public events: if you approve it, up to ten links to public event pages (a career fair, a concert), each with its
  start and end time and one category from a fixed list (career, club, talk, music, sports, arts, food, party,
  volunteer, other). No event title, sender, location or email text is accepted. We store the link without anything
  after a `?`, because invitation emails often put a personal code there. Another student only ever sees an event if you
  both sent the same link, and then only that link and its category.
- How busy you are: one whole number from 0 to 3 for the next two weeks. If you use a phone agent it is worked out on
  your phone from Canvas and your calendar, and no course names or deadlines are sent; in the chat app it is a number you
  give your assistant in words, and it is stored only as that number. Nobody else sees it.
- Consent log: one row per grant, publication or withdrawal, recording the action, its scope and when it happened.
- Activity offers: when your assistant and another student's assistant settle what to do, we store each offer: an
  interest tag from the same closed vocabulary, an https link, the round number, which side offered it, and the answer
  (accept, counter, or pass with one of four reason codes: too expensive, too far, not their vibe, the wrong time).
  There is no title, no place name, no price and no sentence in an offer or an answer.
- Inbox messages: the notifications we compose for you (a new card, a counter-offer, a confirmation, a revocation).
- A pairing record for each device a legacy phone agent runs on, so that client can authenticate. The token is stored
  hashed. If you only ever use the web app, there is no pairing record to make.
- Your chat transcript: the messages you type to your assistant (up to 4,000 characters per turn) and the replies it
  streams back, plus the short system notes the server writes when it applies a change, refuses one, records a conflict,
  or records a negotiation step. The page shows the last 200 messages; the server keeps them for 7 days and then deletes
  them. Text only: there is no field for an attachment, and the transcript is never shown to another student.
- Pending previews: when your assistant (or an `.ics` upload) proposes windows or events, the normalised, already
  validated list waits for you to press Confirm. It holds those items and a state fingerprint, expires after 10 minutes,
  and is deleted when you confirm, cancel, or propose something else.

WHAT WE NEVER ACCEPT

The server rejects these outright, with HTTP 422 `payload_forbidden`. They are not "not stored" by policy — there is no
field for them and the validator refuses unknown fields at every level of the structured payload:

- calendar event titles, attendees, or locations;
- course codes, course names, professor names, or assignment names;
- grades, of any kind, anywhere;
- free text of any description **in structured state**, with three bounded exceptions. Interests (the tags above) are
  capped at 32 characters each and at most eight of them, and are canonicalised before they are used for anything. The
  second is the https link on an activity offer: we check it (https only, plain ASCII, at most 300 characters, a real
  domain name, no password in it, no #fragment, no IP address), the server never opens it, and anyone following it is
  leaving this server. The third is the link on a public event, checked the same way and stored without its `?` part. We
  cannot tell a public page from a page that has a personal code in its path, which is why you are shown each link before
  it is sent.

The chat box is the exception we do not hide: it accepts free text **by design**, because talking to your assistant is
the interface. That surface is bounded — 4,000 characters per turn, at most 20 turns in 10 minutes, 7 days of retention —
and it is addressed to your own assistant, not to another student. Assistant prose never enters your structured state,
and it never enters another student's transcript: the only thing that crosses between two students' assistants is a
server-built note of a few fixed fields (decision, tag, link, reason code). A turn that tries to write anything outside
those fields is discarded whole.

How a calendar becomes free time, in both paths. In the legacy path, your phone agent reads your calendar on your phone,
turns it into time ranges plus tags, asks you before anything leaves, and deletes the rest. In the chat path, you either
tell your assistant when you are free — and press Confirm on the normalised list it shows you — or you upload an `.ics`
export on the schedule page. That upload is read once, in memory, by this server: nothing is spooled to disk, and before
anything is stored it is reduced to intervals. Titles, locations, attendees and descriptions are never stored and never
logged. Recurring events (`RRULE`/`RDATE`), `TZID`-qualified times and `PERIOD` values are **rejected** with a message
naming what to remove from the export, instead of being guessed at and turned into free time you did not offer. The
upload is capped at 2 MiB. What comes back is a preview of windows for you to tick; only the windows you tick are stored.

THE ASSISTANT AND THE MODEL PROVIDER

Your assistant is a language model, and it does not run on this box. When you send a chat turn, three things go to the
model provider named in this deployment's configuration (`AI_API_URL`, `AI_MODEL`): a system prompt describing your own
state (your windows, tags, wants, busy number and events), the last 40 messages of your transcript, and the text you just
typed. The reply streams back and is stored in your transcript for the 7 days described below.

- The provider processes that text. We do not control how long it keeps it, whether it is used to train anything, or
  which country it sits in. We can tell you who the provider is — it is the endpoint and model name in this deployment's
  configuration, which the operator can show you — and that is the honest limit of what we can promise.
- Do not type secrets. Not a password, not a home address, not another student's name or contact details. Nothing you
  type is checked before it leaves, and the 4,000-character limit is a size bound, not a filter.
- Nothing of yours is sent for another student: the counterpart's assistant receives a server-built note (decision, tag,
  link, reason code), never your prose and never your profile.
- If this deployment has no `AI_API_URL`, chat is disabled, the page says so, and no turn is sent anywhere.

LETTING YOUR ASSISTANT NEGOTIATE FOR YOU

Delegation is a consent of its own, and it is **off by default** in a real deployment. The demo box switches it on at
registration (`DELEGATION_DEFAULT_ON=1`) so the negotiation segment runs without a click per student; every other
deployment leaves it off until you turn it on yourself.

- With it on, your assistant may accept, counter or pass an offer from the other student's assistant on your behalf. Your
  assistant is your agent in the negotiation; you are not relaying messages.
- Every step lands in your chat as a structured note — what was decided, the tag, the link, and a reason code when the
  answer is pass — so your transcript is the record of what was done in your name.
- **Only the Settings page changes it after registration.** The demo seed above is the one exception, and it is the demo's
  configuration, not your chat. No chat message and no assistant output can grant or withdraw it; asking your assistant
  to turn it on does nothing.
- You can withdraw it at any time in Settings. Withdrawal is checked inside the same database transaction
  that would make the decision: if you withdraw while your assistant is mid-turn, the decision is dropped,
  nothing is applied, and the transcript says so. There is no moment when an accepted offer can slip through
  between your withdrawal and the write.
- It never covers swiping or confirming a plan. A swipe is yours; a confirmation is yours. Those stay human presses.

YOUR PHOTO (REQUIRED TO BE MATCHED)

A photo is required before you can appear on a card or be matched with anyone: upload one on your own settings page in
a web session. Nothing else publishes it — sending your availability from a client neither publishes nor removes it, and
sending your availability again leaves it exactly as it was. Publishing a different image later is a new decision, not
an edit, because it is a different picture about to be shown to people you have not met.

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

- Availability expires 7 days after it was observed. Expired availability is never read, even before it is deleted. Your
  public events and busy number go with it.
- Closed matches, swipes and match reasons are kept 30 days, and the activity offers made on a match go with it.
- Your chat transcript is kept 7 days from the moment each message is written, then deleted by the sweep. "Clear chat"
  deletes it sooner; that is your right, not a way to reset the turn limit.
- The rows that count your chat turns against the rate limit are not part of the transcript. They hold timestamps, a
  kind and an optional offer reference — no message text — and they are kept somewhat longer than the 10-minute window
  they serve, so deleting your transcript cannot buy you a fresh allowance.
- A pending preview lasts 10 minutes, then it is gone. Confirming, cancelling, or proposing something else deletes it
  too.
- The consent log is kept while your account exists. When you delete your account it is stripped down to a pseudonymous
  id (a salted hash, not your account id or email), the action, its scope, and the timestamp — the detail payload is
  blanked to `{}` — and it is purged after 90 days.
- Blocks are pseudonymous from the moment they are created: they store salted hashes, never your account id, and they
  expire after 12 months.
- Your photo is kept while you leave it published. Remove it and the stored file is deleted; publish a new one and the
  previous file is deleted too. Deleting your account deletes it with everything else.
- Deleted rows persist in encrypted backups until they expire, within 14 days.

This is a self-hosted demo: one FastAPI process and one SQLite file, no analytics vendor, no email vendor, no cloud
queue. The one outside party is the model provider described above, and only chat turns reach it.

Matching your interests to other people's uses a small language-understanding model that runs on this same server. Your interest words are turned into numbers inside this process to judge how alike two interests are; they are not sent to any other party, and the numbers are not stored or shown. The model files themselves are downloaded once from the public model host when the server first starts.

YOUR RIGHTS, AS BUTTONS

- Every grant has a toggle in settings — matching, the two publication tiers, reveal, and delegation. Turning one off
  withdraws the live grant, and the withdrawal is recorded. The chat cannot flip any of them.
- "Leave matching" withdraws your matching grant, deletes your live availability, and voids every in-flight plan you are
  part of — the other side is told the plan is voided, without being told why.
- Your photo has its own button: publish, or remove. Removing is not a toggle you have to remember — the image stops
  being served the moment you press it.
- "Delete my account" runs the whole cascade in one transaction. Your account row is stripped (email, display name and
  password hash blanked, `deleted_at` set, so the session dies). Your availability and its windows, your wants, your hobby tags,
  your swipes, the activity offers you made or answered, your inbox items and the confirmations on your proposals are
  deleted. Your chat transcript, any pending preview, and your chat turn rows go too. Your photo is deleted and the
  stored image file unlinked. Your grants are withdrawn, your
  pairing records are revoked, and your candidates and live proposals are voided — so the other side's card or plan
  stops being served, without being told why. What survives is deliberately narrow: your block hashes (salted, so a
  block keeps working), the report rows (a report is not erased by deleting the reporter), and the stripped consent rows
  described above on their 90-day clock.
- Block and report: one report hides the pair immediately, both directions. A block does the same and additionally
  prevents any future card between the two of you, both directions.

To ask a question about any of this, ask whoever handed you the invite code — there is no support desk. To raise
something about a card or a photo, report the pair: that is the path that reaches the named team member who responds.
