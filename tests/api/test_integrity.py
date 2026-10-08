"""Referential integrity and validation guards (migration 10_integrity + PATCH validators)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from db.postgres.models import (
    NPC,
    Campaign,
    CharacterAssignment,
    CombatState,
    EventLog,
    Session,
    User,
    WorldFlag,
)


async def _count(db_session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    if where:
        stmt = stmt.where(*where)
    return (await db_session.execute(stmt)).scalar_one()


async def _add_session_children(db_session, session_id: int, campaign_id: int) -> None:
    db_session.add_all(
        [
            EventLog(session_id=session_id, event_type="narration", payload={"text": "hi"}),
            CombatState(session_id=session_id, initiative_order=[], round=1),
            WorldFlag(campaign_id=campaign_id, key="door", value="open"),
        ]
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_delete_session_with_children(api_client, db_session) -> None:
    seed = (await api_client.post("/api/seed")).json()
    sid, cid = seed["session_id"], seed["campaign_id"]
    await _add_session_children(db_session, sid, cid)

    resp = await api_client.delete(f"/api/sessions/{sid}")
    assert resp.status_code == 204
    assert await _count(db_session, EventLog, EventLog.session_id == sid) == 0
    assert await _count(db_session, CombatState, CombatState.session_id == sid) == 0
    # Flags belong to the campaign, not the session.
    assert await _count(db_session, WorldFlag, WorldFlag.campaign_id == cid) == 1


@pytest.mark.asyncio
async def test_delete_campaign_with_children(api_client, db_session) -> None:
    seed = (await api_client.post("/api/seed")).json()
    sid, cid = seed["session_id"], seed["campaign_id"]
    await _add_session_children(db_session, sid, cid)
    npc = await api_client.post(
        f"/api/campaigns/{cid}/npcs", json={"name": "Grim", "disposition": "hostile"}
    )
    assert npc.status_code == 201

    resp = await api_client.delete(f"/api/campaigns/{cid}")
    assert resp.status_code == 204
    for model, col in (
        (Session, Session.campaign_id),
        (NPC, NPC.campaign_id),
        (WorldFlag, WorldFlag.campaign_id),
        (CharacterAssignment, CharacterAssignment.campaign_id),
    ):
        assert await _count(db_session, model, col == cid) == 0, model.__name__
    assert await _count(db_session, EventLog) == 0
    assert await _count(db_session, CombatState) == 0
    # The character library survives; only its assignment to the campaign is gone.
    chars = await api_client.get("/api/characters")
    assert chars.status_code == 200 and chars.json()


@pytest.mark.asyncio
async def test_delete_character_clears_session_pointer(api_client, db_session) -> None:
    seed = (await api_client.post("/api/seed")).json()
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]

    assert (await api_client.delete(f"/api/characters/{char_id}")).status_code == 204
    active = (
        await db_session.execute(
            select(Session.active_character_id)
            .where(Session.id == seed["session_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert active is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("/api/campaigns/{cid}", "title"),
        ("/api/campaigns/{cid}", "adventure_collections"),
        ("/api/characters/{char_id}", "name"),
        ("/api/characters/{char_id}", "hp_max"),
        ("/api/npcs/{npc_id}", "disposition"),
        ("/api/sessions/{sid}", "started_at"),
    ],
)
async def test_patch_null_on_non_nullable_is_422(api_client, path, field) -> None:
    seed = (await api_client.post("/api/seed")).json()
    cid, sid = seed["campaign_id"], seed["session_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    npc_id = (
        await api_client.post(
            f"/api/campaigns/{cid}/npcs", json={"name": "Grim", "disposition": "hostile"}
        )
    ).json()["id"]

    url = path.format(cid=cid, sid=sid, char_id=char_id, npc_id=npc_id)
    resp = await api_client.patch(url, json={field: None})
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_nullable_session_field_still_accepts_null(api_client) -> None:
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    resp = await api_client.patch(f"/api/sessions/{sid}", json={"ended_at": None})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_world_flag_rejected_by_db(api_client, db_session) -> None:
    cid = (await api_client.post("/api/seed")).json()["campaign_id"]
    for value in ("a", "b"):
        resp = await api_client.post(
            f"/api/campaigns/{cid}/world-flags", json={"key": "gate", "value": value}
        )
        assert resp.status_code == 201
    flags = (await api_client.get(f"/api/campaigns/{cid}/world-flags")).json()
    assert [(f["key"], f["value"]) for f in flags] == [("gate", "b")]

    db_session.add(WorldFlag(campaign_id=cid, key="gate", value="dup"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_hp_check_constraint(api_client, db_session) -> None:
    await api_client.post("/api/seed")
    assignment = (await db_session.execute(select(CharacterAssignment))).scalars().first()
    assignment.hp_current = assignment.hp_max + 1
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_seed_is_idempotent_and_reuses_setup_user(api_client, db_session) -> None:
    created = await api_client.post(
        "/api/users", json={"email": "me@example.com", "display_name": "Me"}
    )
    assert created.status_code == 201
    uid = created.json()["id"]

    first = (await api_client.post("/api/seed")).json()
    second = (await api_client.post("/api/seed")).json()
    assert first == second
    assert first["user_id"] == uid
    assert await _count(db_session, User) == 1
    assert await _count(db_session, Campaign) == 1
    me = await api_client.get("/api/users/me")
    assert me.json()["id"] == uid


@pytest.mark.asyncio
async def test_concurrent_assign_returns_409(api_client, monkeypatch) -> None:
    """Two assigns racing past the pre-check: the partial unique index wins -> 409, not 500."""
    await api_client.post("/api/seed")
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]  # active in seed campaign
    other = await api_client.post("/api/campaigns", json={"title": "Other", "system": "5e"})
    assert other.status_code == 201

    async def _no_active(*_a, **_kw):
        return None  # simulate the other request not having committed yet

    monkeypatch.setattr(
        "apps.api.routers.campaign_characters.load_active_assignment_any_campaign", _no_active
    )
    resp = await api_client.post(
        f"/api/campaigns/{other.json()['id']}/characters", json={"character_id": char_id}
    )
    assert resp.status_code == 409
    assert isinstance(resp.json()["detail"], str)


@pytest.mark.asyncio
async def test_assign_conflict_detail_is_string_with_context(api_client) -> None:
    seed = (await api_client.post("/api/seed")).json()
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    other = (await api_client.post("/api/campaigns", json={"title": "O", "system": "5e"})).json()

    resp = await api_client.post(
        f"/api/campaigns/{other['id']}/characters", json={"character_id": char_id}
    )
    assert resp.status_code == 409
    body = resp.json()
    assert isinstance(body["detail"], str)
    assert body["active_campaign_id"] == seed["campaign_id"]


@pytest.mark.asyncio
async def test_root_redirects_to_ui(api_client) -> None:
    resp = await api_client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/ui/"


@pytest.mark.asyncio
async def test_ready_reports_each_component(api_client, monkeypatch) -> None:
    async def _qdrant_ok() -> None:
        return None

    monkeypatch.setattr("apps.api.routers.health._check_qdrant", _qdrant_ok)
    ok = await api_client.get("/ready")
    assert ok.status_code == 200
    assert ok.json()["checks"] == {"postgres": "ok", "redis": "ok", "qdrant": "ok"}

    async def _qdrant_down() -> None:
        raise ConnectionError("down")

    monkeypatch.setattr("apps.api.routers.health._check_qdrant", _qdrant_down)
    down = await api_client.get("/ready")
    assert down.status_code == 503
    assert down.json()["checks"]["qdrant"] == "unavailable"
