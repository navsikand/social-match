"""Pages + register/login/settings. P1d owns this file (CONTRACT §11).

Card, swipe, counter, revoke, dashboard, matches, delete-account and pair endpoints are
owned by sibling phases; the pages here only *call* them through same-origin fetch and
degrade gracefully when they are absent.
"""
import json
import os
import sqlite3

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from server import config, db, passwords, scheduling, sessions

router = APIRouter(tags=["webui"])

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(_HERE, "templates")
DOCS_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "docs"))
SKILL_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "skill"))

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


def _snapshot_grants(conn, uid: int) -> dict:
    out: dict = {"matching": None, "publication": None}
    for kind in ("matching", "publication"):
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
    if body.registration_code.strip() != config.registration_code():
        raise db.http_error(403, "invite_required", "a valid registration code is required")
    if not body.adult_attested:
        raise db.http_error(403, "attest_required", "you must confirm you are 18 or older")

    email = body.email.strip().lower()
    display_name = body.display_name.strip()
    if not email or not body.password or not display_name:
        raise db.http_error(422, "payload_forbidden", "email, password and display name are required")

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
    if body.matching is None and body.publication is None:
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
        if body.publication is not None:
            if tier == "off":
                prev = _live_grant(conn, uid, "publication")
                if prev is not None:
                    _withdraw_grant(conn, prev, uid, "publication")
            else:
                _upsert_grant(conn, uid, "publication", tier)

    return {"ok": True, "grants": _snapshot_grants(conn, uid)}


# ---------------------------------------------------------------------------- pages


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {})


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"next": request.query_params.get("next", "")}
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
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"signed_in": True, "user_id": uid, "grants": _snapshot_grants(conn, uid)},
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


@router.get("/skill", response_class=HTMLResponse)
def skill_page(request: Request):
    """Dashboard link target: the Cubbie skill instructions, when the skill package exists."""
    body = _read_doc(os.path.join(SKILL_DIR, "SKILL.md"))
    if body is None:
        body = (
            "Cubbie skill instructions\n"
            "=========================\n\n"
            "The skill package ships alongside the server. Once installed, Cubbie can:\n\n"
            "  1. pair with this account and poll your inbox,\n"
            "  2. read your calendar and reduce events to free time ranges (titles are never sent),\n"
            "  3. show you the exact payload before anything is shared.\n\n"
            "Full instructions are coming with the skill package."
        )
    return templates.TemplateResponse(
        request, "privacy.html", {"title": "Cubbie skill instructions", "content": body, "missing": False}
    )
