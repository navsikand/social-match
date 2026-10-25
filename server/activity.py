"""Agent-to-agent activity negotiation (Track 2).

Once two students have a candidate (shared free time + shared tags), their agents settle WHAT
to do. Either agent may propose an activity; the other accepts, counters with a different one,
or passes with a reason from a closed list. At most MAX_ROUNDS offers per proposal. Only when an
offer is accepted do the *students* get asked, and they confirm on the card exactly as before.

What an offer may carry (option B): a tag from the closed vocabulary that BOTH students already
share, and an https link. Nothing else. Responses carry accept|counter|pass and one of four
reason codes, never free text. All endpoints take the agent's bearer token (same as /api/state)
and every transactional rule is a plain `(conn, uid, ...)` function so tests can race them.

Pull, not push: the other agent sees an offer at its next inbox poll. We never promise realtime.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from server import db, offers, wording
from server.auth import get_identity
from server.schema import TAG_VOCAB
from server.scheduling import (
    SchedulingError,
    _active_candidate,
    _log_consent,
    _push,
    _user_alive,
    candidate_row,
    counterpart,
    current_proposal,
    display_tz,
)

router = APIRouter(prefix="/api/agent", tags=["activity"])


# ------------------------------------------------------------------ request bodies


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeBody(_Strict):
    candidate_id: int
    proposal_version: int
    activity_tag: str
    activity_url: str

    @field_validator("activity_tag")
    @classmethod
    def _tag_in_vocab(cls, value: str) -> str:
        if value not in TAG_VOCAB:
            raise ValueError("activity_tag must come from the closed tag vocabulary")
        return value

    @field_validator("activity_url")
    @classmethod
    def _url_ok(cls, value: str) -> str:
        return offers.validate_url(value)


class RespondBody(_Strict):
    candidate_id: int
    offer_id: int
    response: Literal["accept", "counter", "pass"]
    reason: str | None = None
    activity_tag: str | None = None
    activity_url: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> "RespondBody":
        if self.reason is not None and self.reason not in offers.REASONS:
            raise ValueError(f"reason must be one of {', '.join(offers.REASONS)}")
        if self.response == "accept":
            if self.reason or self.activity_tag or self.activity_url:
                raise ValueError("accept carries no reason, tag or link")
        elif self.response == "pass":
            if not self.reason:
                raise ValueError("pass needs a reason code")
            if self.activity_tag or self.activity_url:
                raise ValueError("pass carries no tag or link; use counter to suggest another activity")
        else:  # counter
            if not self.activity_tag or not self.activity_url:
                raise ValueError("counter needs activity_tag and activity_url")
            if self.activity_tag not in TAG_VOCAB:
                raise ValueError("activity_tag must come from the closed tag vocabulary")
            offers.validate_url(self.activity_url)
        return self


# ------------------------------------------------------------------ shared checks


def _tags(conn, uid: int) -> set[str]:
    return {r["tag"] for r in db.q(conn, "SELECT tag FROM hobby_tags WHERE user_id=?", (uid,))}


def _guard(conn, uid: int, candidate_id: int, now: str):
    """Common gate: a live, unblocked, matching-consented pair; returns (cand, other, prop)."""
    cand = candidate_row(conn, uid, candidate_id)
    other = counterpart(cand, uid)
    _active_candidate(cand, now)
    if not wording.matching_live(conn, cand["user_a"], cand["user_b"]):
        raise SchedulingError(404, "not_found", "no longer available")
    if wording.blocked_pair(conn, cand["user_a"], cand["user_b"]):
        raise SchedulingError(404, "not_found", "no longer available")
    prop = current_proposal(conn, cand["id"])
    if prop is None:
        raise SchedulingError(404, "not_found", "no open proposal")
    return cand, other, prop


def _check_shared_tag(conn, uid: int, other: int, tag: str) -> None:
    if tag not in (_tags(conn, uid) & _tags(conn, other)):
        raise SchedulingError(422, "activity_not_shared", "the activity must be an interest you both share")


def _validate_values(tag: object, url: object) -> tuple[str, str]:
    """Value validation lives HERE, not in the callers (gate review P1 #2).

    An allow-listed *key* is not an allow-listed *value*: the tag must come from the closed
    vocabulary and the link must pass the same strict `offers.validate_url` the activity models
    use. Endpoints may pre-check for a nicer message, but the last word is the core's, before the
    first write of its own transaction — nothing unvalidated reaches a row, an inbox push or a
    counterpart transcript.
    """
    if not isinstance(tag, str) or tag not in TAG_VOCAB:
        raise SchedulingError(
            422, "payload_forbidden", "the activity must come from the closed tag vocabulary"
        )
    try:
        checked = offers.validate_url(url)
    except ValueError as exc:
        raise SchedulingError(422, "payload_forbidden", str(exc)) from exc
    return tag, checked


def _validate_reason(reason: object) -> str:
    if not isinstance(reason, str) or reason not in offers.REASONS:
        raise SchedulingError(
            422, "payload_forbidden", f"reason must be one of {', '.join(offers.REASONS)}"
        )
    return reason


def _require_autonomous_consent(conn, uid: int, now: str) -> None:
    """PLAN §C2a, re-read INSIDE the core's write transaction (gate review P1 #1).

    An autonomous actor acts only while its delegation grant is live and its account still
    exists. The caller's pre-check is advisory: a Settings withdrawal that commits between that
    pre-check and this BEGIN IMMEDIATE is caught here, before the first write.
    """
    if not _user_alive(conn, uid):
        raise SchedulingError(409, "delegation_required", "your account is no longer available")
    granted = db.one(
        conn,
        "SELECT 1 FROM grants WHERE user_id=? AND kind='delegation' AND withdrawn_at IS NULL"
        " AND expires_at > ? LIMIT 1",
        (uid, now),
    )
    if granted is None:
        raise SchedulingError(
            409, "delegation_required", "your delegation was off when this would have applied"
        )


def _agent_offer_payload(conn, cand, prop, offer, *, counter_of=None, reason=None) -> dict:
    tz = display_tz(conn, cand)
    return {
        "kind": "activity_offer",
        "audience": "agent",
        "candidate_id": cand["id"],
        "offer_id": offer["id"],
        "round": offer["round"],
        "max_rounds": offers.MAX_ROUNDS,
        "proposal_version": prop["version"],
        "activity_tag": offer["activity_tag"],
        "activity_url": offer["activity_url"],
        "window": wording.fmt_window(prop["window_start"], prop["window_end"], tz),
        "counter_of": counter_of,
        "reason": reason,
    }


# ------------------------------------------------------------------ propose


def propose(conn, uid: int, candidate_id: int, proposal_version: int, tag: str, url: str,
            *, autonomous: bool = False) -> dict:
    with db.immediate(conn):
        now = db.utcnow()
        cand, other, prop = _guard(conn, uid, candidate_id, now)
        if autonomous:  # authorization reads first, then the consent re-read, then any write
            _require_autonomous_consent(conn, uid, now)
        tag, url = _validate_values(tag, url)
        if prop["version"] != proposal_version:
            raise SchedulingError(409, "stale_version", "the time changed; reload the card")
        if db.one(conn, "SELECT 1 FROM proposal_confirmations WHERE proposal_id=? LIMIT 1", (prop["id"],)):
            raise SchedulingError(409, "plan_already_confirmed", "a student already confirmed this plan")
        _check_shared_tag(conn, uid, other, tag)

        latest = offers.latest_offer(conn, prop["id"])
        if latest is not None:
            if latest["state"] == "offered":
                raise SchedulingError(409, "offer_pending", "an offer is already waiting for an answer")
            if latest["state"] == "accepted":
                raise SchedulingError(409, "already_agreed", "an activity is already agreed")
        round_no = (latest["round"] if latest else 0) + 1
        if round_no > offers.MAX_ROUNDS:
            raise SchedulingError(409, "round_limit", "no more rounds; the agents could not agree")

        conn.execute(
            "INSERT INTO activity_offers (candidate_id, proposal_id, round, proposer_id, activity_tag,"
            " activity_url, state, created_at) VALUES (?,?,?,?,?,?, 'offered', ?)",
            (cand["id"], prop["id"], round_no, uid, tag, url, now),
        )
        offer = db.one(conn, "SELECT * FROM activity_offers WHERE proposal_id=? AND round=?", (prop["id"], round_no))
        _log_consent(conn, uid, "activity:offer", "activity", {"round": round_no, "tag": tag})
        if _user_alive(conn, other):
            _push(conn, other, _agent_offer_payload(conn, cand, prop, offer))
        return {"state": "offered", "offer_id": offer["id"], "round": round_no, "max_rounds": offers.MAX_ROUNDS}


# ------------------------------------------------------------------ respond


def respond(conn, uid: int, candidate_id: int, offer_id: int, response: str,
            reason: str | None = None, tag: str | None = None, url: str | None = None,
            *, autonomous: bool = False) -> dict:
    with db.immediate(conn):
        now = db.utcnow()
        cand, other, prop = _guard(conn, uid, candidate_id, now)
        if autonomous:
            _require_autonomous_consent(conn, uid, now)
        offer = db.one(conn, "SELECT * FROM activity_offers WHERE id=? AND candidate_id=?", (offer_id, cand["id"]))
        if offer is None:
            raise SchedulingError(404, "not_found", "no such offer")
        latest = offers.latest_offer(conn, prop["id"])
        if latest is None or latest["id"] != offer["id"]:
            raise SchedulingError(409, "stale_offer", "that offer is no longer the current one")
        if offer["state"] != "offered":
            raise SchedulingError(409, "offer_closed", "that offer was already answered")
        if offer["proposer_id"] == uid:
            raise SchedulingError(403, "own_offer", "you cannot answer your own offer")

        if response == "accept":
            conn.execute(
                "UPDATE activity_offers SET state='accepted', responder_id=?, responded_at=? WHERE id=?",
                (uid, now, offer["id"]),
            )
            _log_consent(conn, uid, "activity:accept", "activity", {"round": offer["round"], "tag": offer["activity_tag"]})
            tz = display_tz(conn, cand)
            for me, them in ((uid, other), (other, uid)):
                if _user_alive(conn, me):
                    _push(conn, me, _agreed_text(conn, cand, prop, offer, them, tz))
            return {"state": "accepted", "offer_id": offer["id"]}

        if response == "pass":
            reason = _validate_reason(reason)
            conn.execute(
                "UPDATE activity_offers SET state='passed', reason=?, responder_id=?, responded_at=? WHERE id=?",
                (reason, uid, now, offer["id"]),
            )
            _log_consent(conn, uid, "activity:pass", "activity", {"round": offer["round"], "reason": reason})
            if _user_alive(conn, other):
                _push(conn, other, {
                    "kind": "activity_response", "audience": "agent", "candidate_id": cand["id"],
                    "offer_id": offer["id"], "round": offer["round"], "response": "pass", "reason": reason,
                })
            exhausted = offer["round"] >= offers.MAX_ROUNDS
            if exhausted:
                tz = display_tz(conn, cand)
                for me, them in ((uid, other), (other, uid)):
                    if _user_alive(conn, me):
                        _push(conn, me, _none_text(conn, cand, prop, them, tz))
            return {"state": "passed", "offer_id": offer["id"], "exhausted": exhausted}

        # counter
        next_round = offer["round"] + 1
        if next_round > offers.MAX_ROUNDS:
            raise SchedulingError(409, "round_limit", "last round: accept or pass")
        tag, url = _validate_values(tag, url)
        # Finding 2 closure: a counter may carry no reason at all, but whatever arrives must be a
        # closed-list code — never provider prose pushed into the counterpart's inbox/notes. Chat's
        # op schema already rejects reason-on-counter; this makes the CORE refuse it too, for any caller.
        if reason is not None:
            reason = _validate_reason(reason)
        _check_shared_tag(conn, uid, other, tag)
        conn.execute(
            "UPDATE activity_offers SET state='countered', reason=?, responder_id=?, responded_at=? WHERE id=?",
            (reason, uid, now, offer["id"]),
        )
        conn.execute(
            "INSERT INTO activity_offers (candidate_id, proposal_id, round, proposer_id, activity_tag,"
            " activity_url, state, created_at) VALUES (?,?,?,?,?,?, 'offered', ?)",
            (cand["id"], prop["id"], next_round, uid, tag, url, now),
        )
        new = db.one(conn, "SELECT * FROM activity_offers WHERE proposal_id=? AND round=?", (prop["id"], next_round))
        _log_consent(conn, uid, "activity:counter", "activity", {"round": next_round, "tag": tag, "reason": reason})
        if _user_alive(conn, other):
            _push(conn, other, _agent_offer_payload(conn, cand, prop, new, counter_of=offer["id"], reason=reason))
        return {"state": "offered", "offer_id": new["id"], "round": next_round, "max_rounds": offers.MAX_ROUNDS}


# ------------------------------------------------------------------ student-facing texts


def _agreed_text(conn, cand, prop, offer, about_uid: int, tz: str) -> dict:
    """Goes to a STUDENT. 'Settled' is the agents' agreement only: wording never says the plan is on."""
    w = wording.fmt_window(prop["window_start"], prop["window_end"], tz)
    url = wording.card_url(cand["id"])
    who = wording.label(conn, about_uid)
    return {
        "kind": "activity_agreed",
        "audience": "student",
        "candidate_id": cand["id"],
        "about": who,
        "window": w,
        "activity_tag": offer["activity_tag"],
        "activity_url": offer["activity_url"],
        "card_url": url,
        "message": f"You and {who} landed on {offers.human_tag(offer['activity_tag'])} for {w}. Confirm your side: {url}",
    }


def _none_text(conn, cand, prop, about_uid: int, tz: str) -> dict:
    w = wording.fmt_window(prop["window_start"], prop["window_end"], tz)
    url = wording.card_url(cand["id"])
    who = wording.label(conn, about_uid)
    return {
        "kind": "activity_none",
        "audience": "student",
        "candidate_id": cand["id"],
        "about": who,
        "window": w,
        "card_url": url,
        "message": f"No activity worked out with {who} for {w}. You can still confirm the time and pick something yourselves: {url}",
    }


# ------------------------------------------------------------------ endpoints


def _error(exc: SchedulingError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"error": exc.code, "detail": exc.detail})


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "payload_forbidden", "detail": detail})


def _detail(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    msg = str(first.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


async def _body(request: Request, model):
    try:
        raw = await request.json()
    except Exception:
        return None, _forbidden("request body must be a JSON object")
    try:
        return model.model_validate(raw), None
    except ValidationError as exc:
        return None, _forbidden(_detail(exc))


@router.post("/activity/propose")
async def api_propose(request: Request, identity=Depends(get_identity), conn=Depends(db.get_conn)):
    body, err = await _body(request, ProposeBody)
    if err is not None:
        return err
    if body is None:  # _body returns exactly one of (body, error)
        return _forbidden("request body must be a JSON object")
    try:
        out = propose(conn, identity.user_id, body.candidate_id, body.proposal_version,
                      body.activity_tag, body.activity_url)
    except SchedulingError as exc:
        return _error(exc)
    return JSONResponse(status_code=201, content=out)


@router.post("/activity/respond")
async def api_respond(request: Request, identity=Depends(get_identity), conn=Depends(db.get_conn)):
    body, err = await _body(request, RespondBody)
    if err is not None:
        return err
    if body is None:  # _body returns exactly one of (body, error)
        return _forbidden("request body must be a JSON object")
    try:
        out = respond(conn, identity.user_id, body.candidate_id, body.offer_id, body.response,
                      body.reason, body.activity_tag, body.activity_url)
    except SchedulingError as exc:
        return _error(exc)
    return JSONResponse(content=out)


@router.get("/activity/{candidate_id}")
def api_status(candidate_id: int, identity=Depends(get_identity), conn=Depends(db.get_conn)):
    """The agent's read-only view: the current offer on this candidate's current proposal."""
    try:
        cand = candidate_row(conn, identity.user_id, candidate_id)
    except SchedulingError as exc:
        return _error(exc)
    prop = current_proposal(conn, cand["id"])
    view = offers.card_view(conn, identity.user_id, prop["id"]) if prop is not None else None
    other = counterpart(cand, identity.user_id)
    return {
        "candidate_id": cand["id"],
        "state": cand["state"],
        "proposal_version": prop["version"] if prop else None,
        # Both students already see their shared interests on the card, so this is nothing new.
        "shared_tags": sorted(_tags(conn, identity.user_id) & _tags(conn, other)),
        "window": wording.fmt_window(prop["window_start"], prop["window_end"], display_tz(conn, cand)) if prop else None,
        "activity": view,
    }

