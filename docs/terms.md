Study Match — terms of use

WHAT THIS IS

Study Match is a hackathon demo. You sign in and talk to your own AI assistant; two students' assistants negotiate one
plan through a small broker server, and the website holds your chat, your schedule, the card, the swipe and the confirm.
The Cubbie skill still works as a legacy/alternate client, and the paragraphs that mention it say where that path
differs. It is a class-project artefact, provided as-is, for demonstration. It is not a production social product and it
comes with no safety guarantees of one: no automated moderation, no background checks, no
verification of who anyone is, no uptime promise, and no promise that the person on the other side of a card is who the
card says.

WHO CAN USE IT

Only people who were given the invite code (the `REGISTRATION_CODE` gate). The gate exists to keep the demo runset
small; it is not identity verification. You must be 18 or older, and you confirm that by ticking a box when you
register. That is self-attestation — the server records the timestamp of your attestation, and nothing checks it. There
is no age verification and no verification that you attend any particular school. If you are not 18, do not use it.

YOUR ASSISTANT, YOUR RESPONSIBILITY

Your assistant acts for you. You are responsible for what you type, what you upload, and what it sends on your behalf.
In the chat app that means the words in the box and any `.ics` file you choose on the schedule page; in the legacy path
it means what the Cubbie Skill sends, in the same way Cubbie's own terms make you responsible for your agent's actions.
The server separately re-checks every consent grant at the moment it is relied on, and it re-checks your state before it
applies anything — but none of that is a substitute for reading what you are about to agree to. If you tell the
assistant to send something, we treat that as your instruction.

WHAT THE ASSISTANT SAYS

The assistant's replies are generated text. They are not checked by a person before you see them, and they can be wrong
— including about your own state: it may misstate your windows, forget a tag, or claim it changed something it did not.
**The chips on the chat page and the Settings page are the truth**, and a change only counts once you see it there.
When the assistant applies a change, the server writes a short note into your transcript saying what actually happened;
when it could not apply one, the note says that instead. Treat the prose as a helpful paraphrase, never as the record.

The assistant acts on offers only under the **delegation** grant ("Let my assistant accept, counter or pass offers for
me"): accept, counter or pass on an activity offer for a match you already hold. It is off by default in a real
deployment; the demo box seeds it on at registration. You can withdraw it at any time in Settings, and only Settings can
change it — nothing you type to the assistant can turn it on. Withdrawing it never undoes a match, a card or a plan; it
only stops future decisions. Swiping and confirming a plan are never delegated: those are your presses, always.

The `.ics` upload is a demo convenience, not a calendar service. The file is read once in memory and reduced to free
intervals; titles, locations and attendees are dropped, and recurring or time-zoned exports are rejected rather than
guessed at. **Check the previewed windows against your real calendar before you press Confirm** — we cannot see your
calendar and we cannot know that the export was complete or current.

YOUR PHOTO

You can publish one photo on your card, from the settings page in a web session. Your assistant cannot publish it,
replace it or remove it, and sending your availability again does not change it either way. The publish button is the
whole agreement: nothing is published until you press it, and a new image is a new agreement, because it is a new
picture about to be shown to people you have not met.

You are responsible for what your photo shows. We do not check images before they are served — there is no automated
moderation — so the judgement about what is in the picture is yours. A face is fine; a student ID, a name tag, an
address, a room number or anything else that tells a stranger where to find you is a bad idea. Publish something you
would be willing to have a classmate screenshot.

You can remove your photo at any time, and removal is immediate: the image stops being served, for everyone, and the
stored file is deleted. What we cannot do is reach a copy someone already has — see the screenshot point below.

WHAT WE DO NOT DO

- We never book, buy, register for, or reserve anything on your behalf. If a plan needs a court, a table or a ticket,
  you and the other student do that yourselves — we only ever link out.
- We cannot prevent screenshots. Anything shown to another student can be screenshotted, kept and reposted by them.
- We cannot recall an image or a message someone has already seen. Removing a photo stops future fetches; it does not
  undo a view that already happened.
- We do not review photos before they are served, and we have no automated moderation of any kind.
- We cannot recall a message already delivered to someone's phone.
- We do not run background checks, we do not verify identity, and we do not vet the people you are matched with.
- We do not read your calendar. In the legacy path the calendar stays on your phone and the server only receives time
  ranges and tags from a fixed list; in the chat path you type when you are free, or upload an `.ics` export that the
  server reads once in memory and reduces to intervals, keeping no titles, locations or attendees. Anything else in a
  structured payload is rejected.

SAFETY TOOLS

Blocking is bilateral: when you block someone, neither of you can be shown to the other again, in either direction.
Reporting is immediate: one report hides the pair both ways at once, and a named team member — a person on this project,
not a queue — looks at it and responds. Neither tool undoes a conversation you already had,
and neither is a substitute for ordinary caution about meeting strangers — meet in public, tell someone where you are.

Reporting is also the way to raise a photo. It hides the pair at once, which means the card and the photo stop being
served, and it is the path by which the image gets looked at and, if it should not be there, taken down.

MISUSE

If the invite code gets out, or the demo is used to harass someone, we rotate the invite code and the demo stops
accepting new accounts. We may also delete accounts and data without notice, because this is a demo and there is no
service commitment. Do not use this to send anything to another student that you would not say to their face, and do
not publish a photo you would not show them in person.

NO WARRANTY, NO LIABILITY

The service is provided "as is", without warranty of any kind, express or implied, including fitness for a particular
purpose. To the extent the law allows, we are not liable for any loss or damage arising from your use of it — including
missed plans, wrong times, matches you did not enjoy, advice or statements an AI assistant got wrong, or data loss when
the demo database is thrown away. Nothing here
limits liability that cannot be limited by law, and nothing here creates a duty of care that a production social
product would owe you.

CHANGES

These terms and the privacy policy can change as the demo changes. The version served at `/terms` and `/privacy` is the
current one.
