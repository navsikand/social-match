"""Track 2: agent-to-agent activity negotiation (server/activity.py, server/offers.py).

HTTP tests drive the real endpoints with bearer tokens (agents) and signed sessions (students),
exactly as production does. The race test calls the plain function on its own connections.
"""
import threading

import pytest

from server import activity, db
from tests import util
from tests.util import auth_header, mint_pairing, mk_user

TAGS = ("climbing", "coffee")
GYM = "https://rockgym.example.com/open-climb"
CAFE = "https://beanhouse.example.com/menu"
PROPOSE = "/api/agent/activity/propose"
RESPOND = "/api/agent/activity/respond"


@pytest.fixture()
def pair(client):
    """Two students, a candidate with 2 shared tags, a bearer token each. v1 time-only proposal."""
    a = mk_user("ava@example.com", "Ava")
    b = mk_user("ben@example.com", "Ben")
    cid, pid = util.mk_candidate(a, b, tags_list=TAGS)
    ta, _ = mint_pairing(a)
    tb, _ = mint_pairing(b)
    return {"client": client, "a": a, "b": b, "cid": cid, "pid": pid, "ta": ta, "tb": tb}


def _propose(p, who, tag="climbing", url=GYM, version=1, cid=None):
    return p["client"].post(
        PROPOSE,
        headers=auth_header(p["t" + who]),
        json={"candidate_id": cid or p["cid"], "proposal_version": version,
              "activity_tag": tag, "activity_url": url},
    )


def _respond(p, who, offer_id, response, **extra):
    return p["client"].post(
        RESPOND,
        headers=auth_header(p["t" + who]),
        json={"candidate_id": p["cid"], "offer_id": offer_id, "response": response, **extra},
    )


def _inbox(p, who):
    return p["client"].get("/api/inbox", headers=auth_header(p["t" + who])).json()["items"]


def _swipe(p, who, direction="right", version=1):
    util.direct_session(p["client"], p[who])
    return p["client"].post(
        "/api/swipe",
        json={"candidate_id": p["cid"], "proposal_version": version, "direction": direction},
    )


# ------------------------------------------------------------------ happy path


def test_propose_accept_then_students_confirm(pair):
    r = _propose(pair, "a")
    assert r.status_code == 201, r.text
    offer_id = r.json()["offer_id"]

    # The OTHER agent sees it, tagged as agent-facing, with only tag + link + context.
    items = _inbox(pair, "b")
    assert [i["kind"] for i in items] == ["activity_offer"]
    payload = items[0]["payload"]
    assert payload["audience"] == "agent"
    assert payload["activity_tag"] == "climbing" and payload["activity_url"] == GYM
    assert payload["offer_id"] == offer_id and payload["proposal_version"] == 1
    assert "message" not in payload  # never forwarded verbatim to a student
    assert _inbox(pair, "a") == []  # the proposer is not told about its own offer

    assert _respond(pair, "b", offer_id, "accept").status_code == 200

    # Both STUDENTS are told, in words that do not claim the plan is on.
    for who in ("a", "b"):
        texts = [i["payload"] for i in _inbox(pair, who) if i["kind"] == "activity_agreed"]
        assert len(texts) == 1
        t = texts[0]
        assert t["audience"] == "student"
        assert "Confirm your side" in t["message"] and t["card_url"] in t["message"]
        assert "it's on" not in t["message"].lower() and "you're on" not in t["message"].lower()

    # Students confirm on the card as before; match text now carries the agreed plan.
    assert _swipe(pair, "a").status_code == 200
    assert _swipe(pair, "b").status_code == 200
    matches = [i["payload"] for i in _inbox(pair, "a") if i["kind"] == "match"]
    assert matches and "Plan: climbing." in matches[0]["message"]
    assert matches[0]["activity_url"] == GYM


def test_students_cannot_confirm_while_offer_pending(pair):
    assert _propose(pair, "a").status_code == 201
    r = _swipe(pair, "b")
    assert r.status_code == 409 and r.json()["error"] == "activity_pending"
    assert _swipe(pair, "a").json()["error"] == "activity_pending"


def test_no_offers_keeps_the_original_time_only_flow(pair):
    assert _swipe(pair, "a").status_code == 200
    assert _swipe(pair, "b").status_code == 200


def test_agent_status_gives_version_window_and_shared_tags(pair):
    r = pair["client"].get(f"/api/agent/activity/{pair['cid']}", headers=auth_header(pair["ta"]))
    assert r.status_code == 200
    body = r.json()
    assert body["proposal_version"] == 1 and body["activity"] is None
    assert body["shared_tags"] == ["climbing", "coffee"] and body["window"]
    outsider = mk_user("cy@example.com", "Cy")
    tc, _ = mint_pairing(outsider)
    assert pair["client"].get(f"/api/agent/activity/{pair['cid']}", headers=auth_header(tc)).status_code == 404


def test_card_shows_activity_block(pair):
    offer_id = _propose(pair, "a").json()["offer_id"]
    util.direct_session(pair["client"], pair["b"])
    card = pair["client"].get(f"/api/card/{pair['cid']}").json()
    act = card["activity"]
    assert act["state"] == "offered" and act["tag"] == "climbing"
    assert act["url"] == GYM and act["host"] == "rockgym.example.com"
    assert act["proposed_by_you"] is False and act["offer_id"] == offer_id
    util.direct_session(pair["client"], pair["a"])
    assert pair["client"].get(f"/api/card/{pair['cid']}").json()["activity"]["proposed_by_you"] is True


# ------------------------------------------------------------------ the conflict: counter + pass


def test_pass_with_reason_then_counter_then_accept(pair):
    """Both like climbing and coffee; Ben's budget rules the gym out, so his agent counters."""
    o1 = _propose(pair, "a").json()["offer_id"]
    c = _respond(pair, "b", o1, "counter", reason="too_expensive", activity_tag="coffee", activity_url=CAFE)
    assert c.status_code == 200, c.text
    o2 = c.json()["offer_id"]
    assert c.json()["round"] == 2

    back = [i["payload"] for i in _inbox(pair, "a") if i["kind"] == "activity_offer"]
    assert back[0]["counter_of"] == o1 and back[0]["reason"] == "too_expensive"
    assert back[0]["activity_tag"] == "coffee"

    # The old offer is closed, and the proposer cannot answer their own counter-offer's twin.
    assert _respond(pair, "a", o1, "accept").json()["error"] == "stale_offer"
    assert _respond(pair, "b", o2, "accept").json()["error"] == "own_offer"
    assert _respond(pair, "a", o2, "accept").status_code == 200

    util.direct_session(pair["client"], pair["a"])
    act = pair["client"].get(f"/api/card/{pair['cid']}").json()["activity"]
    assert act["state"] == "accepted" and act["tag"] == "coffee"


def test_pass_lets_either_agent_propose_again(pair):
    o1 = _propose(pair, "a").json()["offer_id"]
    r = _respond(pair, "b", o1, "pass", reason="too_far")
    assert r.json() == {"state": "passed", "offer_id": o1, "exhausted": False}
    resp = [i["payload"] for i in _inbox(pair, "a") if i["kind"] == "activity_response"]
    assert resp[0]["response"] == "pass" and resp[0]["reason"] == "too_far"
    # Round 2 can come from the agent that passed.
    r2 = _propose(pair, "b", tag="coffee", url=CAFE)
    assert r2.status_code == 201 and r2.json()["round"] == 2
    # A pending offer blocks a second simultaneous proposal.
    assert _propose(pair, "a").json()["error"] == "offer_pending"


def test_round_cap_then_time_only_fallback(pair):
    o1 = _propose(pair, "a").json()["offer_id"]
    o2 = _respond(pair, "b", o1, "counter", activity_tag="coffee", activity_url=CAFE).json()["offer_id"]
    o3 = _respond(pair, "a", o2, "counter", activity_tag="climbing", activity_url=GYM).json()
    assert o3["round"] == 3
    # Round 4 does not exist: a counter on the last round is refused, accept/pass still work.
    r = _respond(pair, "b", o3["offer_id"], "counter", activity_tag="coffee", activity_url=CAFE)
    assert r.status_code == 409 and r.json()["error"] == "round_limit"
    p = _respond(pair, "b", o3["offer_id"], "pass", reason="not_my_vibe")
    assert p.json()["exhausted"] is True
    for who in ("a", "b"):
        none = [i["payload"] for i in _inbox(pair, who) if i["kind"] == "activity_none"]
        assert len(none) == 1 and none[0]["audience"] == "student"
        assert "confirm the time" in none[0]["message"]
    assert _propose(pair, "a").json()["error"] == "round_limit"
    # Students are not stuck: the time-only plan can still be confirmed.
    assert _swipe(pair, "a").status_code == 200
    assert _swipe(pair, "b").status_code == 200


# ------------------------------------------------------------------ the allowlist


@pytest.mark.parametrize("url", [
    "http://rockgym.example.com/x",            # not https
    "https://user:pw@rockgym.example.com/x",   # credentials
    "https://rockgym.example.com/x#frag",      # fragment
    "https://10.0.0.1/x",                      # IP literal
    "https://localhost/x",                     # single-label host
    "https://rockgym.example.com/a b",         # whitespace
    "javascript:alert(1)",                     # wrong scheme entirely
    "https://rockgym.example.com:8443/x",      # odd port
    "https://exämple.com/x",                   # non-ASCII
    "https://rockgym.example.com/" + "a" * 300,  # too long
    "",
])
def test_bad_links_rejected(pair, url):
    r = _propose(pair, "a", url=url)
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden", r.text


def test_free_text_tag_rejected(pair):
    r = _propose(pair, "a", tag="Phoenix Rock Gym")
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden"


def test_tag_must_be_shared_by_both_students(pair):
    util.tags(pair["a"], ["climbing", "coffee", "chess"])
    r = _propose(pair, "a", tag="chess", url="https://chess.example.com/club")
    assert r.status_code == 422 and r.json()["error"] == "activity_not_shared"


def test_extra_fields_rejected_so_no_title_or_place_can_ride_along(pair):
    r = pair["client"].post(PROPOSE, headers=auth_header(pair["ta"]), json={
        "candidate_id": pair["cid"], "proposal_version": 1, "activity_tag": "climbing",
        "activity_url": GYM, "title": "Therapy - Dr. Ruiz", "place": "Room 4"})
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden"
    o = _propose(pair, "a").json()["offer_id"]
    r = _respond(pair, "b", o, "accept", note="sounds great, but I'm broke")
    assert r.status_code == 422 and r.json()["error"] == "payload_forbidden"


def test_response_shapes(pair):
    o = _propose(pair, "a").json()["offer_id"]
    assert _respond(pair, "b", o, "pass").status_code == 422                       # pass needs a reason
    assert _respond(pair, "b", o, "pass", reason="broke").status_code == 422        # free-text reason
    assert _respond(pair, "b", o, "accept", reason="too_far").status_code == 422    # accept carries nothing
    assert _respond(pair, "b", o, "counter").status_code == 422                     # counter needs tag + link
    assert _respond(pair, "b", o, "counter", activity_tag="coffee", activity_url="http://x.com").status_code == 422
    assert _respond(pair, "b", o, "maybe").status_code == 422
    # None of those changed anything.
    assert _respond(pair, "b", o, "accept").status_code == 200


# ------------------------------------------------------------------ auth, versions, scope


def test_requires_agent_token(pair):
    r = pair["client"].post(PROPOSE, json={"candidate_id": pair["cid"], "proposal_version": 1,
                                           "activity_tag": "climbing", "activity_url": GYM})
    assert r.status_code == 401


def test_outsider_gets_404(pair):
    c = mk_user("cy@example.com", "Cy")
    tc, _ = mint_pairing(c)
    r = pair["client"].post(PROPOSE, headers=auth_header(tc), json={
        "candidate_id": pair["cid"], "proposal_version": 1,
        "activity_tag": "climbing", "activity_url": GYM})
    assert r.status_code == 404


def test_stale_proposal_version_after_a_time_counter(pair):
    util.direct_session(pair["client"], pair["a"])
    r = pair["client"].post("/api/counter", json={
        "candidate_id": pair["cid"], "window_start": "2026-10-29T19:30", "window_end": "2026-10-29T20:30",
        "tz": "America/Los_Angeles"})
    assert r.status_code == 200, r.text
    assert _propose(pair, "a", version=1).json()["error"] == "stale_version"
    assert _propose(pair, "a", version=2).status_code == 201


def test_no_offer_after_a_student_already_confirmed(pair):
    assert _swipe(pair, "a").status_code == 200
    r = _propose(pair, "b")
    assert r.status_code == 409 and r.json()["error"] == "plan_already_confirmed"


def test_blocked_pair_cannot_negotiate(pair):
    assert _swipe(pair, "a", "block").status_code == 200
    assert _propose(pair, "b").status_code == 404


def test_withdrawing_matching_ends_negotiation(pair):
    o = _propose(pair, "a").json()["offer_id"]
    util.direct_session(pair["client"], pair["b"])
    assert pair["client"].post("/api/leave-matching").status_code == 200
    assert _respond(pair, "b", o, "accept").status_code == 404


def test_delete_account_removes_offers(pair, db_env):
    _propose(pair, "a")
    util.direct_session(pair["client"], pair["a"])
    r = pair["client"].post("/api/delete-account", json={"confirm": True})
    assert r.status_code == 200, r.text
    c = db.connect()
    try:
        n = db.one(c, "SELECT COUNT(*) AS n FROM activity_offers WHERE proposer_id=?", (pair["a"],))["n"]
    finally:
        c.close()
    assert n == 0


# ------------------------------------------------------------------ races


def test_two_simultaneous_proposals_yield_exactly_one_offer(pair, db_env):
    barrier = threading.Barrier(2)
    results = []

    def go(uid, tag, url):
        c = db.connect()
        try:
            barrier.wait()
            try:
                results.append(("ok", activity.propose(c, uid, pair["cid"], 1, tag, url)))
            except Exception as exc:  # SchedulingError
                results.append(("err", getattr(exc, "code", repr(exc))))
        finally:
            c.close()

    ts = [threading.Thread(target=go, args=(pair["a"], "climbing", GYM)),
          threading.Thread(target=go, args=(pair["b"], "coffee", CAFE))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(r[0] for r in results) == ["err", "ok"], results
    assert [r[1] for r in results if r[0] == "err"] == ["offer_pending"]
    c = db.connect()
    try:
        assert db.one(c, "SELECT COUNT(*) AS n FROM activity_offers")["n"] == 1
    finally:
        c.close()
