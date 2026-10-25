#!/usr/bin/env python3
"""demo/scenario.py — CONTRACT §14, fourteen scripted beats, assertable AND watchable.

Run it:

    uv run python demo/scenario.py                 # in-process: real app, real HTTP layer
    uv run python demo/scenario.py --live          # same beats against BASE_URL (httpx)

Default mode spins the real FastAPI app in-process (``fastapi.testclient``) against a throwaway
sqlite file. Every beat drives ROUTER-LEVEL HTTP only — registration, pairing, state, inbox,
dashboard, card, swipe, counter, revoke — so nothing is asserted from direct database seeding.
The only direct sqlite access is a READ-ONLY probe for facts no endpoint exposes (a proposal's
``superseded_by``, the presence of ``reports``/``blocks`` rows); in ``--live`` mode that probe is
unavailable and the demo prints NOT VERIFIABLE instead of pretending.

Beats 1-12 mirror §14, with the landed facts where they supersede the §14 draft (beat 13 is the
Track 2 addition: two agents settle what to do):

* error bodies are FLAT ``{"error", "detail"}`` app-wide (§2 + app.py flattener);
* a re-swipe after a revoke hits a VOIDED candidate → ``404 {"error": "not_found"}``
  (S8's check order; the draft's "409" is superseded);
* the proposal epoch used by revoke-intent checks is the candidate's ``created_at``;
* ``both_want_event`` reads the persisted ``wants`` table — true when both users carry
  the ``event`` want.

Exit code 0 only if every beat passed (and, in ``--live``, if live results match the in-process
reference run beat-for-beat). No sleeps: matching generation and every state transition are
synchronous inside the request that causes them.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import warnings
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TZ = "America/Los_Angeles"
DASH = "\u2013"  # wording.fmt_window uses an en dash: "Thu 19:00–21:00"
PSEUDONYM_RE = re.compile(r"\b[a-z_]+-[0-9a-f]{4}\b")

# Weekday indexes: Mon=0 … Sun=6.
THU, FRI, SAT, SUN, MON, TUE, WED = 3, 4, 5, 6, 0, 1, 2


# --------------------------------------------------------------------------- tiny assertion layer


class BeatFailure(AssertionError):
    """A beat's assertion failed. The runner records it and keeps going (diagnostics)."""


def check(cond, msg: str) -> None:
    if not cond:
        raise BeatFailure(msg)


def excerpt(obj, limit: int = 460) -> str:
    text = json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + " …"


def utc_now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_weekday(weekday: int) -> datetime.date:
    """The next occurrence of `weekday` at least tomorrow — inside the §5 30-day horizon."""
    today = datetime.datetime.now(ZoneInfo(TZ)).date()
    day = today + datetime.timedelta(days=1)
    while day.weekday() != weekday:
        day += datetime.timedelta(days=1)
    return day


def window(weekday: int, start_hour: int, end_hour: int) -> tuple[str, str]:
    """Local wall-time window (start, end) for the next `weekday`, e.g. ('…T19:00:00', '…T21:00:00')."""
    day = next_weekday(weekday)
    start = f"{day.isoformat()}T{start_hour:02d}:00:00"
    end = f"{day.isoformat()}T{end_hour:02d}:00:00"
    return start, end


# --------------------------------------------------------------------------- backends


class InProcessBackend:
    """The real app behind TestClient; router-level HTTP only, against a throwaway sqlite file."""

    kind = "in-process"

    def __init__(self, db_path: str):
        from fastapi.testclient import TestClient
        from server.app import app

        # Imported AFTER SOCIAL_MATCH_DB is set: config reads env at call time, nothing caches it.

        self.db_path = db_path
        self._tc = TestClient
        self._app = app
        self._stack = contextlib.ExitStack()
        # One entered client runs the app lifespan exactly once (init_db + sweep loop).
        self._stack.enter_context(TestClient(app, follow_redirects=False))
        self._clients: list = []

    def client(self):
        c = self._tc(self._app, follow_redirects=False)
        self._clients.append(c)
        return c

    def close(self) -> None:
        for c in self._clients:
            with contextlib.suppress(Exception):
                c.close()
        self._stack.close()

    def _probe(self, sql: str, args: tuple = ()):
        """Read-only look at the demo's own sqlite file — never used to seed a beat."""
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def probe_proposal(self, proposal_id: int):
        return self._probe("SELECT state, superseded_by FROM proposals WHERE id=?", (proposal_id,))

    def probe_blocks_count(self):
        return self._probe("SELECT COUNT(*) FROM blocks")

    def probe_reports_count(self, candidate_id: int):
        return self._probe("SELECT COUNT(*) FROM reports WHERE candidate_id=?", (candidate_id,))


class LiveBackend:
    """The same beats over the wire, for filming against a running uvicorn."""

    kind = "live"

    def __init__(self, base_url: str):
        import httpx

        self.base_url = base_url.rstrip("/")
        self._httpx = httpx
        self._clients: list = []

    def client(self):
        c = self._httpx.Client(base_url=self.base_url, follow_redirects=False, timeout=20.0)
        self._clients.append(c)
        return c

    def close(self) -> None:
        for c in self._clients:
            with contextlib.suppress(Exception):
                c.close()

    # Remote database: these facts are not exposed by any endpoint over HTTP.
    def probe_proposal(self, proposal_id: int):
        return None

    def probe_blocks_count(self):
        return None

    def probe_reports_count(self, candidate_id: int):
        return None


# --------------------------------------------------------------------------- the demo driver


class Demo:
    """Thin HTTP client helpers + shared state across beats. No server imports, no direct writes."""

    def __init__(self, backend, verbose: bool = True):
        self.b = backend
        self.verbose = verbose
        self.run_id = secrets.token_hex(3)
        self.reg_code = os.environ.get("REGISTRATION_CODE", "socialmatch2026")
        self.u: dict[str, dict] = {}
        self.counters = {"windows": 0, "tags": 0, "acks": 0, "states": 0, "offers": 0, "answers": 0, "events": 0}
        self.wire: list[dict] = []  # every agent-negotiation body we POST, so beat 13 can audit it
        self._anon = None
        self.cid = 0
        self.card_body: dict = {}
        self.thu: tuple[str, str] = ("", "")

    # ---------------------------------------------------------------- plumbing
    def say(self, *lines: str) -> None:
        if self.verbose:
            for line in lines:
                print(line)

    def anon(self):
        if self._anon is None:
            self._anon = self.b.client()
        return self._anon

    def client(self, key: str):
        return self.u[key]["client"]

    def bearer(self, key: str) -> dict:
        return {"Authorization": f"Bearer {self.u[key]['token']}"}

    def name(self, key: str) -> str:
        return self.u[key]["name"]

    # Read-only probes for facts no endpoint exposes; None in --live mode.
    def probe_proposal(self, proposal_id: int):
        return self.b.probe_proposal(proposal_id)

    def probe_blocks_count(self):
        return self.b.probe_blocks_count()

    def probe_reports_count(self, candidate_id: int):
        return self.b.probe_reports_count(candidate_id)

    # ---------------------------------------------------------------- flows
    def register(self, key: str, display_name: str, avatar_id: int) -> dict:
        c = self.b.client()
        email = f"{key}.{self.run_id}@demo.test"
        r = c.post(
            "/api/register",
            json={
                "email": email,
                "password": "demo-pass-123",
                "display_name": display_name,
                "avatar_id": avatar_id,
                "adult_attested": True,
                "registration_code": self.reg_code,
            },
        )
        check(
            r.status_code == 201,
            f"POST /api/register for {display_name} → {r.status_code} {r.text}",
        )
        body = r.json()
        self.u[key] = {
            "client": c,
            "uid": int(body["user_id"]),
            "name": display_name,
            "email": email,
            "avatar_id": avatar_id,
            "token": None,
            "agent_id": None,
            "cursor": 0,
        }
        # A photo is MANDATORY before anyone is matchable, so every demo student publishes one
        # immediately after registering — the same click a real user makes on Settings.
        self.publish_photo(key)
        return self.u[key]

    def publish_photo(self, key: str) -> None:
        """Upload a tiny real PNG through the real endpoint (the demo never seeds the DB)."""
        import io

        from PIL import Image  # shipped with the app's photo pipeline

        buf = io.BytesIO()
        Image.new("RGB", (240, 240), (200, 120, 80)).save(buf, format="PNG")
        r = self.client(key).post(
            "/api/me/photo",
            content=buf.getvalue(),
            headers={"Content-Type": "image/png"},
        )
        check(
            r.status_code == 200 and r.json().get("ok") is True,
            f"POST /api/me/photo for {self.name(key)} → {r.status_code} {r.text[:200]}",
        )

    def pair(self, key: str) -> dict:
        label = f"sm-{self.name(key).lower()}"
        r = self.anon().post("/api/pair/start", json={"label": label})
        check(r.status_code == 201, f"POST /api/pair/start → {r.status_code} {r.text}")
        started = r.json()
        code = started["code"]
        r = self.client(key).post("/api/pair/approve", json={"code": code})
        check(
            r.status_code == 200 and r.json().get("ok") is True,
            f"POST /api/pair/approve ({self.name(key)}) → {r.status_code} {r.text}",
        )
        r = self.anon().post("/api/pair/finish", json={"code": code})
        check(r.status_code == 200, f"POST /api/pair/finish → {r.status_code} {r.text}")
        finished = r.json()
        self.u[key]["token"] = finished["token"]
        self.u[key]["agent_id"] = finished["agent_id"]
        return {
            "url": started["url"],
            "code": code,
            "expires_in": started["expires_in"],
            "agent_id": finished["agent_id"],
        }

    def state(
        self,
        key: str,
        *,
        windows: list[tuple[str, str]],
        tags: list[str],
        source: str,
        tier: str,
        wants: tuple[str, ...] = ("hangout", "event"),
        ack: str | None = None,
        extra: dict | None = None,
    ):
        payload: dict = {
            "windows": [{"start": s, "end": e, "tz": TZ} for s, e in windows],
            "tags": list(tags),
            "wants": list(wants),
            "free_time": {"source": source},
            "consent": {"matching": True, "publication": {"tier": tier}},
        }
        if source == "agent_computed":
            payload["free_time"]["student_confirmed_at"] = ack or utc_now()
            self.counters["acks"] += 1
        if extra:
            payload.update(extra)
        r = self.client(key).post("/api/state", json=payload, headers=self.bearer(key))
        if r.status_code == 201:
            self.counters["windows"] += len(windows)
            self.counters["tags"] += len(tags)
            self.counters["states"] += 1
        return r

    def post_state_ok(self, key: str, **kwargs):
        r = self.state(key, **kwargs)
        check(
            r.status_code == 201,
            f"{self.name(key)} POST /api/state → {r.status_code} {r.text}",
        )
        return r

    def inbox(self, key: str, since: int | None = None) -> list[dict]:
        u = self.u[key]
        start = u["cursor"] if since is None else since
        r = self.client(key).get("/api/inbox", params={"since": start}, headers=self.bearer(key))
        check(r.status_code == 200, f"GET /api/inbox ({self.name(key)}) → {r.status_code} {r.text}")
        items = r.json()["items"]
        if since is None and items:
            u["cursor"] = items[-1]["seq"]
        return items

    def inbox_all(self, key: str) -> list[dict]:
        items = self.inbox(key, since=0)
        if items:
            self.u[key]["cursor"] = items[-1]["seq"]
        return items

    def dashboard(self, key: str) -> list[dict]:
        r = self.client(key).get("/api/dashboard")
        check(r.status_code == 200, f"GET /api/dashboard ({self.name(key)}) → {r.status_code}")
        return r.json()["candidates"]

    def card(self, key: str, candidate_id: int):
        return self.client(key).get(f"/api/card/{candidate_id}")

    def swipe(self, key: str, candidate_id: int, proposal_version: int, direction: str):
        return self.client(key).post(
            "/api/swipe",
            json={
                "candidate_id": candidate_id,
                "proposal_version": proposal_version,
                "direction": direction,
            },
        )

    def counter(self, key: str, candidate_id: int, start: str, end: str):
        return self.client(key).post(
            "/api/counter",
            json={
                "candidate_id": candidate_id,
                "window_start": start,
                "window_end": end,
                "tz": TZ,
            },
        )

    def propose(self, key: str, candidate_id: int, version: int, tag: str, url: str):
        body = {"candidate_id": candidate_id, "proposal_version": version,
                "activity_tag": tag, "activity_url": url}
        self.wire.append(body)
        r = self.client(key).post("/api/agent/activity/propose", json=body, headers=self.bearer(key))
        if r.status_code == 201:
            self.counters["offers"] += 1
        return r

    def respond(self, key: str, candidate_id: int, offer_id: int, response: str, **extra):
        body = {"candidate_id": candidate_id, "offer_id": offer_id, "response": response, **extra}
        self.wire.append(body)
        r = self.client(key).post("/api/agent/activity/respond", json=body, headers=self.bearer(key))
        if r.status_code == 200:
            self.counters["answers"] += 1
            if response == "counter":
                self.counters["offers"] += 1
        return r

    def agent_status(self, key: str, candidate_id: int) -> dict:
        r = self.client(key).get(f"/api/agent/activity/{candidate_id}", headers=self.bearer(key))
        check(r.status_code == 200, f"GET /api/agent/activity ({self.name(key)}) → {r.status_code} {r.text}")
        return r.json()


# --------------------------------------------------------------------------- the beats


def beat_1_register(d: Demo) -> None:
    ava = d.register("ava", "Ava", 3)
    ben = d.register("ben", "Ben", 5)
    d.say(
        f"  invite code '{d.reg_code}', adult_attested=true → 201 + session cookie auto-set",
        f"  Ava  user_id={ava['uid']}  avatar={ava['avatar_id']}  display_name='Ava'"
        "   (publication: card+name, granted in beat 3)",
        f"  Ben  user_id={ben['uid']}  avatar={ben['avatar_id']}  display_name='Ben'"
        "   (publication: card only, granted in beat 3)",
    )
    check(ava["uid"] != ben["uid"], "registration must create two distinct users")


def beat_2_pair(d: Demo) -> None:
    for key in ("ava", "ben"):
        info = d.pair(key)
        d.say(
            f"  {d.name(key):<5} start → {info['url']}  (code {info['code']}, ttl {info['expires_in']}s)"
            f"  approve as {d.name(key)} → finish → agent_id={info['agent_id']}"
        )
    check(
        d.u["ava"]["agent_id"] != d.u["ben"]["agent_id"],
        "each pairing must mint a distinct agent_id",
    )


def beat_3_state(d: Demo) -> None:
    d.thu = window(THU, 19, 21)
    ack = utc_now()
    r = d.post_state_ok(
        "ava",
        windows=[d.thu],
        tags=["climbing", "live_music"],
        source="agent_computed",
        tier="card+name",
        ack=ack,
    )
    d.say(
        f"  Ava  (agent path) free_time=agent_computed, student_confirmed_at={ack}"
        "  [provenance, not proof]",
        f"       → 201 changed={r.json()['changed']} expires_at={r.json()['expires_at']}",
    )
    check(d.dashboard("ava") == [], "Ava alone must not have a candidate yet")

    r = d.post_state_ok(
        "ben",
        windows=[d.thu],
        tags=["climbing", "live_music"],
        source="student",
        tier="card",
    )
    d.say(
        f"  Ben  (student path) free_time=source student, window {d.thu[0]} → {d.thu[1]} {TZ}",
        f"       → 201 changed={r.json()['changed']} expires_at={r.json()['expires_at']}",
    )


def beat_4_match_asymmetry(d: Demo) -> None:
    ava_dash = d.dashboard("ava")
    ben_dash = d.dashboard("ben")
    check(len(ava_dash) == 1, f"Ava should have exactly 1 candidate, got {ava_dash}")
    check(len(ben_dash) == 1, f"Ben should have exactly 1 candidate, got {ben_dash}")
    cid = int(ava_dash[0]["candidate_id"])
    check(int(ben_dash[0]["candidate_id"]) == cid, "both sides must see the same candidate")
    d.cid = cid

    ava_items = [i for i in d.inbox("ava") if i["kind"] == "candidate"]
    ben_items = [i for i in d.inbox("ben") if i["kind"] == "candidate"]
    check(len(ava_items) == 1, f"Ava should have one candidate item, got {len(ava_items)}")
    check(len(ben_items) == 1, f"Ben should have one candidate item, got {len(ben_items)}")
    ava_msg = ava_items[-1]["payload"]["message"]
    ben_msg = ben_items[-1]["payload"]["message"]

    pseudo = PSEUDONYM_RE.search(ava_msg)
    check(pseudo is not None, f"Ava's message must label Ben by pseudonym: {ava_msg}")
    check("Ben" not in ava_msg, f"Ava's message must not leak Ben's display name: {ava_msg}")
    check("Ava" in ben_msg, f"Ben's message must carry Ava's display name: {ben_msg}")
    check(
        PSEUDONYM_RE.search(ben_msg) is None,
        f"Ben's message must not demote Ava to a pseudonym: {ben_msg}",
    )
    check("is up for" in ava_msg and "is up for" in ben_msg, "candidate wording per §9")
    expected = f"Thu 19:00{DASH}21:00"
    check(expected in ava_msg, f"candidate window should display '{expected}': {ava_msg}")

    d.say(
        f"  candidate #{cid} inbox payload: {excerpt(ava_items[-1]['payload'])}",
        "  Ava's inbox (Ben granted only `card` → pseudonym):",
        f"    {ava_msg}",
        "  Ben's inbox (Ava granted `card+name` → real name):",
        f"    {ben_msg}",
    )


def beat_5_card(d: Demo) -> None:
    cid = d.cid
    r = d.anon().get(f"/m/{cid}")
    loc = r.headers.get("location", "")
    d.say(
        f"  GET /m/{cid} with NO session → {r.status_code}"
        f"{' Location: ' + loc if loc else ''}  (webui login wall)"
    )
    if r.status_code == 200:
        check("about" not in r.text, f"unauthenticated /m/{cid} must not serve a card: {r.text[:200]}")
    else:
        check(
            r.status_code in (302, 303, 307) and "/login" in loc,
            f"expected a login redirect, got {r.status_code} Location={loc!r}",
        )

    r = d.card("ava", cid)
    check(r.status_code == 200, f"GET /api/card/{cid} (Ava) → {r.status_code} {r.text}")
    card = r.json()
    about, win, reasons = card["about"], card["window"], card["reasons"]
    # Owner's call (supersedes the old tier-gated rule): a matched pair always sees the real
    # name. The pseudonym is still emitted, but is no longer what the student sees.
    check(about["display_name"] == "Ben", f"card must show the real name, got {about}")
    check(
        about["interests_raw"] and "climbing" in about["interests_raw"],
        f"the card must carry the words the student typed, got {about.get('interests_raw')}",
    )
    check(
        isinstance(about["pseudonym"], str) and PSEUDONYM_RE.fullmatch(about["pseudonym"]) is not None,
        f"pseudonym should be tag-<4hex>, got {about['pseudonym']!r}",
    )
    check(
        {"climbing", "live_music"} <= set(about["hobby_tags"]),
        f"hobby_tags should be carried, got {about['hobby_tags']}",
    )
    check(
        win["display"] == f"Thu 19:00{DASH}21:00",
        f"window.display should be 'Thu 19:00{DASH}21:00', got {win['display']!r}",
    )
    check(card["proposal_version"] == 1, f"card should bind proposal version 1, got {card['proposal_version']}")
    check(
        reasons.get("shared_tags") == ["climbing", "live_music"],
        f"reasons.shared_tags wrong: {reasons}",
    )
    check(reasons.get("overlap_hours") == 2, f"reasons.overlap_hours should be 2, got {reasons}")
    check(reasons.get("both_want_event") is True, f"both_want_event should be True: {reasons}")
    d.card_body = card
    d.say(
        "  Ava's card JSON (her session, `about` = Ben):",
        "    "
        + excerpt(
            {
                "about": about,
                "window": win,
                "reasons": reasons,
                "state": card["state"],
                "proposal_version": card["proposal_version"],
            }
        ),
    )


def beat_6_confirm(d: Demo) -> None:
    version = int(d.card_body["proposal_version"])
    r = d.swipe("ava", d.cid, version, "right")
    check(r.status_code == 200, f"Ava POST /api/swipe right → {r.status_code} {r.text}")
    body = r.json()
    check(body["state"] == "a_confirmed", f"proposal should be a_confirmed, got {body}")
    d.say(
        f"  Ava POST /api/swipe {{candidate_id:{d.cid}, proposal_version:{version},"
        f" direction:'right'}} → 200 state={body['state']}"
    )

    items = [i for i in d.inbox("ben") if i["kind"] == "confirmation"]
    check(len(items) == 1, f"Ben should get one confirmation item, got {len(items)}")
    msg = items[-1]["payload"]["message"]
    check("Ava confirmed" in msg, f"confirmation must name Ava: {msg}")
    check("confirm your side" in msg, f"confirmation must ask to confirm: {msg}")
    check("you're on" not in msg and "you\u2019re on" not in msg, f"must never say \"you're on\": {msg}")
    d.say("  Ben's inbox:", f"    {msg}")


def beat_7_match(d: Demo) -> None:
    version = int(d.card_body["proposal_version"])
    r = d.swipe("ben", d.cid, version, "right")
    check(r.status_code == 200, f"Ben POST /api/swipe right → {r.status_code} {r.text}")
    body = r.json()
    check(body["state"] == "both_confirmed", f"proposal should be both_confirmed, got {body}")
    expected = f"Thu 19:00{DASH}21:00"
    for key in ("ava", "ben"):
        items = [i for i in d.inbox(key) if i["kind"] == "match"]
        check(len(items) == 1, f"{d.name(key)} should get one match item, got {len(items)}")
        msg = items[0]["payload"]["message"]
        check(msg.startswith("It's on:"), f"match wording must start \"It's on:\": {msg}")
        check(expected in msg, f"match item must carry the window '{expected}': {msg}")
        d.say(f"  {d.name(key)}'s inbox:", f"    {msg}")

    r = d.card("ava", d.cid)
    check(r.status_code == 200, f"matched card should still serve → {r.status_code} {r.text}")
    check(r.json()["state"] == "matched", f"candidate state should be matched, got {r.json()['state']}")
    d.say(f"  done: candidate #{d.cid} two-sided confirmation → 200 state={body['state']}, card state=matched")


def beat_8_idempotent(d: Demo) -> None:
    version = int(d.card_body["proposal_version"])
    r = d.swipe("ben", d.cid, version, "right")
    check(r.status_code in (200, 409), f"duplicate swipe should be 200 or 409, got {r.status_code} {r.text}")
    if r.status_code == 409:
        err = r.json().get("error")
        check(err in ("already_swiped", "stale_version"), f"unexpected 409 code {err!r}")
        observed = f"409 {err}"
    else:
        check(
            r.json().get("state") == "both_confirmed",
            f"idempotent replay must keep both_confirmed, got {r.json()}",
        )
        observed = "200 idempotent, state=both_confirmed"

    card = d.card("ava", d.cid)
    check(card.status_code == 200, f"card after replay → {card.status_code} {card.text}")
    check(card.json()["state"] == "matched", f"candidate state must stay matched: {card.json()['state']}")
    for key in ("ava", "ben"):
        matches = [i for i in d.inbox_all(key) if i["kind"] == "match"]
        check(
            len(matches) == 1,
            f"{d.name(key)} should have exactly ONE \"It's on\" item, got {len(matches)}",
        )
    d.say(
        f"  Ben re-swipes right on candidate #{d.cid} at version {version} → {observed}",
        "  stability: exactly one \"It's on\" item per user, candidate still matched",
    )


def beat_9_revoke(d: Demo) -> None:
    d.register("cleo", "Cleo", 2)
    d.register("dev", "Dev", 7)
    for key in ("cleo", "dev"):
        info = d.pair(key)
        d.say(f"  pair {d.name(key):<5} {info['url']} → agent_id={info['agent_id']}")

    fri = window(FRI, 19, 21)
    d.post_state_ok("cleo", windows=[fri], tags=["coffee", "chess"], source="student", tier="card+name")
    d.post_state_ok("dev", windows=[fri], tags=["coffee", "chess"], source="student", tier="card+name")
    dash = d.dashboard("cleo")
    check(len(dash) == 1, f"Cleo should see one candidate, got {dash}")
    cid = int(dash[0]["candidate_id"])

    r = d.client("cleo").post("/api/revoke", json={"candidate_id": cid})
    check(r.status_code == 200, f"Cleo POST /api/revoke → {r.status_code} {r.text}")
    check(r.json()["state"] == "voided", f"revoke should report voided, got {r.json()}")

    r = d.card("cleo", cid)
    check(r.status_code == 404, f"card after revoke → {r.status_code} {r.text}")
    # card() reports "no live proposal" on the same 404 branch as an expired match, so the code
    # there is `expired`; the status is the contract-visible fact and the re-swipe below pins
    # `not_found`. (delta noted in the run report)
    check(
        r.json().get("error") in ("not_found", "expired"),
        f"card after revoke should 404 as not_found/expired, got {r.json()}",
    )
    check(d.dashboard("cleo") == [], "revoked candidate must leave Cleo's dashboard")
    check(d.dashboard("dev") == [], "revoked candidate must leave Dev's dashboard")

    expected = f"The plan for Fri 19:00{DASH}21:00 was withdrawn."
    for key in ("cleo", "dev"):
        items = [i for i in d.inbox(key) if i["kind"] == "revocation"]
        check(len(items) == 1, f"{d.name(key)} should get one revocation item, got {len(items)}")
        msg = items[-1]["payload"]["message"]
        check(msg == expected, f"revocation wording per §9, got {msg!r}")
        d.say(f"  {d.name(key)}'s inbox:", f"    {msg}")

    r = d.swipe("dev", cid, 1, "right")
    check(r.status_code == 404, f"re-swipe on a voided candidate → {r.status_code} {r.text}")
    check(r.json().get("error") == "not_found", f"re-swipe error code: {r.json()}")
    d.say(
        f"  Dev re-swipes the voided card → 404 {r.json()['error']}"
        "  (S8 check order: voided candidate → 404, superseding the S14 draft's 409)",
    )


def beat_10_counter(d: Demo) -> None:
    d.register("eli", "Eli", 4)
    d.register("fay", "Fay", 6)
    for key in ("eli", "fay"):
        info = d.pair(key)
        d.say(f"  pair {d.name(key):<5} {info['url']} → agent_id={info['agent_id']}")

    sat = window(SAT, 18, 22)
    d.post_state_ok("eli", windows=[sat], tags=["running", "live_music"], source="student", tier="card+name")
    d.post_state_ok("fay", windows=[sat], tags=["running", "live_music"], source="student", tier="card+name")
    dash = d.dashboard("eli")
    check(len(dash) == 1, f"Eli should see one candidate, got {dash}")
    cid = int(dash[0]["candidate_id"])

    first = d.card("fay", cid)
    check(first.status_code == 200, f"Fay card → {first.status_code} {first.text}")
    old_pid = int(first.json()["proposal_id"])
    check(
        first.json()["window"]["display"] == f"Sat 18:00{DASH}22:00",
        f"initial proposal window: {first.json()['window']}",
    )

    new_start, new_end = window(SAT, 19, 20)
    r = d.counter("eli", cid, new_start, new_end)
    check(r.status_code == 200, f"Eli POST /api/counter → {r.status_code} {r.text}")
    body = r.json()
    check(body["state"] == "proposed", f"counter should leave a proposed proposal, got {body}")
    check(body["proposal_version"] == 2, f"counter should bump to version 2, got {body}")
    check(int(body["proposal_id"]) != old_pid, f"counter should create a new proposal, got {body}")
    d.say(
        f"  Eli counters with {new_start} → {new_end} ({TZ}) → 200 state=proposed"
        f" proposal_id={body['proposal_id']} version={body['proposal_version']} (was #{old_pid} v1)"
    )

    items = [i for i in d.inbox("fay") if i["kind"] == "counter"]
    check(len(items) == 1, f"Fay should get one counter item, got {len(items)}")
    msg = items[-1]["payload"]["message"]
    check("Eli suggested" in msg, f"counter must name Eli: {msg}")
    check(f"Sat 19:00{DASH}20:00" in msg, f"counter must carry the NEW window: {msg}")
    check("instead" in msg, f"counter wording per §9: {msg}")
    d.say("  Fay's inbox:", f"    {msg}")

    second = d.card("fay", cid)
    check(second.status_code == 200, f"card after counter → {second.status_code} {second.text}")
    check(second.json()["proposal_version"] == 2, f"card should show v2: {second.json()}")
    check(int(second.json()["proposal_id"]) == int(body["proposal_id"]), "card should serve the new proposal")
    check(
        second.json()["window"]["display"] == f"Sat 19:00{DASH}20:00",
        f"card window should be the counter window, got {second.json()['window']}",
    )

    r = d.swipe("eli", cid, 1, "right")
    check(r.status_code == 409, f"swipe on the superseded version → {r.status_code} {r.text}")
    check(r.json().get("error") == "stale_version", f"stale swipe code: {r.json()}")
    d.say(f"  Eli swipes on the OLD version 1 → 409 {r.json()['error']} (version binding)")

    rows = d.probe_proposal(old_pid)
    if rows is None:
        d.say("  proposal superseded_by: NOT VERIFIABLE over HTTP (live mode has no DB access)")
    else:
        state, superseded_by = rows[0]
        check(state == "voided", f"old proposal should be voided, probe says {state!r}")
        check(
            int(superseded_by or 0) == int(body["proposal_id"]),
            f"superseded_by should point at #{body['proposal_id']}, probe says {superseded_by!r}",
        )
        d.say(
            f"  [db probe, read-only] old proposal #{old_pid}: state={state},"
            f" superseded_by=#{superseded_by} (new proposal #{body['proposal_id']}, version 2)"
        )


def beat_11_no_overlap_block_report(d: Demo) -> None:
    # ---- (a) no shared time -------------------------------------------------
    d.register("gus", "Gus", 1)
    d.register("hana", "Hana", 8)
    for key in ("gus", "hana"):
        info = d.pair(key)
        d.say(f"  pair {d.name(key):<5} {info['url']} → agent_id={info['agent_id']}")

    mon = window(MON, 9, 10)
    wed = window(WED, 9, 10)
    d.post_state_ok("gus", windows=[mon], tags=["board_games"], source="student", tier="card")
    d.post_state_ok("hana", windows=[wed], tags=["board_games"], source="student", tier="card")
    check(d.dashboard("gus") == [], f"disjoint windows must yield no candidate for Gus: {d.dashboard('gus')}")
    check(d.dashboard("hana") == [], f"disjoint windows must yield no candidate for Hana: {d.dashboard('hana')}")
    for key in ("gus", "hana"):
        check(
            not [i for i in d.inbox(key) if i["kind"] == "candidate"],
            f"{d.name(key)} must get no candidate item on a disjoint pair",
        )
    d.say(
        "  Gus free Mon 09:00–10:00, Hana free Wed 09:00–10:00 → zero candidates, zero matches",
        '  The agent tells Hana, verbatim: "No time that works this week - keep the request open?"',
    )

    # ---- (b) block, both directions ----------------------------------------
    tue_a = window(TUE, 14, 15)
    d.post_state_ok("gus", windows=[tue_a], tags=["board_games"], source="student", tier="card")
    d.post_state_ok("hana", windows=[tue_a], tags=["board_games"], source="student", tier="card")
    dash = d.dashboard("gus")
    check(len(dash) == 1, f"overlapping windows should now match, got {dash}")
    cid = int(dash[0]["candidate_id"])
    # Consume the candidate notifications now, so the post-block inbox check is about NEW items.
    d.inbox("gus")
    d.inbox("hana")
    card = d.card("gus", cid)
    check(card.status_code == 200, f"Gus card → {card.status_code} {card.text}")
    r = d.swipe("gus", cid, int(card.json()["proposal_version"]), "block")
    check(r.status_code == 200, f"Gus POST /api/swipe block → {r.status_code} {r.text}")
    check(r.json()["state"] == "voided", f"block should void the candidate, got {r.json()}")
    r = d.card("hana", cid)
    check(r.status_code == 404, f"blocked card should 404, got {r.status_code} {r.text}")

    blocks = d.probe_blocks_count()
    if blocks is None:
        d.say("  blocks rows (both directions): NOT VERIFIABLE over HTTP (live mode)")
    else:
        check(blocks[0][0] == 2, f"block must write BOTH directions, found {blocks[0][0]} rows")
        d.say("  [db probe, read-only] blocks rows written both directions: 2")

    tue_b = window(TUE, 16, 17)
    d.post_state_ok("gus", windows=[tue_b], tags=["board_games"], source="student", tier="card")
    d.post_state_ok("hana", windows=[tue_b], tags=["board_games"], source="student", tier="card")
    check(d.dashboard("gus") == [], f"blocked pair must not regenerate for Gus: {d.dashboard('gus')}")
    check(d.dashboard("hana") == [], f"blocked pair must not regenerate for Hana: {d.dashboard('hana')}")
    for key in ("gus", "hana"):
        check(
            not [i for i in d.inbox(key) if i["kind"] == "candidate"],
            f"{d.name(key)} must get no new candidate item after a block",
        )
    d.say("  both sides re-post fresh, overlapping state → still zero candidates (block suppresses generation)")

    # ---- (c) report ≠ block -------------------------------------------------
    d.register("ivy", "Ivy", 3)
    d.register("jon", "Jon", 5)
    for key in ("ivy", "jon"):
        info = d.pair(key)
        d.say(f"  pair {d.name(key):<5} {info['url']} → agent_id={info['agent_id']}")

    sun = window(SUN, 15, 16)
    d.post_state_ok("ivy", windows=[sun], tags=["anime"], source="student", tier="card+name")
    d.post_state_ok("jon", windows=[sun], tags=["anime"], source="student", tier="card+name")
    dash = d.dashboard("jon")
    check(len(dash) == 1, f"Ivy+Jon should match, got {dash}")
    cid2 = int(dash[0]["candidate_id"])
    blocks_before = d.probe_blocks_count()
    card2 = d.card("jon", cid2)
    check(card2.status_code == 200, f"Jon card → {card2.status_code} {card2.text}")
    r = d.swipe("jon", cid2, int(card2.json()["proposal_version"]), "report")
    check(r.status_code == 200, f"Jon POST /api/swipe report → {r.status_code} {r.text}")
    check(r.json()["state"] == "hidden", f"report should hide the candidate, got {r.json()}")
    r = d.card("ivy", cid2)
    check(r.status_code == 404, f"reported card should 404 for Ivy, got {r.status_code} {r.text}")

    reports = d.probe_reports_count(cid2)
    blocks_after = d.probe_blocks_count()
    if reports is None or blocks_before is None or blocks_after is None:
        d.say("  reports row present / no blocks row: NOT VERIFIABLE over HTTP (live mode)")
    else:
        check(reports[0][0] == 1, f"report should write a reports row, found {reports[0][0]}")
        check(
            blocks_after[0][0] == blocks_before[0][0],
            "report must NOT write a blocks row (report ≠ block)",
        )
        d.say(
            "  [db probe, read-only] reports row: 1; blocks rows unchanged:"
            f" {blocks_before[0][0]} → {blocks_after[0][0]}"
        )
    d.say(f"  Jon reports candidate #{cid2} → hidden, card 404 for both, no block written")


def beat_12_payload(d: Demo) -> None:
    start, end = d.thu
    bad = {
        "windows": [{"start": start, "end": end, "tz": TZ}],
        "tags": ["climbing", "live_music"],
        "wants": ["hangout"],
        "free_time": {"source": "student"},
        "consent": {"matching": True, "publication": {"tier": "card+name"}},
        # A storage object that carries a calendar title: must die at the allowlist.
        "calendar_titles": ["Therapy - Dr. Ruiz"],
    }
    r = d.client("ava").post("/api/state", json=bad, headers=d.bearer("ava"))
    check(r.status_code == 422, f"payload with calendar_titles → {r.status_code} {r.text}")
    body = r.json()
    check(body.get("error") == "payload_forbidden", f"422 body should be payload_forbidden: {body}")
    check("Therapy" not in r.text, f"the 422 must not echo the rejected title: {r.text}")
    d.say(
        "  Ava POST /api/state with a top-level 'calendar_titles' key →"
        f" 422 {body['error']}",
        f"    detail: {body.get('detail')}",
        "    the title never enters storage — the allowlist rejects it before any write",
    )

    r = d.post_state_ok(
        "ava",
        windows=[d.thu],
        tags=["climbing", "live_music", "coffee"],
        source="student",
        tier="card+name",
    )
    check(r.json()["changed"] is True, f"a genuinely different valid payload should change state: {r.json()}")
    d.say(
        "  Ava POST /api/state again with a VALID payload (source student, 3 tags) →"
        f" 201 changed={r.json()['changed']}",
        "    a rejected payload never poisons the account; the next valid write lands",
    )


# ---- beat 13: Track 2 — two agents settle WHAT to do ----------------------------------------------
#
# The two "agents" below are scripted stand-ins. In production each is a student's assistant following
# skill/SKILL.md Step 6: it runs a web search for options and applies the student's private rules.
# What this beat proves is the SERVER side of that conversation: what is allowed to cross, the
# round structure, and that the students are only asked once the agents agree.

GYM_URL = "https://www.rockgym.example.com/saturday-open-climb"
CAFE_URL = "https://www.beanhouse.example.com/study-nights"


class StandInAgent:
    """Private rules live ONLY here. They are never put in a request body."""

    def __init__(self, name: str, budget_cap: int | None):
        self.name, self.budget_cap = name, budget_cap

    def judge(self, tag: str, url: str, price_found: int) -> tuple[str, str | None]:
        """What this agent would say to an offer whose page it opened. price_found stays local."""
        if self.budget_cap is not None and price_found > self.budget_cap:
            return "counter", "too_expensive"
        return "accept", None


def beat_13_activity(d: Demo) -> None:
    d.register("kai", "Kai", 2)
    d.register("lena", "Lena", 5)
    for key in ("kai", "lena"):
        d.pair(key)
    sat = window(SAT, 14, 17)
    for key in ("kai", "lena"):
        d.post_state_ok(key, windows=[sat], tags=["climbing", "coffee"], source="student", tier="card+name")
    cid = int(d.dashboard("kai")[0]["candidate_id"])

    status = d.agent_status("lena", cid)
    check(status["shared_tags"] == ["climbing", "coffee"], f"shared tags: {status}")
    check(status["activity"] is None, f"nothing offered yet: {status}")
    version = status["proposal_version"]
    d.say(f"  match {cid}: shared interests {status['shared_tags']}, window {status['window']}, no activity yet")

    kai = StandInAgent("Kai's agent", budget_cap=10)      # private: a $10 budget
    lena = StandInAgent("Lena's agent", budget_cap=None)  # private: no limit
    d.say(f"  private rules, never sent: {kai.name} has a budget cap, {lena.name} has none")

    # Lena's agent "searches", finds a climbing gym, and offers it. Only a tag and a link go out.
    r = d.propose("lena", cid, version, "climbing", GYM_URL)
    check(r.status_code == 201, f"Lena's agent proposes → {r.status_code} {r.text}")
    o1 = r.json()["offer_id"]
    d.say(f"  Lena's agent offers climbing + a link → 201 round 1 of {r.json()['max_rounds']}")

    # Until the agents agree, neither student can confirm.
    r = d.swipe("kai", cid, version, "right")
    check(r.status_code == 409 and r.json().get("error") == "activity_pending", f"swipe while pending: {r.status_code} {r.text}")
    d.say("  Kai tries to confirm early → 409 activity_pending (the agents are still talking)")

    # Kai's agent finds the page costs more than its student's rules allow -> counter with a CODE.
    offer = [i for i in d.inbox("kai") if i["kind"] == "activity_offer"]
    check(len(offer) == 1 and offer[0]["payload"]["audience"] == "agent", f"Kai's agent should see one agent-facing offer: {offer}")
    check("message" not in offer[0]["payload"], "agent offers carry no student-facing message")
    verdict, reason = kai.judge("climbing", GYM_URL, price_found=18)
    check((verdict, reason) == ("counter", "too_expensive"), f"stand-in verdict: {(verdict, reason)}")
    r = d.respond("kai", cid, o1, "counter", reason=reason, activity_tag="coffee", activity_url=CAFE_URL)
    check(r.status_code == 200 and r.json()["round"] == 2, f"Kai's agent counters → {r.status_code} {r.text}")
    o2 = r.json()["offer_id"]
    d.say("  Kai's agent counters: coffee + a link, reason code 'too_expensive' (no amount, no sentence) → round 2")

    back = [i for i in d.inbox("lena") if i["kind"] == "activity_offer"]
    check(back and back[-1]["payload"]["counter_of"] == o1 and back[-1]["payload"]["reason"] == "too_expensive",
          f"Lena's agent should see the counter and the code: {back}")
    verdict, _ = lena.judge("coffee", CAFE_URL, price_found=5)
    check(verdict == "accept", "Lena's agent has no limit, so it accepts")
    r = d.respond("lena", cid, o2, "accept")
    check(r.status_code == 200 and r.json()["state"] == "accepted", f"Lena's agent accepts → {r.status_code} {r.text}")
    d.say("  Lena's agent accepts the coffee counter")

    # NOW the students are asked, in plain words, and the plan is still not 'on'.
    asked: dict[str, str] = {}
    for key in ("kai", "lena"):
        texts = [i["payload"] for i in d.inbox(key) if i["kind"] == "activity_agreed"]
        check(len(texts) == 1, f"{d.name(key)} should get one agreement text, got {texts}")
        msg = asked[key] = texts[0]["message"]
        check("landed on coffee" in msg and "Confirm your side" in msg, f"agreement wording: {msg}")
        check("it's on" not in msg.lower() and "you're on" not in msg.lower(), f"premature 'on': {msg}")
    d.say(f"  Kai is asked: {asked['kai']}", f"  Lena is asked: {asked['lena']}")

    card = d.card("kai", cid).json()
    check(card["activity"]["state"] == "accepted" and card["activity"]["tag"] == "coffee", f"card activity: {card['activity']}")
    r = d.swipe("kai", cid, version, "right")
    check(r.status_code == 200, f"Kai confirms his side → {r.status_code} {r.text}")
    r = d.swipe("lena", cid, version, "right")
    check(r.status_code == 200, f"Lena confirms her side → {r.status_code} {r.text}")
    final = [i["payload"] for i in d.inbox("kai") if i["kind"] == "match"]
    check(final and "Plan: coffee." in final[-1]["message"], f"match text should carry the plan: {final}")
    d.say(f"  both students confirm -> {final[-1]['message']}")

    # Audit everything the agents put on the wire in this beat.
    allowed = {"candidate_id", "proposal_version", "activity_tag", "activity_url", "offer_id", "response", "reason"}
    for body in d.wire:
        check(set(body) <= allowed, f"unexpected field on the wire: {sorted(set(body) - allowed)}")
        blob = json.dumps(body).lower()
        check("budget" not in blob and "$" not in blob and "price" not in blob,
              f"a private rule or price leaked into a body: {blob}")
    d.say(f"  audit: {len(d.wire)} agent messages, only tags, https links, ids and reason codes; no budget, no price")

    # Over-sharing is refused by the server, not by good manners.
    r = d.propose("kai", cid, version, "coffee", "http://not-https.example.com")
    check(r.status_code in (409, 422), f"plain-http link should be refused: {r.status_code}")
    r = d.client("kai").post("/api/agent/activity/propose", headers=d.bearer("kai"), json={
        "candidate_id": cid, "proposal_version": version, "activity_tag": "coffee", "activity_url": CAFE_URL,
        "title": "Therapy - Dr. Ruiz"})
    check(r.status_code == 422 and r.json().get("error") == "payload_forbidden", f"extra field: {r.status_code} {r.text}")
    d.say("  an offer with a title field, or a plain-http link -> refused (422 payload_forbidden)")


def beat_14_shared_event(d: Demo) -> None:
    """Pairing signals: two students sent the same PUBLIC event page; only that overlap is shown."""
    d.register("noor", "Noor", 3)
    d.register("omar", "Omar", 4)
    for key in ("noor", "omar"):
        d.pair(key)
    wed = window(WED, 18, 20)
    day = wed[0][:10]
    fair = "https://careers.asu.example.edu/fall-fair"
    show = "https://shows.example.com/synthwave-night"

    def ev(url: str, category: str, start_h: int, end_h: int) -> dict:
        return {"url": url, "start": f"{day}T{start_h:02d}:00", "end": f"{day}T{end_h:02d}:00",
                "tz": TZ, "category": category}

    # Noor's agent found the fair in her inbox, swapped the personal invite for the public page,
    # and she approved the list. Omar sends the same page (a different spelling) plus a show only he has.
    r = d.post_state_ok("noor", windows=[wed], tags=["pottery"], source="student", tier="card+name",
                        extra={"events": [ev(fair, "career", 10, 14)],
                               "events_confirmed_at": utc_now(), "busy_load": 2})
    check(r.json()["events"]["stored"] == [fair], f"stored links: {r.json()}")
    d.say(f"  Noor sends 1 public event + a busy score of 2 → stored as {r.json()['events']['stored']}")
    r = d.post_state_ok("omar", windows=[wed], tags=["pottery"], source="student", tier="card+name",
                        extra={"events": [ev(fair + "/?utm_source=newsletter&rsvp=abc123", "career", 10, 14),
                                          ev(show, "music", 20, 23)],
                               "events_confirmed_at": utc_now(), "busy_load": 1})
    check(fair in r.json()["events"]["stored"] and not any("abc123" in u for u in r.json()["events"]["stored"]),
          f"the personal part of the link was dropped: {r.json()}")
    d.say("  Omar sends the same page spelled differently, plus a show only he has → the ?query part is dropped")

    cands = d.dashboard("noor")
    check(len(cands) == 1, f"Noor should have exactly one card: {cands}")
    cid = int(cands[0]["candidate_id"])
    body = d.card("noor", cid).json()
    check(body["reasons"].get("shared_events") == [{"url": fair, "category": "career"}],
          f"the card shows the event both have: {body['reasons']}")
    check(show not in json.dumps(body) and "abc123" not in json.dumps(body),
          "Omar's other event and the personal part of his link never reach Noor's card")
    d.say("  Noor's card: \"You both have this career event\" — and nothing about Omar's show")

    # What is refused: a title, a plain-http link, and a list the student did not approve.
    bad = [
        ({"events": [dict(ev(fair, "career", 10, 14), title="Interview with Acme")],
          "events_confirmed_at": utc_now()}, "an event title"),
        ({"events": [ev("http://careers.example.edu/fair", "career", 10, 14)],
          "events_confirmed_at": utc_now()}, "an http link"),
        ({"events": [ev(fair, "career", 10, 14)]}, "no approval stamp"),
        ({"busy_load": 9}, "a busy score of 9"),
    ]
    for extra, label in bad:
        r = d.state("noor", windows=[wed], tags=["pottery"], source="student", tier="card+name", extra=extra)
        check(r.status_code == 422 and r.json().get("error") == "payload_forbidden",
              f"{label} should be refused: {r.status_code} {r.text}")
    d.say("  refused (422): an event title, an http link, an unapproved list, a busy score of 9")
    d.counters["events"] += 3



# ------------------------------------------------------- chat-era beats (integrator)

ICS_TPL = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Social Match demo//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "SUMMARY:Dentist\r\n"
    "DTSTART:{start_utc}\r\n"
    "DTEND:{end_utc}\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "SUMMARY:Career fair\r\n"
    "LOCATION:Rec Hall\r\n"
    "DTSTART;VALUE=DATE:{d2}\r\n"
    "DTEND;VALUE=DATE:{d3}\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _ics_utc_times() -> tuple[str, str]:
    """Monday 09:00-10:30 local (TZ) as ICS basic-format UTC strings."""
    day = next_weekday(0)
    start = datetime.datetime(day.year, day.month, day.day, 9, 0, 0, tzinfo=ZoneInfo(TZ))
    end = start + datetime.timedelta(hours=1, minutes=30)
    return (
        start.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ"),
        end.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ"),
    )


def beat_15_manual_schedule(d: Demo) -> None:
    """The demo affordance: windows typed by hand, no calendar + no agent anywhere."""
    d.register("mia", "Mia", 7)
    c = d.client("mia")
    thu = window(3, 19, 21)  # Thursday 19:00-21:00 local
    r = c.post(
        "/api/schedule/windows",
        json={"tz": TZ, "windows": [{"start": thu[0], "end": thu[1]}]},
    )
    check(
        r.status_code == 200 and r.json().get("ok") is True,
        f"manual windows should save: {r.status_code} {r.text[:200]}",
    )
    h = c.get("/api/chat/history").json()
    check(h["state"]["windows"] == 1, f"state chips should count 1 window: {excerpt(h['state'])}")
    d.say("  Mia typed one window in /schedule → saved through the same write path as any agent")
    d.counters["windows"] += 1


def beat_16_ics_upload(d: Demo) -> None:
    """The calendar file path: parse in memory, keep ONLY intervals, preview then Confirm."""
    c = d.client("mia")
    d2 = next_weekday(1).strftime("%Y%m%d")   # Tuesday (ICS basic date format)
    d3 = next_weekday(2).strftime("%Y%m%d")   # Wednesday (all-day DATE ends are exclusive)
    start_utc, end_utc = _ics_utc_times()
    ics = ICS_TPL.format(start_utc=start_utc, end_utc=end_utc, d2=d2, d3=d3).encode()
    r = c.post(f"/api/schedule/ics?tz={TZ}", content=ics, headers={"Content-Type": "text/calendar"})
    body = r.json()
    items = body.get("items", [])
    check(bool(items), "free-time preview must contain at least one window")
    pick = [0]
    confirm = c.post("/api/schedule/ics/confirm", json={"id": body["id"], "pick": pick})
    check(
        confirm.status_code == 200 and confirm.json().get("ok") is True,
        f"confirming the picked window should apply it: {confirm.status_code} {confirm.text[:200]}",
    )
    h = c.get("/api/chat/history").json()
    check(h["state"]["windows"] == 1, f"the picked ICS window should now be stored: {excerpt(h['state'])}")
    d.say("  a real .ics: two events (one all-day), parsed in memory; only intervals kept; Mia")
    d.say("  ticked one previewed window and pressed Confirm — the click is the approval stamp")
    d.counters["windows"] += 1


def beat_17_chat_turn(d: Demo) -> None:
    """The assistant thread. In-process: a scripted provider round trip with a state action.
    In --live: never spend a real provider call; assert the honest surfaces instead."""
    c = d.client("mia")
    h = c.get("/api/chat/history").json()
    check("messages" in h and "state" in h, "history shape: messages + state + pending")

    if d.b.kind != "in-process":
        page = c.get("/chat", follow_redirects=False)
        check(page.status_code == 200, f"GET /chat should render: {page.status_code}")
        d.say("  live mode: skipped the scripted provider turn (no spend), asserted the chat page")
        return

    from server import ai, chat as chat_mod

    original = ai.stream_reply
    original_url = os.environ.get("AI_API_URL")

    async def scripted(system, messages, *, transport=None):
        yield "Sure, matcha noted. "
        yield '```json\n{"actions": [{"op": "set_interests", "tags": ["matcha"]}]}\n```\n'
        yield ai.finished()  # F5: the [DONE] completion marker — without it nothing applies

    try:
        os.environ["AI_API_URL"] = "https://demo-configured.example/v1/chat/completions"
        ai_stream = chat_mod.ai.stream_reply
        chat_mod.ai.stream_reply = scripted
        r = c.post("/api/chat", json={"content": "I'm into matcha"})
        check(r.status_code == 200, f"chat turn should stream: {r.status_code} {r.text[:200]}")
        done = [line for line in r.text.splitlines() if '"done"' in line]
        actions = [line for line in r.text.splitlines() if '"actions"' in line]
        check(bool(done) and bool(actions), "stream should end with done and carry an actions event")
        h = c.get("/api/chat/history").json()
        check(
            "matcha" in h["state"]["interests"],
            f"the assistant's set_interests should land in the chips: {excerpt(h['state'])}",
        )
        conn_probe = d.b.probe_db("SELECT tag FROM hobby_tags") if hasattr(d.b, "probe_db") else None
        if conn_probe:
            check(any(row[0] == "coffee" for row in conn_probe),
                  "matcha must be canonicalised to coffee in the matcher's vocabulary")
        d.say("  Mia told her assistant 'I'm into matcha' → tags applied; matcha counts as coffee")
        d.counters["tags"] += 1
    finally:
        chat_mod.ai.stream_reply = ai_stream
        ai.stream_reply = original
        if original_url is None:
            os.environ.pop("AI_API_URL", None)
        else:
            os.environ["AI_API_URL"] = original_url



BEATS = [
    (1, "register", beat_1_register),
    (2, "pairing", beat_2_pair),
    (3, "state", beat_3_state),
    (4, "match-asymmetry", beat_4_match_asymmetry),
    (5, "card", beat_5_card),
    (6, "confirm", beat_6_confirm),
    (7, "match", beat_7_match),
    (8, "idempotent-confirm", beat_8_idempotent),
    (9, "revoke", beat_9_revoke),
    (10, "counter", beat_10_counter),
    (11, "no-overlap/block/report", beat_11_no_overlap_block_report),
    (12, "payload/therapy", beat_12_payload),
    (13, "agents settle an activity", beat_13_activity),
    (14, "shared public event", beat_14_shared_event),
    (15, "manual schedule", beat_15_manual_schedule),
    (16, "ics → preview → confirm", beat_16_ics_upload),
    (17, "assistant turn + matcha", beat_17_chat_turn),
]


# --------------------------------------------------------------------------- runner


def run_suite(backend, verbose: bool = True) -> list[dict]:
    """Run every beat in order. Failures are recorded, not raised, so one run shows all deltas."""
    demo = Demo(backend, verbose=verbose)
    results: list[dict] = []
    if verbose:
        print(f"\n=== CONTRACT §14 demo — {backend.kind} ===")
        if backend.kind == "in-process":
            print(f"    throwaway sqlite: {backend.db_path}")
        else:
            print(f"    server: {backend.base_url}")
    try:
        for number, name, fn in BEATS:
            if verbose:
                print(f"\n--- BEAT {number} {name} " + "-" * max(3, 58 - len(name)))
            try:
                fn(demo)
            except BeatFailure as exc:
                results.append({"beat": number, "name": name, "status": "FAIL", "detail": str(exc)})
                if verbose:
                    print(f"BEAT {number} {name}: FAIL — {exc}")
            except Exception as exc:  # unexpected: still a failed beat, with the exception type
                results.append(
                    {
                        "beat": number,
                        "name": name,
                        "status": "FAIL",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
                if verbose:
                    print(f"BEAT {number} {name}: FAIL — {type(exc).__name__}: {exc}")
            else:
                results.append({"beat": number, "name": name, "status": "PASS", "detail": ""})
                if verbose:
                    print(f"BEAT {number} {name}: PASS")
    finally:
        if verbose:
            print_summary(demo, results)
        backend.close()
    return results


def print_summary(demo: Demo, results: list[dict]) -> None:
    passed = sum(1 for r in results if r["status"] == "PASS")
    total = len(results)
    print("\n--- WHAT CROSSED THE WIRE " + "-" * 40)
    print(
        f"  availability: {demo.counters['windows']} windows, {demo.counters['tags']} tags"
        "  (closed vocab only, ≤20 windows / ≤5 tags per payload)"
    )
    print(
        "  provenance:  "
        f"{demo.counters['acks']} student acknowledgement (student_confirmed_at — provenance, not proof)"
    )
    print(
        "  identity:    1 display name shown where `card+name` is live (Ava → Ben);"
        " 1 pseudonym (tag-<4hex>) where only `card` is live (Ben → Ava)"
    )
    print(
        f"  negotiation: {demo.counters['offers']} offers (a tag + an https link), {demo.counters['answers']} answers"
        " (accept | counter | pass + a reason code); budgets, prices, venue names: never sent"
    )
    print(
        f"  signals:     {demo.counters['events']} public event links (link + time + category), busy score 0-3;"
        " event titles, senders, locations, email text: never sent"
    )
    print(
        "  rejected:    calendar titles (e.g. 'Therapy - Dr. Ruiz'), class names, grades, photos"
        "  → 422 payload_forbidden, never stored, never matched on"
    )
    if passed == total:
        print(f"\nALL {total} BEATS PASS ({passed}/{total})")
    else:
        failed = [f"#{r['beat']} {r['name']} ({r['detail']})" for r in results if r["status"] != "PASS"]
        print(f"\nBEATS FAILED ({passed}/{total} passed) — {'; '.join(failed)}")


def prepare_env() -> tuple[str, str]:
    """Throwaway DB + demo secrets, set BEFORE importing server.app. Returns (db_path, tmpdir)."""
    tmpdir = tempfile.mkdtemp(prefix="social-match-demo-")
    db_path = os.path.join(tmpdir, "demo.sqlite3")
    os.environ["SOCIAL_MATCH_DB"] = db_path
    os.environ.setdefault("SESSION_SECRET", "demo-session-secret-not-for-production")
    os.environ.setdefault("BLOCK_SALT", "demo-block-salt-not-for-production")
    os.environ.setdefault("REGISTRATION_CODE", "socialmatch2026")
    os.environ.setdefault("BASE_URL", "http://localhost:8000")
    os.environ.setdefault("ENCODER_ENABLED", "0")  # offline and reproducible; the live server uses the encoder
    # Keep the output clean: importing TestClient emits a Starlette/httpx UserWarning.
    warnings.filterwarnings("ignore", message=r".*starlette\.testclient.*deprecated.*")
    return db_path, tmpdir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CONTRACT §14 demo: twelve asserted beats over HTTP.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the same beats over httpx against BASE_URL (default http://localhost:8000)",
    )
    parser.add_argument(
        "--against-url",
        default=None,
        help="same as --live, with an explicit base URL",
    )
    args = parser.parse_args(argv)

    db_path, tmpdir = prepare_env()
    try:
        if args.live or args.against_url:
            base = args.against_url or os.environ.get("BASE_URL", "http://localhost:8000")
            print(f"=== live mode: {base} (plus an in-process reference run for delta checking) ===")
            reference = run_suite(InProcessBackend(db_path), verbose=False)
            live = run_suite(LiveBackend(base), verbose=True)

            deltas = []
            for ref, got in zip(reference, live, strict=True):
                if ref["status"] != got["status"]:
                    deltas.append((ref, got))
            if deltas:
                print("\nLIVE DELTA vs the in-process reference run (same beats, real server):")
                for ref, got in deltas:
                    print(
                        f"  BEAT {ref['beat']} {ref['name']}: in-process={ref['status']}"
                        f" live={got['status']}"
                        f" | in-process detail: {ref['detail'] or '—'}"
                        f" | live detail: {got['detail'] or '—'}"
                    )
                print(f"\nEXIT 1 — {len(deltas)} beat(s) differ between live and in-process state")
                return 1

            failed = [r for r in live if r["status"] != "PASS"]
            if failed:
                print(f"\nEXIT 1 — {len(failed)} live beat(s) failed")
                return 1
            print(f"\nLive and in-process agree on all {len(live)} beats.")
            return 0

        results = run_suite(InProcessBackend(db_path), verbose=True)
        if any(r["status"] != "PASS" for r in results):
            print("\nEXIT 1 — one or more beats failed (see BEAT lines above)")
            return 1
        return 0
    finally:
        if not (args.live or args.against_url):
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
