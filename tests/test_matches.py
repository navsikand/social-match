"""Lane A: GET /api/matches + the /matches page (CONTRACT-photos.md §3, PLAN §A).

Every row is an authorization decision made at read time; `wording.label` only renders the
person after those checks pass. Seeds come from tests/util.py, state changes are direct SQL so a
test names exactly the one grant/row it revokes.
"""
import pytest

from server import db
from tests import util

WINDOW = ("2026-10-29T19:00:00", "2026-10-29T21:00:00")
PAST_WINDOW = ("2026-10-20T19:00:00", "2026-10-20T21:00:00")
TZ = "America/Los_Angeles"


# ------------------------------------------------------------------ helpers


@pytest.fixture()
def seeded(db_env):
    db.init_db()
    return db_env


def _sql(stmt, args=()):
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(stmt, args)
    finally:
        c.close()


def _pair(window=WINDOW, tz=TZ, **kw):
    a = util.mk_user("ava@example.com", "Ava", avatar_id=3)
    b = util.mk_user("bea@example.com", "Bea", avatar_id=4)
    cid, pid = util.mk_candidate(a, b, window=window, tz=tz, **kw)
    return a, b, cid, pid


def _function_body(html: str, name: str) -> str:
    """The brace-balanced body of `function name(...) { ... }` in rendered inline JS."""
    at = html.index("function " + name + "(")
    start = html.index("{", at)
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    raise AssertionError(f"unbalanced function {name}")


def _confirmed(cid, pid, a, b):
    """Both sides confirmed: candidate matched, proposal both_confirmed, two confirmation rows."""
    now = db.utcnow()
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
            c.execute("UPDATE proposals SET state='both_confirmed' WHERE id=?", (pid,))
            for uid in (a, b):
                c.execute(
                    "INSERT OR IGNORE INTO proposal_confirmations (proposal_id, user_id, confirmed_at)"
                    " VALUES (?,?,?)",
                    (pid, uid, now),
                )
    finally:
        c.close()


def _rows(client, uid):
    util.direct_session(client, uid)
    r = client.get("/api/matches", headers={"Accept": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()["matches"]


# ------------------------------------------------------------------ happy path


def test_two_sided_confirm_appears_for_both_users_with_own_label(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    # Only A publishes their name; B keeps the pseudonymous card grant.
    util.pub_name(a)

    a_view = _rows(client, a)
    b_view = _rows(client, b)
    assert [m["candidate_id"] for m in a_view] == [cid]
    assert [m["candidate_id"] for m in b_view] == [cid]

    # `about` is always the counterpart. Owner's call: the real name is shown regardless of
    # the publication tier, so both sides read a name.
    assert a_view[0]["about"] == "Bea"
    assert b_view[0]["about"] == "Ava"
    assert a_view[0]["state"] == "matched"
    assert a_view[0]["historical"] is False
    assert a_view[0]["card_url"].endswith(f"/m/{cid}")
    assert "·" in a_view[0]["window"]["display_with_date"]
    assert a_view[0]["window"]["display"] in a_view[0]["window"]["display_with_date"]
    assert a_view[0]["confirmed_at"]


def test_real_name_is_always_used_on_matches(seeded, client):
    """Owner's call, superseding the tier-gated label: a confirmed plan shows the real name."""
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    row = _rows(client, a)[0]
    assert row["about"] == "Bea"


# ------------------------------------------------------------------ never listed


def test_proposed_and_a_confirmed_never_appear(seeded, client):
    a, b, cid, pid = _pair()
    # proposal still 'proposed'
    assert _rows(client, a) == []
    _sql("UPDATE proposals SET state='a_confirmed' WHERE id=?", (pid,))
    _sql("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
    assert _rows(client, a) == []


def test_blocked_pair_absent_both_directions(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    ah, bh = db.user_hash(a), db.user_hash(b)
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO blocks (user_hash, blocked_user_hash, created_at, expires_at)"
                " VALUES (?,?,?,?)",
                (ah, bh, db.utcnow(), db.utcnow(days=30)),
            )
    finally:
        c.close()
    assert _rows(client, a) == []
    assert _rows(client, b) == []


def test_counterpart_withdraws_matching_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='matching'", (db.utcnow(), b))
    assert _rows(client, a) == []


def test_counterpart_withdraws_publication_card_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql(
        "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='publication'",
        (db.utcnow(), b),
    )
    assert _rows(client, a) == []


def test_expired_availability_row_disappears(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE availability SET expires_at='2000-01-01T00:00:00Z' WHERE user_id=?", (b,))
    assert _rows(client, a) == []
    assert _rows(client, b) == []


def test_hidden_candidate_absent(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE candidates SET state='hidden' WHERE id=?", (cid,))
    assert _rows(client, a) == []
    assert _rows(client, b) == []


# ------------------------------------------------------------------ current proposal


def test_counter_superseded_proposal_shows_only_current(seeded, client):
    a, b, cid, pid = _pair()
    new_start = util.to_utc("2026-10-30T18:00:00", TZ)
    new_end = util.to_utc("2026-10-30T20:00:00", TZ)
    c = util.conn()
    try:
        with db.immediate(c):
            c.execute(
                "INSERT INTO proposals (candidate_id, window_start, window_end, version, state,"
                " expires_at) VALUES (?,?,?,2,'both_confirmed',?)",
                (cid, new_start, new_end, db.utcnow(days=30)),
            )
            pid2 = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute(
                "UPDATE proposals SET state='voided', superseded_by=? WHERE id=?", (pid2, pid)
            )
            c.execute("UPDATE candidates SET state='matched' WHERE id=?", (cid,))
            for uid in (a, b):
                c.execute(
                    "INSERT INTO proposal_confirmations (proposal_id, user_id, confirmed_at)"
                    " VALUES (?,?,?)",
                    (pid2, uid, db.utcnow()),
                )
    finally:
        c.close()

    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["window"]["start_utc"] == new_start
    assert rows[0]["window"]["end_utc"] == new_end


# ------------------------------------------------------------------ history


def test_past_window_is_historical_without_card_link(seeded, client):
    a, b, cid, pid = _pair(window=PAST_WINDOW)
    _confirmed(cid, pid, a, b)
    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["historical"] is True
    assert rows[0]["card_url"] is None
    # date suffix present, computed in the window's tz ('20 Oct' in America/Los_Angeles)
    assert rows[0]["window"]["display_with_date"].endswith("· 20 Oct")


def test_closed_candidate_still_listed_while_authorized(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE candidates SET state='closed' WHERE id=?", (cid,))
    _sql("UPDATE proposals SET state='closed' WHERE id=?", (pid,))
    rows = _rows(client, a)
    assert len(rows) == 1
    assert rows[0]["state"] == "closed"


# ------------------------------------------------------------------ auth / page


def test_unauthenticated_matches_is_401(seeded, client):
    r = client.get("/api/matches")
    assert r.status_code == 401


def test_matches_page_redirects_to_login(seeded, client):
    r = client.get("/matches", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/matches"


# ------------------------------------------------------------------ A2 / C6: dashboard rows
#
# The dashboard row must name the person and the time, and say whose confirmation it is
# waiting on, instead of printing an internal state word.


def _confirm(client, uid, cid, version=1):
    util.direct_session(client, uid)
    r = client.post(
        "/api/swipe",
        json={"candidate_id": cid, "proposal_version": version, "direction": "right"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _dashboard(client, uid):
    util.direct_session(client, uid)
    r = client.get("/api/dashboard")
    assert r.status_code == 200, r.text
    return r.json()["candidates"]


def test_dashboard_row_carries_person_window_and_flags(seeded, client):
    a, b, cid, pid = _pair()
    rows = _dashboard(client, a)
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_id"] == cid
    assert row["about"]["display_name"] == "Bea"
    assert row["window"]["display"] == "Thu 19:00–21:00"
    assert row["state"] == "open"  # existing consumers keep their field
    assert row["awaiting_you"] is False
    assert row["awaiting_them"] is False


def test_dashboard_flags_who_is_waiting_on_whom(seeded, client):
    a, b, cid, pid = _pair()
    body = _confirm(client, a, cid)
    assert body["state"] == "a_confirmed"

    mine = _dashboard(client, a)[0]
    assert mine["awaiting_them"] is True
    assert mine["awaiting_you"] is False

    theirs = _dashboard(client, b)[0]
    assert theirs["awaiting_you"] is True
    assert theirs["awaiting_them"] is False


def test_card_confirmation_branches_keep_revoke_and_release_busy(seeded, client):
    """BLOCKER-4 (structural: pytest cannot execute this inline JS).

    Both confirmation outcomes retire ONLY the decision controls; #revoke survives, busy is
    released on the success path so a poll can run, and a polled render re-shows the
    confirmation the first confirmer was waiting on.
    """
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    util.direct_session(client, a)
    html = client.get(f"/m/{cid}").text

    hidden = html.split('data-matched-hide="', 1)[1].split('"', 1)[0].split()
    assert "revoke" not in hidden
    assert {"yes", "no", "block", "report", "counter-toggle"} == set(hidden)

    hide = _function_body(html, "hideDecisionControls")
    assert "[data-matched-hide]" in hide
    # The old bug hid every button in .actions, which took #revoke with it.
    assert 'querySelectorAll(".actions button")' not in hide

    waiting = _function_body(html, "showWaiting")
    confirmed = _function_body(html, "showConfirmedPlan")
    assert "hideDecisionControls();" in waiting
    assert "hideDecisionControls();" in confirmed
    assert 'querySelectorAll(".actions button")' not in waiting
    assert 'querySelectorAll(".actions button")' not in confirmed

    swipe = _function_body(html, "swipe")
    right = swipe.split('if (direction === "right") {', 1)[1].split("const outcome", 1)[0]
    # busy must be released after EITHER settlement, or the poller never runs again.
    assert right.index("showConfirmedPlan();") < right.index("setBusy(false);")
    assert right.index("showWaiting(cardName);") < right.index("setBusy(false);")

    render = _function_body(html, "render")
    matched = render.split('if (data.state === "matched")', 1)[1]
    assert "showConfirmedPlan();" in matched


def test_all_pollers_skip_hidden_overlap_and_stale_responses(seeded, client):
    """REGRESSION-4 (structural): the same guarded poller on card, dashboard and matches."""
    a, b, cid, pid = _pair()
    util.direct_session(client, a)
    pages = {
        "dashboard": client.get("/").text,
        "matches": client.get("/matches").text,
        "card": client.get(f"/m/{cid}").text,
    }
    for name, html in pages.items():
        load_name, poll_name = ("loadCard", "pollCard") if name == "card" else ("load", "poll")
        load = _function_body(html, load_name)
        poll = _function_body(html, poll_name)
        assert "const seq = ++pollSeq;" in load, name
        assert "if (seq !== pollSeq) return;" in load, name
        assert "document.hidden" in poll, name
        assert "pollInFlight" in poll, name
        # No overlapping request: the in-flight flag is set before the awaited load.
        assert poll.index("pollInFlight = true") < poll.index("await " + load_name), name
        assert "pollInFlight = false" in poll, name
        resume = html.split('document.addEventListener("visibilitychange"', 1)[1]
        assert "if (!document.hidden) " + poll_name + "();" in resume.split("});", 1)[0], name


def test_card_page_disables_yes_while_offer_pending(seeded, client):
    """A6: the CTA shares the negotiation note's sentence and is disabled while offered."""
    a, b, cid, pid = _pair()
    util.direct_session(client, a)
    html = client.get(f"/m/{cid}").text
    assert 'a.state === "offered"' in html
    assert "offerPending" in html
    assert "You can confirm once your agents agree." in html
    assert 'document.getElementById("yes").title' in html
    # Polling wiring: the card reloads itself while visible.
    assert "window.setInterval" in html
    assert "visibilitychange" in html


def test_card_page_counter_prefills_from_window(seeded, client):
    """C7: the suggestion form starts from the card's window in the card's zone."""
    a, b, cid, pid = _pair()
    util.direct_session(client, a)
    html = client.get(f"/m/{cid}").text
    assert "prefillCounter" in html
    assert "localInput" in html
    assert "ensureTzOption" in html
    assert ".min = nowLocal" in html
    assert "Both of you must be free then." in html


# ------------------------------------------------------------------ B7: no invented cards


def test_dashboard_page_never_fabricates_a_card(seeded, client):
    a = util.mk_user("ava@example.com", "Ava")
    util.direct_session(client, a)
    html = client.get("/").text
    assert "PLACEHOLDER_CANDIDATES" not in html
    assert "sample_window" not in html
    assert 'window.location.href = "/login?next="' in html
    assert 'textContent = "Retry"' in html


# ------------------------------------------------------------------ B4: expiry notice


def test_matches_reports_availability_lapse_anonymously(seeded, client):
    a, b, cid, pid = _pair()
    _confirmed(cid, pid, a, b)
    _sql("UPDATE availability SET expires_at='2000-01-01T00:00:00Z' WHERE user_id=?", (b,))
    util.direct_session(client, a)
    r = client.get("/api/matches")
    assert r.status_code == 200
    body = r.json()
    assert body["matches"] == []
    assert body["availability_lapsed"] is True
    # Nothing identifying leaks with the flag.
    assert "Bea" not in r.text

    util.direct_session(client, a)
    html = client.get("/matches").text
    assert "Your saved free time expired" in html


# ------------------------------------------------------------------ B2: duplicate counter


def test_identical_counter_is_rejected_server_side(seeded, client):
    a, b, cid, pid = _pair()
    util.direct_session(client, a)
    body = {
        "candidate_id": cid,
        "window_start": "2026-10-29T19:00",
        "window_end": "2026-10-29T20:00",
        "tz": TZ,
    }
    first = client.post("/api/counter", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["proposal_version"] == 2

    again = client.post("/api/counter", json=body)
    assert again.status_code == 409, again.text
    assert again.json()["error"] == "duplicate_counter"
    # No second version was minted.
    c = util.conn()
    try:
        versions = [row["version"] for row in db.q(
            c, "SELECT version FROM proposals WHERE candidate_id=? ORDER BY version", (cid,)
        )]
    finally:
        c.close()
    assert versions == [1, 2]
