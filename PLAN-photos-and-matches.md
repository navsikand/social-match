# Plan v2 — confirmed-matches list + card photos

Revised after an adversarial plan review (FIX-PLAN, 11 findings). No implementation yet. Supersedes v1 of this
file; the review's fixes are integrated inline and summarised in §0.

Scope: **A. `/matches` returns real data** (it dead-ends on "the match list service isn't connected yet") and
**B. an explicit photo-publish action for the swipe card** (preset avatars only today).

---

## 0. What the review changed

| # | v1 said | v2 does |
| --- | --- | --- |
| 1 | Photo served to anyone sharing *any* candidate | **Same live-card authorization as `card()`, plus photo consent, re-checked every request.** Declined/hidden/voided/closed/expired never qualify |
| 2 | Matches gated on blocks + matching only | **The full §7 authorization table** before rendering a row; `wording.label` renders, it does not authorize. `matching_live` stays |
| 3 | Closed plans listed with a card link | Closed plans are **read-only history**: listed only while authorization holds and within 30-day retention, no actionable link, no withdraw button |
| 4 | "third tier or a separate boolean" | **One explicit action: "Publish this photo on my card."** Revision-bound, independent of name, no forgotten toggle, never implied by `card+name` |
| 5 | Hand-rolled JPEG/PNG metadata surgery | **Re-encode through a real decoder (Pillow).** Hand-parsing was unsound: orientation, trailing bytes, APP14 colour interpretation, and "valid header" ≠ "decodable image" |
| 6 | `request.body()` then size check | **Authenticate first, stream with a running byte cap**, plus proxy cap and a per-account upload rate limit |
| 7 | Atomic file write, done | **Serialized per-account publication/deletion**, revision-specific filenames, liveness re-check, retryable orphan cleanup, DB access revoked even if unlink fails |
| 8 | "people you match with" | **"Signed-in people receiving your live candidate card — before either person confirms."** Plus FLOW §§2/7/10–12 and privacy updates; visible identifying content and screenshots stated plainly |
| 9 | `private, max-age=60` | **`no-store`** on photos and sensitive JSON, including error responses |
| 10 | `table_info` + `ALTER` | **Serialized inside `BEGIN IMMEDIATE`**, tested fresh/existing/repeated/concurrent; one provisioned upload path verified under the service identity and SELinux |
| 11 | Response `about`, template reads `display_name` | Template contract updated to consume `label` + `state`; rendered-behaviour tests; dates on historical windows |

**New dependency:** `pillow` (`uv add pillow`). Justified by finding 5 — the alternative was to defer B entirely.
It buys a genuine decode check, EXIF orientation, pixel caps and metadata-free re-encoding in ~20 lines that a
hand-rolled parser cannot match safely.

**Ship order:** A first, on its own, before any photo migration.

---

## A. Confirmed matches

### Problem
`server/webui/templates/matches.html` fetches `GET /api/matches`; the route was never written.

### Definition
A row qualifies when **all** of these hold at read time:

1. the candidate involves the viewer and is `state IN ('matched','closed')` — `hidden` (reported), `declined`,
   `voided` never appear;
2. **both users' matching grants are live** (`wording.matching_live`);
3. **the counterpart's `publication · card` grant is live** — the same gate the card uses;
4. **both users' availability is unexpired** (FLOW's live-availability rule — a stale pair stops being rendered);
5. **not blocked**, either direction;
6. a current proposal exists that is `both_confirmed` (or `closed` for history).

Only after those pass do we call `wording.label(conn, about_uid)` — that function decides *name vs pseudonym*;
it does not decide *whether to render the person at all*.

### Closed rows are history, not actions
- Included only while the checks above hold, and only inside the existing 30-day retention window.
- Rendered with a date (`Thu 19:00–21:00 · 25 Oct`) so repeating weekdays are distinguishable.
- **No card link, no withdraw button** — `card()` rejects closed candidates, so linking would hand the user a dead
  end. History is read-only by design; withdrawing a past plan is not a thing.
- Upcoming vs historical is classified **at read time** (window_end vs now), not by waiting for the sweep — the
  sweep may not have run.

### API
`GET /api/matches` (session) →

```jsonc
{"matches": [
  {"candidate_id": 12,
   "about": "Ava",                 // wording.label, after the table above passes
   "window": {"start_utc": "...", "end_utc": "...", "tz": "...",
              "display": "Thu 19:00–21:00", "display_with_date": "Thu 19:00–21:00 · 25 Oct"},
   "state": "matched",             // matched (upcoming) | closed (past, read-only)
   "historical": false,
   "confirmed_at": "...",
   "card_url": "https://.../m/12"  // null when historical
  }
]}
```

Order: upcoming first (soonest), then historical (most recent). Empty list is a normal 200.

### Template
`matches.html` is rewritten to consume exactly this shape (`about`, `window.display_with_date`, `state`,
`card_url`), with a real empty state ("No confirmed plans yet.") and an honest error line. The 404
"service isn't connected" branch is deleted.

### Tests
Two-sided confirm appears for both; `proposed`/`a_confirmed` absent; blocked pair absent both directions;
counterpart withdraws matching → row gone; counterpart withdraws **card publication** → row gone; availability
expires → row gone; `hidden` (report) → row gone; superseded proposal (counter) → row reflects the current
proposal only; past window → `historical: true`, `card_url: null`; unauthenticated → 401 and page redirect.

---

## B. Photo publish

### B1. Consent = one explicit action, revision-bound
There is no new settings toggle and no fourth tier. The upload control itself is the consent:

> **Publish this photo on my card**
> Your photo is shown to signed-in people who receive your live candidate card — before either person confirms.
> Not to the public, not to anyone you haven't been matched with. You can remove it any time.
> *(preview of the exact image that will be stored)*

- Absence of a photo = off. Nothing is published implicitly.
- **Replacement counts as a new publication**: uploading a new image requires the same explicit action, because
  it is a different image about to be shown to strangers.
- Removal withdraws immediately (byte-level access gone, not just the field).
- **Independence from name:** photo publication must never displace or be displaced by `card` / `card+name`. The
  existing publication upsert withdraws the previous publication row, so photo permission is **not** a `grants`
  tier — it is stored against the photo revision (below).
- **Relationship to the skill payload:** `POST /api/state` neither grants nor withdraws photo permission, and a
  state resend **preserves** it (photo consent is web-only, because only the web UI can show the preview and
  audience copy that make it informed consent). This is documented in FLOW §2 so the consent story stays honest
  rather than implying every disclosure flows through one payload.

### B2. Data model
```sql
-- users gains three nullable columns (idempotent migration, §B9)
photo_path        TEXT     -- 'sha-free' filename relative to UPLOAD_DIR, revision-specific
photo_revision    INTEGER  -- increments on every successful upload
photo_published_at TEXT    -- non-null only while that revision is authorized for display
```
Gate for *display/serving*: `photo_path IS NOT NULL AND photo_published_at IS NOT NULL` **and** the stored file
exists for the revision named in `photo_path`. (`photo_path` always names the current revision's file, so a
replacement cannot leave the old bytes addressable.)

Revisions are what make an old image unreachable the moment it is replaced: filenames are
`{uid}-{revision}.jpg`, so a replacement writes a new name and the old bytes are unreferenced (then cleaned, §B8).

### B3. Upload
`POST /api/me/photo` (session, `Content-Type: image/jpeg|image/png`, **raw body** — no multipart, so no
`python-multipart` dependency; the review confirmed multipart buys nothing here).

Pipeline, in this order:
1. **Authenticate, then check the account is live** (`deleted_at IS NULL`) — before reading any body bytes.
   This is an early rejection only; it is **not** the authoritative check (see step 7).
2. **Stream `request.stream()` with a running cap of 5 MB**; abort mid-stream past the cap → `413`. Never trust
   `Content-Length` (missing, lying, or absent under chunked encoding).
3. **Per-account rate limit**: 5 uploads / 10 minutes, counted in SQLite (`photo_uploads` rows or reuse
   `consent_log`), `429` beyond it. Prevents a cheap storage/CPU DoS.
4. **`PIL.Image.open` + `verify()`, then reopen and decode** — this is the decode check the review demanded.
   Reject anything that fails → `415`.
5. **Pixel cap** 8000×8000 *and* a decompression-bomb guard (`Image.MAX_IMAGE_PIXELS`), checked before
   `load()` → `413`.
6. **Normalise onto a fresh, pixel-only image.** `ImageOps.exif_transpose(img)` (orientation applied, then the
   EXIF is irrelevant), convert to RGB (or RGBA for PNG alpha), `thumbnail()` to ≤1600 px, then **build a new
   Image and copy pixels into it** (`Image.new(mode, size)` + `.paste()`). The copy starts with an empty `.info`,
   which is the point: passing no encoder arguments is *not* enough, because a transformed Pillow image can still
   carry `info` keys (`exif`, `icc_profile`, `comment`, …) that the encoder will happily write back out. Save the
   copy with no metadata arguments of any kind (`JPEG q85` / `PNG optimize`).
7. **Re-read `deleted_at` inside the `BEGIN IMMEDIATE` transaction**, immediately before publishing. An account
   deleted while the body was streaming must not end up with a servable photo — `delete_account` tombstones the
   row rather than removing it (`server/scheduling.py:548–561`), so the check is a plain column read, and it is
   the one that counts. If the account is gone: discard the temp file, write nothing, return 401.
   Otherwise write `{uid}-{rev}.jpg|png` via temp file + `os.replace`, mode `0640`, in that same transaction so
   the row and file cannot disagree.
8. Set `photo_path`, `photo_revision = rev+1`, `photo_published_at = now`; log `consent_log: photo:published`
   with `{revision}`.

`DELETE /api/me/photo` → `photo_published_at = NULL` (+ file unlink attempt) **first**, so access is revoked even
if the unlink fails; then best-effort delete; log `photo:withdrawn`.

Why re-encoding rather than byte surgery: EXIF/GPS/XMP/PNG text chunks all disappear by construction, orientation
is baked into pixels, a bomb cannot pass the pixel cap, and the stored bytes are guaranteed decodable — none of
which a header-walking parser can promise.

### B4. Serving
`GET /media/photo/{user_id}` (session required, **`no-store`**). Two layers, and the first applies to everyone:

**1. Common gate (self included):** the account is live (`deleted_at IS NULL`), `photo_published_at` is
non-null, and the file named by `photo_path` exists. **Self-preview bypasses only layer 2 — never this.** So after
the owner removes the photo, the owner's own URL also returns 404: withdrawal is withdrawal.

**2. Non-self gate** — the same live checks as `card()` (`server/scheduling.py:181–206`) for a candidate between
viewer and `user_id`:
   - candidate state `open`/`matched` only (declined, hidden, voided, closed → **404**);
   - a **current proposal exists** and the candidate is **not expired** (`card()` rejects both);
   - both matching grants live; `user_id`'s `publication · card` live; both availabilities unexpired; not blocked.

Withdrawing card publication therefore stops strangers but still leaves the owner's own preview working — the
owner is looking at their own image, not at a disclosure.
- **`404` for every refusal** (no photo, not yours, authorization lapsed) so the endpoint cannot be used to
  probe whether a given user has a photo or a card. There is no `403` on this route.
- Headers: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`,
  `Content-Type` from the stored extension. No static mount, no filename addressing, no public URL.
- **Re-checked on every request**: an image already viewed cannot be recalled, but a withdrawn or replaced photo
  must stop being fetchable immediately. `no-store` (finding 9) is what makes that true in practice.

### B5. Card
`about.photo_url` is emitted only when §B4's non-self checks would pass; otherwise absent and the card falls back
to the preset avatar. The card page also states, once, that the photo is shown only to matched people.

### B6. Surfaces and copy
- **Settings**: the publish control in §B1 (preview, publish, remove).
- **Card**: photo or avatar, plus the one-line disclosure.
- **Privacy policy**: replace "no photo upload" with what is now true — one photo per account; ≤5 MB; JPEG/PNG;
  decoded, orientation-applied, resized, **re-encoded with all metadata removed**; served only to signed-in
  people holding a live card from you, before either side confirms; `no-store`; deleted on removal and with the
  account; **the image itself may contain identifying content (a face, a name tag, a room) and cannot be
  moderated automatically; screenshots cannot be prevented.**
- **FLOW.md**: §2 grant table gains the photo publication act; §7 authorization list gains it; §10/§11 model and
  deferred list updated; §12 demo gains one beat ("publish a photo, see it on the card, remove it, card falls
  back to the avatar").
- **Terms**: add the one-line reality — you are responsible for what your photo shows; reporting is the moderation
  path, and the named team member responds.

### B7. Deletion
- `DELETE /api/me/photo`: revoke → unlink → log (revoke first).
- `delete_account`: inside the existing transaction set `photo_path/photo_published_at = NULL` first, then
  best-effort unlink **after commit** (SQLite cannot roll back filesystem work). Orphan cleanup (§B8) covers a
  crash between the two.
- Account deletion always wins: even if unlink fails, the DB no longer authorizes serving.

### B8. Orphan cleanup
A `sweep` step deletes files in `UPLOAD_DIR` that are **unreferenced AND older than a one-hour grace window** —
both conditions, never either one alone (a file older than an hour may still be the live photo of a quiet user;
an unreferenced file may be a publication still in flight). Reference-checking and deletion run in the same
`BEGIN IMMEDIATE` that publication uses, so a file cannot be judged orphaned while an upload is publishing it.
Retryable, idempotent, logged. This is the answer to "upload raced deletion" and to replaced revisions.

### B9. Migration and deployment (finding 10)
- `db.init_db()` runs the column additions **inside `BEGIN IMMEDIATE`** with an `PRAGMA table_info` re-read, so
  two starting processes cannot both try to `ALTER`. Tests: fresh DB, pre-existing DB (simulate by creating the
  old table first), repeated startup, and two initializers racing.
- **One deployed path**, decided now: `UPLOAD_DIR=/home/nav/devhacks3/track2-study-match/uploads`, which already
  sits inside the unit's `ReadWritePaths`. Verified by writing/reading/deleting a file **as the deployed process**
  on the box, not assumed; check `ausearch`/`journalctl` for SELinux denials after the first upload (this host is
  `Enforcing`, and the `bin_t` vs `user_home_t`/`container_file_t` lesson from the service install applies to a
  new writable directory too).

### B10. Tests
1. Publish PNG → 200; file `{uid}-{rev}.png` exists; `photo_published_at` set.
2. Publish a JPEG carrying EXIF `GPS`, an ICC profile, a `comment` and a PNG with `tEXt`/`eXIf`: reopen the
   **stored** bytes and assert structurally that `image.getexif()` is empty, `image.info` contains none of
   `exif`/`icc_profile`/`comment`/`pnginfo`/`text`, **and** the image decodes (not just "bytes don't contain the
   string GPS" — a substring search is not a metadata assertion).
3. EXIF orientation 6 → stored pixels are physically rotated (no orientation tag relied upon).
4. 1600 px cap: 4000×3000 in → ≤1600 long edge out.
5. `text/plain` with a `.jpg` header → 415; truncated JPEG → 415; 20000×20000 claimed PNG → 413.
6. 6 MB body → 413; **missing `Content-Length`** streaming a 6 MB body → 413 (cap enforced mid-stream).
7. Upload rate limit → 429 on the 6th inside the window, driven by an **injected clock** (`db._FIXED_NOW`), not
   `sleep`; include the boundary case at exactly the window edge.
8. Unauthenticated → 401 before any body read (assert no file written).
9. Serving: self 200; counterpart-with-live-card 200; **after counterpart withdraws matching → 404**; after
   `DELETE /api/me/photo` → 404; after block → 404; declined candidate → 404; hidden candidate → 404; anonymous →
   401; headers include `no-store`.
10. Replacement: new revision filename, old file unreferenced, old URL path (`{uid}`-addressed) now serves the
    **new** image, not the cached old one (with `no-store` the client must re-fetch).
11. Concurrency: upload vs delete-account, **both orderings**, threads + `threading.Barrier` + separate
    `db.connect()` connections (the idiom `tests/test_concurrency.py` already uses) — end state is either a live
    photo for a live account or no photo for a deleted account, never a servable photo for a deleted user; type
    change jpg→png leaves no stale sibling; injected unlink failure still revokes serving (serving is gated by
    the DB, not by the file).
12. Orphan cleanup removes unreferenced files, keeps referenced ones.
13. Matches page renders confirmed rows from real data (template contract), and the historical row has no link.

---

## C. Order of work

1. **A** — endpoint + template + tests. Ships alone; touches no schema.
2. Migration (3 columns, serialized) + `UPLOAD_DIR` provisioning verified on the host.
3. `uv add pillow`; `server/photos.py` (validate/normalise/store) + unit tests, no HTTP yet.
4. Upload/delete endpoints (streamed caps, rate limit, revision publish).
5. Serving endpoint + authorization tests (the leak surface — test before UI).
6. Card + settings UI, card disclosure line.
7. Docs: privacy, terms, FLOW §§2/7/10–12/§12 demo.
8. rsync → restart → verify on `devhacks.navraj.me` with a real JPEG (check `journalctl` for SELinux denials) →
   push both repos.

## D. Cut (per review)

Custom image sanitisation by hand (replaced by Pillow re-encode), a separate photo toggle, galleries, cropping,
CDN, automated NSFW moderation, server-side thumbnails beyond the single 1600 px normalise. **If safe decoding
or on-host deployment verification can't fit, B is deferred rather than shipped half-verified.**

## E. Residual risks (stated, not mitigated)

- The image itself can identify someone (face, name tag, room). No automated moderation exists; reporting hides
  the pair and a named team member responds.
- Screenshots cannot be prevented once a card is rendered.
- An already-viewed image cannot be recalled; only future fetches stop.
- Uploads cost disk and CPU: capped (5 MB, 1600 px, rate-limited) but not quota-limited per account.

## E2. Review status

Round 1: **FIX-PLAN**, 11 findings. Round 2: 7 closed + 4 blockers (common photo gate for self, liveness re-read
inside the publication transaction, pixel-only image boundary, cleanup = unreferenced **and** past grace). Round 3,
blockers only: **all four CLOSED → APPROVE-PLAN**. No code written; this document is the approved plan.

## F. Open questions still needing a human

1. Who is the named report responder for the deployed demo? (Goes in privacy + terms.)
2. Does the hackathon deployment need an account-level storage quota, or is 1 photo × ≤1600 px × N accounts
   acceptable?
