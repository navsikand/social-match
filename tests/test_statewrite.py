"""Mode-split tests for the shared state-write service (CONTRACT-chat.md §3).

The full existing suite is the agent-mode regression proof; these tests pin the chat-mode
grant-preserving contract and the two agent-mode semantics post_state must keep. Time is
frozen with `db._FIXED_NOW` — no sleeps, no network.
"""
import pytest

from server import db, statewrite
from server.schema import StatePayload
from tests import util

FROZEN = "2026-10-24T12:00:00Z"
TZ = "America/Los_Angeles"
WINDOW = {"start": "2026-10-29T19:00:00", "end": "2026-10-29T21:00:00", "tz": TZ}
GRANT_SELECT = (
    "SELECT id, user_id, kind, tier, version, fields_json, audience, granted_at, expires_at,"
    " withdrawn_at FROM grants ORDER BY id"
)


@pytest.fixture(autouse=True)
def frozen(db_env):
    db.init_db()  # the `client` fixture normally does this via app lifespan
    previous = db._FIXED_NOW
    db._FIXED_NOW = FROZEN
    try:
        yield FROZEN
    finally:
        db._FIXED_NOW = previous


def make_body(**overrides) -> dict:
    body = {
        "windows": [dict(WINDOW)],
        "tags": ["matcha"],
        "wants": ["hangout"],
        "free_time": {"source": "student"},
        "consent": {"matching": True, "publication": {"tier": "card"}},
        "busy_load": 2,
    }
    body.update(overrides)
    return body


def make_payload(**overrides) -> StatePayload:
    return StatePayload.model_validate(make_body(**overrides))


def write(uid: int, payload: StatePayload, mode: str = "chat") -> dict:
    conn = util.conn()
    try:
        with db.immediate(conn):
            return statewrite.write_state(conn, uid, payload, mode=mode)
    finally:
        conn.close()


def rows(sql: str, args: tuple = ()):  # noqa: ANN001
    conn = util.conn()
    try:
        return db.q(conn, sql, args)
    finally:
        conn.close()


def one(sql: str, args: tuple = ()):  # noqa: ANN001
    conn = util.conn()
    try:
        return db.one(conn, sql, args)
    finally:
        conn.close()


def grant_dump() -> list[tuple]:
    return [tuple(r) for r in rows(GRANT_SELECT)]


# ---------------------------------------------------------------- chat mode: data yes, consent no


def test_chat_mode_writes_data_never_consent():
    uid = util.mk_user("ava@example.com", "Ava")
    util.grant(uid, "matching")
    util.pub_name(uid)
    before = grant_dump()
    assert len(before) == 2

    out = write(uid, make_payload())

    # Data landed.
    assert [r["starts_at"] for r in rows(
        "SELECT starts_at FROM availability_windows WHERE user_id=?", (uid,)
    )] == [util.to_utc(WINDOW["start"], TZ)]
    assert [r["tag"] for r in rows("SELECT tag FROM hobby_tags WHERE user_id=?", (uid,))] == [
        "coffee"  # "matcha" canonicalised
    ]
    assert [r["raw"] for r in rows("SELECT raw FROM interest_raw WHERE user_id=?", (uid,))] == [
        "matcha"
    ]
    assert [r["want"] for r in rows("SELECT want FROM user_wants WHERE user_id=?", (uid,))] == [
        "hangout"
    ]
    assert one("SELECT busy_load FROM user_load WHERE user_id=?", (uid,))["busy_load"] == 2
    assert out["interests"]["scored"] == ["coffee"]

    # Every grants row is byte-identical: no insert, renew, restore or withdraw.
    assert grant_dump() == before

    # Exactly one audit row, chat-scoped; no grant events at all.
    log = rows("SELECT action, scope FROM consent_log WHERE user_id_hash=?", (db.user_hash(uid),))
    assert [(r["action"], r["scope"]) for r in log] == [("state:written", "chat")]
    assert rows(
        "SELECT 1 FROM consent_log WHERE action IN ('grant:matching','grant:publication')"
    ) == []


def test_chat_mode_cannot_resurrect_withdrawn_grant():
    uid = util.mk_user("ben@example.com", "Ben")
    util.grant(uid, "matching")
    conn = util.conn()
    try:
        with db.immediate(conn):
            conn.execute(
                "UPDATE grants SET withdrawn_at=? WHERE user_id=? AND kind='matching'",
                (FROZEN, uid),
            )
    finally:
        conn.close()

    write(uid, make_payload(consent={"matching": True, "publication": {"tier": "card+name"}}))

    matching = one(
        "SELECT withdrawn_at, version FROM grants WHERE user_id=? AND kind='matching'", (uid,)
    )
    assert matching["withdrawn_at"] == FROZEN
    assert matching["version"] == 1
    assert rows("SELECT 1 FROM grants WHERE user_id=? AND kind='publication'", (uid,)) == []
    # Availability still stored; it is inert while no live matching grant exists.
    assert len(rows("SELECT 1 FROM availability_windows WHERE user_id=?", (uid,))) == 1


# ---------------------------------------------------------------- agent mode: unchanged


def _post_state(client, token: str, body: dict):
    return client.post("/api/state", json=body, headers=util.auth_header(token))


def test_agent_refusal_runs_leave_cascade_exactly_once(client):
    uid = util.mk_user("cara@example.com", "Cara")
    token, _agent_id = util.mint_pairing(uid)
    util.grant(uid, "matching")
    util.windows(uid, [(WINDOW["start"], WINDOW["end"])], tz=TZ)

    body = make_body(consent={"matching": False, "publication": None})
    resp = _post_state(client, token, body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["changed"] is True
    assert out["interests"] == {"scored": [], "mapped": [], "kept_unscored": []}
    assert out["events"] == {"stored": []}

    assert one(
        "SELECT withdrawn_at FROM grants WHERE user_id=? AND kind='matching'", (uid,)
    )["withdrawn_at"] == FROZEN
    assert rows("SELECT 1 FROM availability WHERE user_id=?", (uid,)) == []
    assert rows("SELECT 1 FROM availability_windows WHERE user_id=?", (uid,)) == []
    audits = rows(
        "SELECT action FROM consent_log WHERE user_id_hash=? AND action='withdraw:matching'",
        (db.user_hash(uid),),
    )
    assert len(audits) == 1  # exactly one, no duplicate withdraw:matching


def test_agent_publication_renews_and_withdraws(client):
    uid = util.mk_user("dana@example.com", "Dana")
    token, _agent_id = util.mint_pairing(uid)

    resp = _post_state(client, token, make_body(consent={"matching": True, "publication": {"tier": "card"}}))
    assert resp.status_code == 201, resp.text

    resp = _post_state(
        client, token, make_body(consent={"matching": True, "publication": {"tier": "card+name"}})
    )
    assert resp.status_code == 201, resp.text

    pub = rows(
        "SELECT tier, version, withdrawn_at FROM grants WHERE user_id=? AND kind='publication'"
        " ORDER BY version",
        (uid,),
    )
    assert [(r["tier"], r["version"]) for r in pub] == [("card", 1), ("card+name", 2)]
    assert pub[0]["withdrawn_at"] == FROZEN
    assert pub[1]["withdrawn_at"] is None

    # Omitting publication on the next payload withdraws the live tier.
    resp = _post_state(client, token, make_body(consent={"matching": True, "publication": None}))
    assert resp.status_code == 201, resp.text
    pub = rows(
        "SELECT withdrawn_at FROM grants WHERE user_id=? AND kind='publication' ORDER BY version",
        (uid,),
    )
    assert pub[0]["withdrawn_at"] == FROZEN
    assert pub[1]["withdrawn_at"] == FROZEN
    assert len(rows(
        "SELECT 1 FROM consent_log WHERE user_id_hash=? AND action='withdraw:publication'",
        (db.user_hash(uid),),
    )) == 1


def test_unknown_mode_raises_value_error():
    uid = util.mk_user("eve@example.com", "Eve")
    conn = util.conn()
    try:
        with db.immediate(conn), pytest.raises(ValueError):
            statewrite.write_state(conn, uid, make_payload(), mode="bogus")
    finally:
        conn.close()
