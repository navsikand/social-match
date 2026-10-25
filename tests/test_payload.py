"""POST /api/state allowlist + write-path tests (CONTRACT §5, §13 P1b). P1b owns this file.

Every test freezes `db._FIXED_NOW` so 30-day horizon checks and expiry timestamps are
deterministic; the fixture restores the previous value in a finally block.
"""
import json

import pytest
from pydantic import ValidationError

from server import config, db
from server import schema as payload_schema
from server.schema import StatePayload
from tests import util

FROZEN = "2026-10-24T12:00:00Z"
WINDOW = {"start": "2026-10-29T19:00", "end": "2026-10-29T21:00", "tz": "America/Los_Angeles"}


@pytest.fixture(autouse=True)
def frozen_now():
    previous = db._FIXED_NOW
    db._FIXED_NOW = FROZEN
    try:
        yield FROZEN
    finally:
        db._FIXED_NOW = previous


def make_payload(**overrides) -> dict:
    body = {
        "windows": [dict(WINDOW)],
        "tags": ["climbing", "live_music"],
        "wants": ["hangout", "event"],
        "free_time": {"source": "student"},
        "consent": {"matching": True, "publication": {"tier": "card+name"}},
    }
    body.update(overrides)
    return body


@pytest.fixture()
def student(client):
    """A user with a live pairing; returns (user_id, token, agent_id)."""
    uid = util.mk_user("ava@example.com", "Ava")
    token, agent_id = util.mint_pairing(uid)
    return uid, token, agent_id


def post(client, token, body):
    return client.post("/api/state", json=body, headers=util.auth_header(token))


def assert_forbidden(resp):
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "payload_forbidden"
    assert isinstance(body["detail"], str) and body["detail"]
    return body


def rows(sql: str, args: tuple = ()):
    c = util.conn()
    try:
        return db.q(c, sql, args)
    finally:
        c.close()


# ---- auth -------------------------------------------------------------------------


def test_state_requires_bearer(client):
    r = client.post("/api/state", json=make_payload())
    assert r.status_code == 401


# ---- 422 allowlist cases ----------------------------------------------------------


def test_unknown_top_level_key(client, student):
    _, token, _ = student
    body = make_payload(extra_notes="hello")
    assert "extra_notes" in assert_forbidden(post(client, token, body))["detail"]


def test_unknown_window_key(client, student):
    _, token, _ = student
    win = dict(WINDOW, location="library")
    assert_forbidden(post(client, token, make_payload(windows=[win])))


def test_unknown_nested_keys(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(free_time={"source": "student", "note": "x"})))
    assert_forbidden(
        post(client, token, make_payload(consent={"matching": True, "share": True}))
    )
    assert_forbidden(
        post(
            client,
            token,
            make_payload(consent={"matching": True, "publication": {"tier": "card", "name": "Ava"}}),
        )
    )


def test_too_many_windows(client, student):
    _, token, _ = student
    body = make_payload(windows=[dict(WINDOW) for _ in range(21)])
    assert_forbidden(post(client, token, body))


def test_twenty_windows_is_allowed(client, student):
    _, token, _ = student
    body = make_payload(windows=[dict(WINDOW) for _ in range(20)])
    assert post(client, token, body).status_code == 201


def test_start_not_before_end(client, student):
    _, token, _ = student
    for start, end in (
        ("2026-10-29T21:00", "2026-10-29T19:00"),
        ("2026-10-29T19:00", "2026-10-29T19:00"),
    ):
        assert_forbidden(post(client, token, make_payload(windows=[dict(WINDOW, start=start, end=end)])))


def test_duration_bound_is_eight_hours(client, student):
    _, token, _ = student
    exact = {"start": "2026-10-29T13:00", "end": "2026-10-29T21:00", "tz": WINDOW["tz"]}
    assert post(client, token, make_payload(windows=[exact])).status_code == 201
    over = {"start": "2026-10-29T12:59", "end": "2026-10-29T21:00", "tz": WINDOW["tz"]}
    assert_forbidden(post(client, token, make_payload(windows=[over])))


def test_bad_timezone(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(windows=[dict(WINDOW, tz="Mars/Olympus")])))


def test_window_outside_next_30_days(client, student):
    _, token, _ = student
    assert_forbidden(
        post(
            client,
            token,
            make_payload(windows=[{"start": "2026-12-01T19:00", "end": "2026-12-01T21:00", "tz": WINDOW["tz"]}]),
        )
    )
    assert_forbidden(
        post(
            client,
            token,
            make_payload(windows=[{"start": "2026-10-20T19:00", "end": "2026-10-20T21:00", "tz": WINDOW["tz"]}]),
        )
    )


def test_arbitrary_interest_is_accepted_and_canonicalised(client, student):
    """Front-desk complaint: "the matcher doesn't have a matcha tag".

    Interests are free text now; the server boils them down. "matcha" scores as coffee and
    is echoed back to the student as matcha -> coffee.
    """
    _, token, _ = student
    r = post(client, token, make_payload(tags=["climbing", "matcha"]))
    assert r.status_code == 201, r.text
    body = r.json()["interests"]
    assert body["scored"] == ["climbing", "coffee"]
    assert {"raw": "matcha", "tag": "coffee"} in body["mapped"]
    got = sorted(row["tag"] for row in rows("SELECT tag FROM hobby_tags"))
    assert got == ["climbing", "coffee"]


def test_unknown_interest_is_kept_but_not_scored(client, student):
    _, token, _ = student
    r = post(client, token, make_payload(tags=["climbing", "mycology"]))
    assert r.status_code == 201, r.text
    body = r.json()["interests"]
    assert body["scored"] == ["climbing"]
    assert body["kept_unscored"] == ["mycology"]
    assert rows("SELECT tag FROM hobby_tags WHERE tag='mycology'") == []


def test_long_tag_rejected(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(tags=["climbing", "x" * 33])))


def test_tag_with_control_characters_rejected(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(tags=["climbing\nTherapy - Dr. Ruiz"])))


def test_nine_distinct_tags_rejected(client, student):
    _, token, _ = student
    nine = ["climbing", "hiking", "running", "coffee", "cinema", "chess", "anime", "debate", "baking"]
    assert_forbidden(post(client, token, make_payload(tags=nine)))


def test_six_tags_after_dedup_allowed(client, student):
    _, token, _ = student
    tags = ["climbing", "climbing", "hiking", "running", "coffee", "cinema"]
    r = post(client, token, make_payload(tags=tags))
    assert r.status_code == 201, r.text
    got = [row["tag"] for row in rows("SELECT tag FROM hobby_tags ORDER BY tag")]
    assert got == sorted(["climbing", "hiking", "running", "coffee", "cinema"])


def test_wants_whitelist(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(wants=["hangout", "study_session"])))


def test_cubbie_computed_requires_ack(client, student):
    _, token, _ = student
    assert_forbidden(post(client, token, make_payload(free_time={"source": "cubbie_computed"})))
    ok = make_payload(
        free_time={"source": "cubbie_computed", "student_confirmed_at": "2026-10-24T09:10:00Z"}
    )
    assert post(client, token, ok).status_code == 201
    assert rows("SELECT source FROM availability")[0]["source"] == "cubbie_computed"


def test_non_dict_and_bad_json_bodies(client, student):
    _, token, _ = student
    r = client.post(
        "/api/state", content=b"[1,2,3]", headers={**util.auth_header(token), "content-type": "application/json"}
    )
    assert_forbidden(r)


# ---- happy path -------------------------------------------------------------------


def test_happy_path_writes_everything(client, student):
    uid, token, agent_id = student
    r = post(client, token, make_payload())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["changed"] is True
    assert out["expires_at"] == db.utcnow(days=config.state_ttl_days())

    avail = rows("SELECT * FROM availability WHERE user_id=?", (uid,))[0]
    assert avail["written_by_agent_id"] == agent_id
    assert avail["observed_at"] == FROZEN
    assert avail["expires_at"] == out["expires_at"]
    assert avail["source"] == "student"
    assert len(avail["payload_hash"]) == 64

    # canonical hash = sha256 of sorted-key json of the validated payload
    import hashlib

    canonical = json.dumps(
        StatePayload.model_validate(make_payload()).model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert avail["payload_hash"] == hashlib.sha256(canonical.encode()).hexdigest()

    wins = rows("SELECT * FROM availability_windows WHERE user_id=?", (uid,))
    assert len(wins) == 1
    # 19:00–21:00 America/Los_Angeles on 2026-10-29 (PDT, UTC-7) → 02:00–04:00Z next day
    assert wins[0]["starts_at"] == "2026-10-30T02:00:00Z"
    assert wins[0]["ends_at"] == "2026-10-30T04:00:00Z"
    assert wins[0]["tz"] == "America/Los_Angeles"

    assert [row["tag"] for row in rows("SELECT tag FROM hobby_tags ORDER BY tag")] == [
        "climbing",
        "live_music",
    ]

    grants = rows("SELECT * FROM grants WHERE user_id=? ORDER BY kind", (uid,))
    assert [(g["kind"], g["tier"], g["version"], g["withdrawn_at"]) for g in grants] == [
        ("matching", None, 1, None),
        ("publication", "card+name", 1, None),
    ]
    assert all(g["expires_at"] == db.utcnow(days=config.grant_ttl_days()) for g in grants)

    actions = [row["action"] for row in rows("SELECT action FROM consent_log ORDER BY id")]
    assert "state:received" in actions
    assert "grant:matching" in actions
    assert "grant:publication" in actions

    received = rows("SELECT * FROM consent_log WHERE action='state:received'")[0]
    detail = json.loads(received["detail_json"])
    assert detail["payload_hash"] == avail["payload_hash"]
    assert detail["student_confirmed_at"] is None


def test_windows_are_replaced_not_appended(client, student):
    uid, token, _ = student
    post(client, token, make_payload())
    post(client, token, make_payload(windows=[{"start": "2026-11-02T08:00", "end": "2026-11-02T09:00", "tz": "UTC"}]))
    wins = rows("SELECT * FROM availability_windows WHERE user_id=?", (uid,))
    assert len(wins) == 1
    assert wins[0]["starts_at"] == "2026-11-02T08:00:00Z"
    assert wins[0]["tz"] == "UTC"


def test_grant_versions_bump_and_old_row_withdrawn(client, student):
    uid, token, _ = student
    post(client, token, make_payload())
    post(client, token, make_payload())
    matching = rows("SELECT * FROM grants WHERE user_id=? AND kind='matching' ORDER BY version", (uid,))
    assert [g["version"] for g in matching] == [1, 2]
    assert matching[0]["withdrawn_at"] == FROZEN
    assert matching[1]["withdrawn_at"] is None


def test_changed_flag_flips_only_on_real_change(client, student):
    _, token, _ = student
    assert post(client, token, make_payload()).json()["changed"] is True
    assert post(client, token, make_payload()).json()["changed"] is False
    changed = post(client, token, make_payload(tags=["climbing"])).json()["changed"]
    assert changed is True


# ---- consent withdrawal semantics -------------------------------------------------


def test_false_matching_withdraws_live_grant(client, student):
    uid, token, _ = student
    post(client, token, make_payload())
    r = post(client, token, make_payload(consent={"matching": False}))
    assert r.status_code == 201, r.text

    assert rows("SELECT * FROM grants WHERE user_id=? AND kind='matching' AND withdrawn_at IS NULL", (uid,)) == []
    assert rows("SELECT * FROM grants WHERE user_id=? AND kind='publication' AND withdrawn_at IS NULL", (uid,)) == []
    actions = [row["action"] for row in rows("SELECT action FROM consent_log ORDER BY id")]
    assert "withdraw:matching" in actions
    assert "withdraw:publication" in actions
    # no new grant rows were created by the "off" payload
    assert len(rows("SELECT * FROM grants WHERE user_id=?", (uid,))) == 2


def test_publication_tier_change_upserts(client, student):
    uid, token, _ = student
    post(client, token, make_payload())
    post(client, token, make_payload(consent={"matching": True, "publication": {"tier": "card"}}))
    pub = rows(
        "SELECT * FROM grants WHERE user_id=? AND kind='publication' ORDER BY version", (uid,)
    )
    assert [(g["tier"], g["version"], g["withdrawn_at"]) for g in pub] == [
        ("card+name", 1, FROZEN),
        ("card", 2, None),
    ]


# ---- THE THERAPY TEST -------------------------------------------------------------


def test_therapy_titles_never_accepted(client, student):
    """The allowlist is the guarantee: a payload carrying calendar titles is 422 and nothing
    about it — title text included — ever reaches the database."""
    uid, token, _ = student
    body = make_payload()
    body["calendar_titles"] = ["Therapy - Dr. Ruiz", "CS 101 Lecture"]
    body["windows"] = [dict(WINDOW, title="Therapy - Dr. Ruiz")]

    r = post(client, token, body)
    assert_forbidden(r)
    assert "calendar_titles" in r.json()["detail"] or "title" in r.json()["detail"]

    c = util.conn()
    try:
        dump = []
        table_dumps = (
            "SELECT * FROM availability",
            "SELECT * FROM availability_windows",
            "SELECT * FROM hobby_tags",
            "SELECT * FROM grants",
            "SELECT * FROM consent_log",
        )
        for statement in table_dumps:
            for row in db.q(c, statement):
                dump.append(str(tuple(row)))
        assert "Therapy" not in "\n".join(dump)
        assert db.q(c, "SELECT * FROM availability WHERE user_id=?", (uid,)) == []
    finally:
        c.close()

    # And structurally: the canonical model has no place for a title.
    assert "calendar_titles" not in json.dumps(StatePayload.model_json_schema())
    with pytest.raises(ValidationError):
        StatePayload.model_validate(make_payload(calendar_titles=["Therapy - Dr. Ruiz"]))

    # The accepted canonical input is title-free by construction.
    accepted = StatePayload.model_validate(make_payload())
    assert "Therapy" not in json.dumps(accepted.model_dump())
    assert payload_schema.TAG_VOCAB and "climbing" in payload_schema.TAG_VOCAB
