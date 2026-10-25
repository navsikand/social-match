"""Pages + register/login/settings. P1d owns this file (CONTRACT §11).

Card, swipe, counter, revoke, dashboard, matches, delete-account and pair endpoints are
owned by sibling phases; the pages here only *call* them through same-origin fetch and
degrade gracefully when they are absent.
"""
import json
import os
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import TemplateNotFound
from pydantic import BaseModel

from server import chat, config, db, passwords, scheduling, sessions

router = APIRouter(tags=["webui"])

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_HERE, "templates")
DOCS_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "docs"))

templates = Jinja2Templates(directory=TEMPLATES_DIR)

_AVATARS = range(1, 9)
_COOKIE = "smsession"
_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_PUBLICATION_TIERS = ("card", "card+name")


# --------------------------------------------------------------------------- bodies


class RegisterBody(BaseModel):
    email: str = ""
    password: str = ""
    display_name: str = ""
    avatar_id: int = 1
    adult_attested: bool = False
    registration_code: str = ""


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


class PublicationBody(BaseModel):
    tier: str = "off"


class GrantsBody(BaseModel):
    matching: bool | None = None
    publication: PublicationBody | None = None
    delegation: bool | None = None


# ------------------------------------------------------------------------ helpers


def _set_session(response: Response, uid: int) -> None:
    response.set_cookie(
        _COOKIE,
        sessions.sign_session(uid),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _current_uid(request: Request, conn) -> int | None:
    """Session uid that still maps to a live user row."""
    uid = sessions.session_user_id(request)
    if uid is None:
        return None
    row = db.one(conn, "SELECT id FROM users WHERE id=? AND deleted_at IS NULL", (uid,))
    return uid if row is not None else None


def _live_grant(conn, uid: int, kind: str):
    return db.one(
        conn,
        "SELECT * FROM grants WHERE user_id=? AND kind=? AND withdrawn_at IS NULL"
        " AND expires_at > ? ORDER BY version DESC LIMIT 1",
        (uid, kind, db.utcnow()),
    )


def _log_consent(conn, uid: int, action: str, kind: str, detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO consent_log (user_id_hash, action, scope, detail_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (db.user_hash(uid), action, kind, json.dumps(detail or {}), db.utcnow()),
    )


def _withdraw_grant(conn, row, uid: int, kind: str) -> None:
    conn.execute("UPDATE grants SET withdrawn_at=? WHERE id=?", (db.utcnow(), row["id"]))
    _log_consent(conn, uid, f"withdraw:{kind}", kind, {"version": row["version"]})


def _upsert_grant(conn, uid: int, kind: str, tier: str | None = None) -> None:
    """CONTRACT §7: withdraw the live row, insert version prev+1 (starting at 1)."""
    prev = _live_grant(conn, uid, kind)
    version = 1
    if prev is not None:
        _withdraw_grant(conn, prev, uid, kind)
        version = int(prev["version"]) + 1
    conn.execute(
        "INSERT INTO grants (user_id, kind, tier, version, fields_json, audience,"
        " granted_at, expires_at) VALUES (?,?,?,?,?,'matched_users_only',?,?)",
        (
            uid,
            kind,
            tier,
            version,
            json.dumps({"audience": "matched_users_only"}),
            db.utcnow(),
            db.utcnow(days=config.grant_ttl_days()),
        ),
    )
    _log_consent(conn, uid, f"grant:{kind}", kind, {"tier": tier} if tier else {})


def _seed_default_grants(conn, uid: int) -> None:
    """Demo box: signup seeds the delegation grant (version 1) + its consent row.

    With MATCHING_DEFAULT_ON it ALSO seeds the two grants matchmaking requires. Chat turns can
    never grant anything (grant-preserving by design), so without this a chat-only student is
    permanently unmatchable: "I have windows stored and I still see no matches".
    """
    if config.matching_default_on():
        _upsert_grant(conn, uid, "matching")
        # A card grant alone demotes labels to pseudonym; card+name is what a student expects
        # to see on their own demo. Both are still revocable from Settings at any time.
        _upsert_grant(conn, uid, "publication", "card")
    if not config.delegation_default_on():
        return
    conn.execute(
        "INSERT INTO grants (user_id, kind, tier, version, fields_json, audience,"
        " granted_at, expires_at) VALUES (?,?,?,?,?,'matched_users_only',?,?)",
        (
            uid,
            "delegation",
            None,
            1,
            json.dumps({"audience": "matched_users_only"}),
            db.utcnow(),
            db.utcnow(days=config.grant_ttl_days()),
        ),
    )
    _log_consent(conn, uid, "grant:delegation", "delegation", {})


def _snapshot_grants(conn, uid: int) -> dict:
    out: dict = {"matching": None, "publication": None, "delegation": None}
    for kind in ("matching", "publication", "delegation"):
        row = _live_grant(conn, uid, kind)
        if row is not None:
            out[kind] = {
                "version": row["version"],
                "tier": row["tier"],
                "granted_at": row["granted_at"],
                "expires_at": row["expires_at"],
            }
    return out


def _read_doc(path: str) -> str | None:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass
    return None


def _doc_page(request: Request, title: str, path: str, missing: str, template: str):
    body = _read_doc(path)
    return templates.TemplateResponse(
        request,
        template,
        {"title": title, "content": body or missing, "missing": body is None},
    )


# ------------------------------------------------------------------- auth endpoints


@router.post("/api/register", status_code=201)
def register(body: RegisterBody, response: Response, conn=Depends(db.get_conn)):
    required = config.registration_code()
    if required and body.registration_code.strip() != required:
        raise db.http_error(403, "invite_required", "a valid registration code is required")
    if not body.adult_attested:
        raise db.http_error(403, "attest_required", "you must confirm you are 18 or older")

    email = body.email.strip().lower()
    display_name = body.display_name.strip()
    if not email or not body.password or not display_name:
        raise db.http_error(422, "payload_forbidden", "email, password and display name are required")
    # D7: the 8-character rule is enforced here as well as in the browser (minlength=8).
    if len(body.password) < 8:
        raise db.http_error(422, "payload_forbidden", "password must be at least 8 characters")

    avatar_id = body.avatar_id if body.avatar_id in _AVATARS else 1
    try:
        with db.immediate(conn):
            if db.one(conn, "SELECT id FROM users WHERE email=?", (email,)) is not None:
                raise db.http_error(409, "email_taken", "an account with that email already exists")
            conn.execute(
                "INSERT INTO users (email, password_hash, display_name, preset_avatar_id,"
                " adult_attested_at, created_at) VALUES (?,?,?,?,?,?)",
                (
                    email,
                    passwords.hash_password(body.password),
                    display_name,
                    avatar_id,
                    db.utcnow(),
                    db.utcnow(),
                ),
            )
            uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            _seed_default_grants(conn, uid)
    except sqlite3.IntegrityError as exc:
        raise db.http_error(
            409, "email_taken", "an account with that email already exists"
        ) from exc

    _set_session(response, uid)
    return {"ok": True, "user_id": uid, "email": email, "display_name": display_name}


@router.post("/api/login")
def login(body: LoginBody, response: Response, conn=Depends(db.get_conn)):
    email = body.email.strip().lower()
    row = db.one(
        conn,
        "SELECT id, password_hash FROM users WHERE email=? AND deleted_at IS NULL",
        (email,),
    )
    if row is None or not passwords.verify(body.password, row["password_hash"]):
        raise db.http_error(401, "invalid_credentials", "email or password is incorrect")
    _set_session(response, int(row["id"]))
    return {"ok": True, "user_id": int(row["id"])}


@router.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(_COOKIE, path="/")
    return {"ok": True}


# ----------------------------------------------------------------- settings grants


@router.post("/api/settings/grants")
def settings_grants(
    body: GrantsBody,
    user=Depends(sessions.require_session),
    conn=Depends(db.get_conn),
):
    uid = int(user["id"])
    if body.matching is None and body.publication is None and body.delegation is None:
        raise db.http_error(422, "payload_forbidden", "nothing to update")

    tier = None
    if body.publication is not None:
        tier = body.publication.tier
        if tier not in (*_PUBLICATION_TIERS, "off"):
            raise db.http_error(422, "payload_forbidden", "tier must be card, card+name or off")

    with db.immediate(conn):
        if body.matching is not None:
            if body.matching:
                _upsert_grant(conn, uid, "matching")
            else:
                # Reviewer P1 #3: withdrawing matching is not just a grant toggle — run the
                # shared leave-matching cascade (availability, open AND matched plans,
                # counterpart notifications) in this same transaction.
                scheduling._leave_cascade(conn, uid, "settings")
        if body.delegation is not None:
            if body.delegation:
                _upsert_grant(conn, uid, "delegation")
            else:
                prev = _live_grant(conn, uid, "delegation")
                if prev is not None:
                    _withdraw_grant(conn, prev, uid, "delegation")
        if body.publication is not None:
            if tier == "off":
                prev = _live_grant(conn, uid, "publication")
                if prev is not None:
                    _withdraw_grant(conn, prev, uid, "publication")
            else:
                _upsert_grant(conn, uid, "publication", tier)

    return {"ok": True, "grants": _snapshot_grants(conn, uid)}


class TzBody(BaseModel):
    tz: str


@router.post("/api/profile/tz")
def save_timezone(body: TzBody, user=Depends(sessions.require_session), conn=Depends(db.get_conn)):
    """The browser's own IANA zone (Intl...resolvedOptions().timeZone), stored once.

    The client already knows this exactly, so the assistant never has to ask and never has an
    excuse to guess. Validated against the zoneinfo database so a garbage string cannot become
    a timezone the scheduler later refuses.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = (body.tz or "").strip()
    if not name or len(name) > 64:
        raise db.http_error(422, "payload_forbidden", "tz must be an IANA timezone name")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise db.http_error(422, "payload_forbidden", f"{name!r} is not an IANA timezone name")
    uid = int(user["id"])
    with db.immediate(conn):
        prev = db.one(conn, "SELECT tz_hint FROM users WHERE id=?", (uid,))
        conn.execute("UPDATE users SET tz_hint=? WHERE id=?", (name, uid))
    if prev is None or prev["tz_hint"] != name:
        chat.log.info("tz_hint_set user=%s tz=%s", uid, name)
    return {"ok": True, "tz": name}


# ---------------------------------------------------------------------------- pages


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(
        request, "register.html", {"invite_required": bool(config.registration_code())}
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": request.query_params.get("next", ""),
            # D8: account deletion lands here with an honest notice.
            "deleted": request.query_params.get("deleted", ""),
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse("/login?next=/", status_code=303)
    return templates.TemplateResponse(
        request, "dashboard.html", {"signed_in": True, "user_id": uid}
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse("/login?next=/settings", status_code=303)
    # D6: remember the published photo across reloads, not just for the session that posted it.
    photo = db.one(conn, "SELECT photo_path, photo_published_at FROM users WHERE id=?", (uid,))
    has_photo = bool(photo and photo["photo_path"] and photo["photo_published_at"])
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "signed_in": True,
            "user_id": uid,
            "grants": _snapshot_grants(conn, uid),
            "has_photo": has_photo,
            "photo_url": f"/media/photo/{uid}" if has_photo else "",
        },
    )


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse("/login?next=/chat", status_code=303)
    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "signed_in": True,
            "ai_enabled": bool(config.ai_api_url()),
            "model_name": config.ai_model(),
            "state": chat.state_summary(conn, uid),
        },
    )


@router.get("/schedule", response_class=HTMLResponse)
def schedule_page(request: Request, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse("/login?next=/schedule", status_code=303)
    row = db.one(
        conn,
        "SELECT tz FROM availability_windows WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (uid,),
    )
    try:
        return templates.TemplateResponse(
            request,
            "schedule.html",
            {"signed_in": True, "tz_default": row["tz"] if row is not None else "UTC"},
        )
    except TemplateNotFound:
        # Lane C's page is not installed on this build: honest 503, no placeholder page.
        return JSONResponse(
            status_code=503,
            content={"error": "schedule_unavailable", "detail": "the schedule page is not installed"},
        )


@router.get("/m/pair/{code}", response_class=HTMLResponse)
def pair_approve_page(request: Request, code: str, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    return templates.TemplateResponse(
        request,
        "pair_approve.html",
        {"signed_in": uid is not None, "code": code},
    )


@router.get("/m/{candidate_id:int}", response_class=HTMLResponse)
def card_page(request: Request, candidate_id: int, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse(f"/login?next=/m/{candidate_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "card.html",
        {"signed_in": True, "candidate_id": candidate_id},
    )


@router.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request, conn=Depends(db.get_conn)):
    uid = _current_uid(request, conn)
    if uid is None:
        return RedirectResponse("/login?next=/matches", status_code=303)
    return templates.TemplateResponse(request, "matches.html", {"signed_in": True})


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return _doc_page(
        request,
        "Privacy",
        os.path.join(DOCS_DIR, "privacy.md"),
        "Privacy policy — full policy coming.",
        "privacy.html",
    )


@router.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return _doc_page(
        request,
        "Terms",
        os.path.join(DOCS_DIR, "terms.md"),
        "Terms of use — full policy coming.",
        "terms.html",
    )

