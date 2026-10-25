# UI/UX audit — consolidated findings (review-corrected, then FIXED)

> **Status: historical audit. All findings below were resolved in the fix waves that followed;
> this document is kept as the record of what was found, not as a list of open defects.**
> Every P1 and P2 was implemented and gated twice by an expensive reviewer (the second gate
> blocked on four defects introduced by the first fix wave, and on two more in the rework; all six
> are closed). Current evidence: **544 tests pass**, `demo/scenario.py` **17/17 beats**, and the
> consent/state invariants are unchanged — chat and schedule writes remain `mode="chat"`
> grant-preserving, and grant mutation stays agent-only (`server/statewrite.py`).
> Each finding is marked **[FIXED]** below with the surface that now carries the behaviour.

Five parallel journeys audited (onboarding, chat, schedule, decision loop, cross-cutting), each
reading the templates **and** the backend that shapes them. This version has been through an
expensive review that checked every P1 and half the P2s against source: it corrected seven
claims, downgraded two severities, amended four fixes and **found one defect all five lanes
missed**. Corrections are marked `[corrected]`.

Product framing: one student on a laptop, the same student on a phone. Core loop is
**talk → preview → confirm → match → confirm plan**. Order and timing are where it breaks.

Verdict from the first review: **SHIP-WITH-CORRECTIONS** — nothing here was a fake finding, but
several descriptions were too strong and four proposed fixes would have been wrong. Final gate:
**APPROVED** after the two release-blocking follow-ups below were closed.

---

## A. Order-of-operations defects

### A1. [P1] **[FIXED]** `/schedule` is write-only; Save on the blank form replaces every stored window
`server/webui/templates/schedule.html:13-31,148,270-272` · `server/schedules.py:536,580-584` · **C, E**
At the time of the audit there was no `GET /api/schedule/windows` (only `GET /schedule/pending`), so
the form always loaded blank while Save posted the visible rows as the *whole* list. Fixed: the GET
exists, the form prefills from it, writes stay disabled until it succeeds, and every
nonempty→empty replacement asks for confirmation.
1) add “Thu 19:00–21:00”, Save → “Saved. 1 window stored.” 2) reload → blank form 3) Save again →
“Saved. 0 windows stored.” Availability is gone, silently.
**Fix:** add the GET and prefill. Do **not** simply disable empty saves — offer an explicit
“Clear all windows?” confirmation instead, so intentional clearing still works. Keep
`mode="chat"` and the Save-click provenance; never route this through a grant-changing write.

### A2. [P1] **[FIXED]** “Yes, let's do it” lands on an empty Matches page and leaves the card looking unanswered
`card.html:255` · `scheduling.py:241,166,463-481` · **D**
Right swipe → “Confirmed your side. We'll tell you when they confirm too.” → 900 ms later
`/matches` → **“No confirmed plans yet.”** → the dashboard still lists the same card, all buttons
live → the natural move is to press Yes again.
**Fix:** branch on the returned proposal state — first confirmer sees “Waiting for <name>” in
place; the second confirmer sees the confirmed plan and may navigate. A blanket “Waiting” would
misrepresent the second person.

### A3. [P1] **[FIXED]** A confirmed plan cannot be withdrawn — while Matches promises it can
`card.html:196-201` · `matches.html:6` `[corrected: was :3]` · **D**
`state === "matched"` hides **every** button via `querySelectorAll("button")`, including
`#revoke`. Matches copy still says “Withdrawing is always one tap away on the card.”
**Fix:** hide only yes/no/block/report/suggest; keep `#revoke` (the API accepts matched candidates).

### A4. [P1] **[FIXED]** Turning “Matching” off is an undisclosed destructive save
`settings.html:23-26,70-81` · `router.py:269-277` → `scheduling._leave_cascade` · **A**
`[corrected]` Unticking does nothing by itself — the cascade runs when **Save sharing settings**
posts `matching:false`, and the cascade itself is *intentional* (`CONTRACT.md:227-228` requires
leave-matching to delete availability and void candidates). The defect is disclosure: the label
reads like a pause switch, there is no confirmation, and the success copy says only “Matching is
off” after `DELETE FROM availability` plus voiding open **and** matched plans.
**Fix:** state the consequence in the control and require an explicit confirm before posting
`matching:false`.

### A5. [P1] **[FIXED]** Unticking every ICS row reports “Saved. 4 windows.” — nothing was saved
`schedule.html:221-231` · `schedules.py:730-732,780` · **C**
An empty `pick` means *cancel*: the server returns 200 `{cancelled:true}` and the client prints
`state.windows` regardless.
**Fix:** branch on `data.cancelled`; disable Save-selected at zero selections.

### A6. [P1] **[FIXED]** Primary CTA is enabled while the backend is guaranteed to refuse
`card.html:34,221` · `scheduling.py:415` · **D**
“You can confirm once your agents agree” sits beside a live “Yes, let's do it” that returns
`409 activity_pending`.
**Fix:** disable `#yes` while `activity.state === "offered"`, and poll the card so it re-enables
itself.

### A7. [P2] **[FIXED]** A stalled turn has no cancel and no first-token watchdog
`chat.html:743-747,864-866` · `chat.py:1009-1011` · `ai.py:205-213` · **B**
`[corrected; was P1]` Tokens *do* stream and render live, and 60 s is a configurable HTTPX
timeout rather than an overall turn deadline. The real gap is the absence of a Cancel control and
of any watchdog if the **first** token never arrives; the only exit is navigating away.
**Fix:** add Cancel beside Send (wired to `abortController.abort()`) and a ~10 s watchdog line.
Cancellation copy must **not** promise rollback — an aborted read is not proof the server stopped;
reconcile from history afterwards and keep `[DONE]`-gated execution intact.

### A8. [P1] **[FIXED]** Confirm outcomes can render off-screen and are never announced
`chat.html:32,32-42` · **B, E**
`[corrected]` The transcript has a *max*-height, so the separation from the Confirm button is
conditional on a long conversation — not guaranteed. What always holds: the status line has no
`role="status"`/`aria-live`, so a screen-reader user is told nothing about a Confirm outcome, and
on a phone with a long thread the result can be outside the viewport.
**Fix:** move `#chat-status` above the composer and give it `role="status" aria-live="polite"`.

### A9. [P2] **[FIXED]** An expired preview keeps a live Confirm button
`chat.py:844-849,1248-1256` · `chat.html:36-42` · **B, E**
`[corrected; was P1]` Expiry *is* enforced — the click cannot apply stale data; `_latest_pending`
just doesn't filter it, so a dead preview is still offered until the hourly sweep.
**Fix:** filter `expires_at > now` in `_latest_pending`; render “expires at HH:MM” and disable
Confirm past it.

### A10. [P2] **[FIXED]** An ICS preview cannot be confirmed from Chat, and Chat says it is empty
`chat.py:1248-1251` · `chat.html:213-227,290-292,346-348` · **missed by all five lanes**
`/api/chat/history` assumes `items` is a dict of groups; the ICS preview stores a list, so it is
replaced with empty groups. Chat then renders “This preview has no items” with no checkboxes —
the advertised cross-surface confirmation dead-ends (the Schedule page can still confirm it).
**Fix:** normalise list-shaped items into `{"windows": [...], "events": []}` on read, and route
`kind='ics'` Confirm to `/api/schedule/ics/confirm` from Chat as the UI contract already says.

---

## B. Timing / staleness

### B1. [P1] **[FIXED]** Nothing updates by itself anywhere
`dashboard.html:66-86` · `card.html:326-341` · `matches.html:52-68` · `chat.html:658` · **D, E, B**
Single fetch per page: the other person's confirm, a counter, a decline, a worker-written note or
a new card are all invisible until a manual reload. `/api/inbox` exists and no template uses it.
**Fix:** poll the read endpoints + chat history every 15–30 s while visible; re-render on change.

### B2. [P2] **[FIXED]** Double-submit windows on every action
`card.html:242-262,269-282,303-321` · `schedule.html:131-157` · `settings.html:70-88` ·
`register.html:44-48` · **D, E, C, A**
`setBusy(false)` runs in `finally` while navigation waits 900 ms; Save/Create/Sign-in never
disable. A second tap creates a new proposal or grant version.
**Fix:** keep actions disabled until navigation; in-flight guard; reject an identical
resubmission server-side.

### B3. [P2] **[FIXED]** Uploading an `.ics` destroys a pending chat preview
`schedules.py:498` · **C** — `DELETE FROM chat_pending WHERE user_id=?` is unscoped and kills a
`kind='windows'` preview. **Fix:** scope to `kind='ics'` or 409 with a “finish the pending
preview first”.

### B4. [P2] **[FIXED]** Confirmed plans vanish from /matches when availability lapses
`scheduling.py:230-236` · `statewrite.py:145` (7-day TTL) · **D, E**
**Fix (amended):** do **not** retain identifiable plan rows past availability expiry —
`CONTRACT-photos.md:106-109` requires live availability for Matches authorization. The
contract-safe correction is an anonymous notice (“your saved free time expired — re-add it to
see your plans”).

### B5. [P2] **[FIXED]** Overnight state changes happen with no notice to either side
`sweep.py:69-91` · **D**
`[corrected]` The sweep does not delete confirmed plans: it voids expired open candidates and
`proposed`/`a_confirmed` proposals, and transitions completed `both_confirmed` plans to `closed`.
The defect is that a voided proposal or candidate simply disappears with no message.
**Fix:** surface a “this plan expired — open the card again” item instead of quiet disappearance.

### B6. [P2] **[FIXED]** The negotiation worker can answer under the user's live Accept button
`chat.html:609-633` · `negotiate.py` 15 s tick · **B**
**Fix:** poll the panel and re-check the offer state immediately before posting.

### B7. [P2] **[FIXED]** Dashboard fabricates a match card on 401/network failure
`dashboard.html:23-25,66-86` · **A, D, E**
`[corrected]` The fixture has a null id, so it renders “Match card” (not “#12”) with a fabricated
“Thu 19:00–21:00 · open” pill; the explanatory note is suppressed whenever the list is non-empty,
so a student with a dead session sees an invented time slot.
**Fix:** delete the fixture; 401 → `/login?next=/`; other failures → notice + Retry.

### B8. [P2] **[FIXED]** Removing a schedule row mid-save leaves screen and server disagreeing
`schedule.html:114,133,148-151` · **C** — **Fix:** disable row editing while saving.

### B9. [P2] **[FIXED]** Any offer-response failure wipes the Counter/Pass form just filled in
`chat.html:562-566,611,437-500` · **B** — **Fix:** refresh only on success.

### B10. [P2] **[FIXED]** “Clear history” during a stream blocks invisibly
`chat.html:840-857` · `chat.py:1549-1551` · **B** — **Fix:** disable while a turn is in flight.

---

## C. Truthfulness of what the UI says

### C1. [P2] **[FIXED]** A rejected send leaves a bubble that was never stored and destroys the typed text
`chat.html:748,786,835` · `chat.py:1284-1290` · **B** — the user row is appended optimistically
and the textarea cleared before the POST; on 409 only the assistant row is removed, and a ledger
row is spent before the `turn_busy` check.
**Fix:** restore the text and drop the bubble on failure; lock before reserving.

### C2. [P2] **[FIXED]** Timezone select ignores both the browser zone and the saved zone
`schedule.html:13-23,57-64` · `router.py:398` · **C, E** — hard-coded `America/Los_Angeles` with
7 zones; the browser zone is applied only if already listed; `tz_default` is passed and never
rendered. Windows get stored in the wrong zone with no warning.
**Fix:** insert the browser zone as an option; fall back to `tz_default`.

### C3. [P2] **[FIXED]** Raw identifiers, internal state words and UTC leaked to students
`chat.html:504` (“Match #id”) · `dashboard.html:34,41` (“Match card”, pill prints `open`) ·
`card.html:140-141` (raw `2026-…Z → …Z`) · `chat.html:9,16-17` (`Model:`, `AI_API_URL`) ·
`settings.html:35-36` (`DELEGATION_DEFAULT_ON`) · **E**

### C4. [P2] **[FIXED]** Friendly error branches are unreachable, so backend prose is shown verbatim
`base.html:48-55` · `register.html:70-76` · **A, E**
`[corrected]` For flattened `{error, detail}` responses `apiError` returns the `detail` **string**
first, so the user reads backend prose (e.g. “a valid registration code is required”), not a raw
code; and branches testing `data.detail.error` can never fire.
**Fix:** test `data.error` at the top level and map known codes to sentences.

### C5. [P2] **[FIXED]** Block / Report / No-thanks all report “Done.”, then the redirect wipes it
`card.html:253-254,288-292` · `scheduling.py:520,531-534,551-556` · **D**
Block is mutual and retained for months; the user is told “Done.”
**Fix:** distinct outcome copy per action, kept on the destination page.

### C6. [P2] **[FIXED]** Dashboard rows show neither person nor time
`scheduling.py:172-179` · **D** — payload is `candidate_id/card_url/state`, so every row reads
“Match card · Window available on the card”. **Fix:** return and render `about.display_name` +
`window.display`; drop the state pill.

### C7. [P2] **[FIXED]** “Suggest another time” opens empty pickers in a possibly-wrong zone
`card.html:44-61,144` · **D** — no prefill from `data.window`, no `min`, end not tied to start.

### C8. [P2] **[FIXED]** A Google/Outlook export is refused with a multi-KB semicolon wall
`schedules.py:373,330-333` · `schedule.html:208` — per-event TZID errors joined into one
paragraph. **Fix:** per-property counts plus the first three indices, collapsible.

### C9. [P3] **[FIXED]** Accepting an offer is a dead end (no link to the card/matches)
`chat.html:551-553,519` · **B**

---

## D. Accessibility & mobile

### D1. [P2] **[FIXED]** The transcript is `aria-live` (re-announced per token); the status line has none
`chat.html:33-34,683-695` vs `chat.html:32` · **B, E**

### D2. [P2] **[FIXED]** Sticky multi-row header on phones; nav has no active state
`style.css:64-75,321-326` · `base.html:12-21` · **A, E** — 2–3 row stack at ≤560 px, no
`aria-current`, “Sign out” ~1.2 rem tap target.

### D3. [P2] **[FIXED]** Register page starts below the fold on a 380 px phone
`register.html:5-17` · `style.css:313-319` · **A**

### D4. [P2] **[FIXED]** Most async results unannounced; busy state invisible
`settings/dashboard/card/matches` notice nodes lack `role=status`; `setBusy` sets only `disabled`
· **A, C, D**

### D5. [P3] **[FIXED]** Remove controls are ~17–19 px tap targets (chat Clear, schedule Remove)
`style.css:185` · `chat.html:30` · `schedule.html:110-114` · **B, C**

### D6. [P3] **[FIXED]** Settings photo panel forgets the published photo after reload; file input unlabelled
`settings.html:12-16` · `router.py:390-396` · **A, E**

### D7. [P2] **[FIXED]** The 8-character password rule is enforced on neither side
`register.html:18,26` (`novalidate`) · `router.py:195-203` · **A**

### D8. [P3] **[FIXED]** Deleting the account lands on a blank sign-in page where the credentials then “fail”
`settings.html:129-133` · `sessions.py:31-39` · `scheduling.py:698-704` · **A**

---

## What is genuinely good (so we don't "fix" it)

- Chat's sticky error/conflict flag and its `status.hidden` guard: correct and tested.
- Preview→Confirm provenance, the grant firewall, and `[DONE]`-gated execution: honest and they
  hold under review.
- Schedule's reject-never-ignore parser: correct refusals; the problem is presentation (C8).
- Card copy for the one-sided confirm: honest. Only its destination is wrong (A2).
- Leave-matching's cascade: intentional and contract-required. The defect is disclosure (A4).

## Original execution order (review-endorsed) — all of it is now complete

The order below is the sequence the waves actually ran in. Every item in it is done; the two
later gates added rework inside steps 1 and 2, not new scope.

1. **Demo-breaking:** A1, A3, A2, A4 disclosure, A5, B7, A10 (the ICS→Chat dead end).
2. **Liveness:** A6, B1 — buttons that cannot work and screens that never update.
3. **Data/copy integrity:** B2, B3, C1, C2, C3, C4, C5.
4. **Recoverable polish:** A7, A9 timeouts/expiry, then D1–D7 (cheap, and the difference between
   “demo” and “usable”).
