# Build contract — photos + matches (phase B/C)

Authoritative for this increment. `PLAN-photos-and-matches.md` is the approved design; this file is the *interface*
so parallel agents never guess. Where they disagree, the plan wins on behaviour, this file wins on signatures.

## Ownership

| Files | Lane |
| --- | --- |
| `server/scheduling.py` (matches endpoint, `card()` photo_url, `delete_account` photo columns), `server/webui/templates/matches.html`, `tests/test_matches.py` | **A** |
| `server/photos.py`, `tests/test_photos_unit.py` | **B-core** |
| `server/photo_api.py`, `tests/test_photo_api.py` | **B-api** |
| `server/webui/templates/settings.html`, `server/webui/templates/card.html`, `server/webui/static/style.css` (append only), `tests/test_photo_ui.py` | **B-ui** |
| `docs/privacy.md`, `docs/terms.md`, `FLOW.md` | **DOCS** |

Frozen (integrator only): `schema.sql`, `server/db.py`, `server/config.py`, `server/app.py`, `server/wording.py`,
`server/sessions.py`, `tests/conftest.py`, `tests/util.py`, `pyproject.toml`.

Never edit another lane's files. Never add dependencies (pillow is already installed). Use `uv run ...`; never
`uv sync`/`uv add`/`uv lock`.

## 1. `server/photos.py` — pure image pipeline (no DB, no filesystem, no FastAPI)

```python
MAX_UPLOAD_BYTES = 5 * 1024 * 1024      # 5 MB, enforced by the endpoint while streaming
MAX_INPUT_PX     = 8000                 # either dimension, before decode
MAX_OUTPUT_PX    = 1600                 # long edge of what we store
JPEG_QUALITY     = 85

class PhotoRejected(Exception):
    code: str    # 'too_large' | 'unsupported' | 'too_big' | 'corrupt'
    detail: str

def normalise(data: bytes) -> tuple[bytes, str]:
    """Validate and re-encode. Returns (image_bytes, 'jpg'|'png').

    Steps, all mandatory: PIL.Image.open → verify() → reopen → check dimensions against
    MAX_INPUT_PX and Image.MAX_IMAGE_PIXELS → load() → exif_transpose → convert
    (RGB, or RGBA when the source has alpha) → thumbnail(MAX_OUTPUT_PX) → build a FRESH
    Image.new(mode, size) and paste pixels into it (so .info is empty) → save with no
    metadata arguments at all → return bytes + extension.

    Raises PhotoRejected('unsupported') for anything that is not a decodable JPEG/PNG,
    ('too_big') for a dimension/pixel-cap violation, ('corrupt') for a decode error,
    ('too_large') if the caller passes more than MAX_UPLOAD_BYTES.
    """
```

**Test expectations (assert these, they are the privacy promise):** reopen the returned bytes and check
`img.getexif()` is empty and `img.info` has none of `exif`, `icc_profile`, `comment`, `pnginfo`, `text`; check
orientation 6 input comes out physically rotated; check a 4000×3000 input is ≤1600 on the long edge.

## 2. `server/photo_api.py` — storage, publication, serving

Router object named `router`, mounted in `app.py` (already done). Paths are absolute (no prefix).

```python
router = APIRouter(tags=["photos"])

def card_photo_url(conn, viewer_uid: int, about_uid: int) -> str | None
    """Authorization read used by scheduling.card(). Returns '/media/photo/{about_uid}' only when
    the non-self gate passes, else None. Must be cheap and side-effect free."""
```

Endpoints:

| Method | Path | Auth | Behaviour |
| --- | --- | --- | --- |
| `POST` | `/api/me/photo` | session | raw body, publish |
| `DELETE` | `/api/me/photo` | session | withdraw |
| `GET` | `/media/photo/{user_id}` | session | serve bytes |

Upload (`POST`), in this order:
1. session check, then `users.deleted_at IS NULL` — **early reject only**; 401 if gone.
2. stream `request.stream()` accumulating into a buffer, abort as soon as the total exceeds
   `photos.MAX_UPLOAD_BYTES` → `413 {"error":"too_large"}`. Never trust `Content-Length`.
3. rate limit: at most **5 uploads per 10 minutes** per user. Count rows in `consent_log` with
   `action='photo:published'` and `created_at > now - 10min` → `429 {"error":"rate_limited"}`.
4. `photos.normalise(body)`; map `PhotoRejected.code`: `too_large`→413, `too_big`→413,
   `unsupported`/`corrupt`→415, body `{"error": <code>, "detail": ...}`.
5. `with db.immediate(conn):` **re-read `deleted_at`** (the authoritative check) → 401 if gone;
   `rev = photo_revision + 1`; write `{uid}-{rev}.{ext}` to `config.upload_dir()` via temp file +
   `os.replace` (mode 0640); `UPDATE users SET photo_path=?, photo_revision=?, photo_published_at=?`;
   insert `consent_log` row `photo:published` with `{"revision": rev}`; after commit, best-effort unlink
   of any older `{uid}-*.{jpg,png}` file that is no longer referenced.
6. `200 {"ok": true, "photo_url": "/media/photo/{uid}", "revision": rev}`.

Delete (`DELETE`): `UPDATE users SET photo_published_at=NULL` **first** (revocation is the DB, not the file),
then best-effort unlink, log `photo:withdrawn`, `200 {"ok": true}`.

Serve (`GET /media/photo/{user_id}`), **404 for every refusal** (no 403, so it cannot be used to probe):
- session required (401 if not signed in);
- **common gate**: target account not deleted, `photo_published_at IS NOT NULL`, `photo_path` set, file exists;
- **self** → serve (bypasses only the candidate/grant checks below);
- **non-self** → also require: a candidate between viewer and target with `state IN ('open','matched')` and
  `expires_at > now`; a current non-void proposal for it; `wording.matching_live` for the pair;
  `wording.live_grant(target,'publication','card')` non-null; both availabilities live
  (`expires_at > now`); not `wording.blocked_pair`.
- Headers: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`.
- Implementation: return `Response(content=bytes, media_type=...)` (read the file inside the handler).

Media type from the stored extension: `.jpg` → `image/jpeg`, `.png` → `image/png`.

## 3. Lane A — `GET /api/matches` + `card()` photo field

`GET /api/matches` (session) → `{"matches": [...]}` — qualify only when **all** hold:
candidate involves viewer and `state IN ('matched','closed')`; `wording.matching_live`; counterpart has a live
`publication · card` grant; both availabilities unexpired; not blocked; a current proposal exists with
`state IN ('both_confirmed','closed')`.

Row: `{"candidate_id", "about": wording.label(conn, about_uid), "window": {"start_utc","end_utc","tz",
"display": wording.fmt_window(...), "display_with_date": "Thu 19:00–21:00 · 25 Oct"},
"state": "matched"|"closed", "historical": bool, "confirmed_at", "card_url": str|None}`.
`historical` = `window_end < now`; `card_url` is `None` when historical. Order upcoming first, then most recent.

`card()` gains one key: `"photo_url": photo_api.card_photo_url(conn, uid, about_uid)` (may be None).
The stub returns None today; the real implementation lands in B-api. **Import defensively**:
`from server import photo_api` at module top is fine (the module already exists).

`delete_account` also sets `photo_path=NULL, photo_published_at=NULL` inside its existing transaction.

`matches.html` is a client-rendered page: fetch `/api/matches`, render `about`, `window.display_with_date`,
`state`, and an "Open card" link only when `card_url` is non-null. Empty state "No confirmed plans yet.".
Delete the 404 "service isn't connected" branch. Keep the existing `show()/postJSON` helpers style.

## 4. Lane B-ui — settings + card

**Settings**: a "Your photo" panel — file input (`accept="image/jpeg,image/png"`), preview canvas, and buttons
*Publish* / *Remove*. The publish copy is exactly:

> **Publish this photo on my card** — Your photo is shown to signed-in people who receive your live candidate
> card, before either person confirms. Not to the public, not to anyone you haven't been matched with. You can
> remove it any time.

Upload path: user picks a file → draw to canvas capped at **1600 px** long edge → `canvas.toBlob('image/jpeg',
0.85)` → `POST /api/me/photo` with `Content-Type: image/jpeg` and the blob as the body (no FormData). Show the
server's error text on failure (413/415/429). Remove → `DELETE /api/me/photo`, then clear the preview.

**Card**: when `data.about.photo_url` is present, render `<img>` (with the preset avatar as `onerror` fallback);
otherwise the existing avatar glyph. Add one line under the photo: *"Shown only to people who receive this card."*

Append styles only (never rewrite `server/webui/static/style.css`; add a small block at the end).

## 5. Lane DOCS

- `docs/privacy.md`: replace the "no photo upload" claim with the truth — one photo per account, ≤5 MB, JPEG/PNG,
  decoded and re-encoded with all metadata removed, shown to signed-in people holding a live card from you before
  either person confirms, never public, `no-store`, deleted on removal and with the account; add the honest
  limits: the image itself may identify you, there is no automated moderation, screenshots cannot be prevented.
  Add photos to the delete cascade list.
- `docs/terms.md`: you are responsible for what your photo shows; reporting is the moderation path and a named
  team member responds.
- `FLOW.md`: §2 gains the photo publication act (web-only; `/api/state` neither grants nor withdraws it, resends
  preserve it); §7 authorization list gains the photo gate; §10 model gains the three `photo_*` columns; §11
  deferred list drops "photo upload" and keeps "automated moderation"; §12 demo gains a beat (publish → card
  shows it → remove → avatar returns).

## 6. Integration gate

`uv run pytest` must be green for the full suite, and `uv run python demo/scenario.py` must still print
`ALL 12 BEATS PASS`. Lanes report: files touched, tests added, anything the contract left ambiguous.
