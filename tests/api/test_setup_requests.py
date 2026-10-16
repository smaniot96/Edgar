"""Guided setup retries survive response loss without guessing from record lists."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from apps.api.schemas.character import CharacterCreate
from apps.api.schemas.session import SessionCreate
from apps.api.services.setup_requests import creation_identity, creation_replay
from db.postgres.models import Character


def hero(**changes):
    return {"name": "Aster", "character_class": "Fighter", "level": 1, "hp_max": 10, **changes}


def test_creation_digest_is_canonical_and_excludes_transport_key():
    key = uuid4()
    first = CharacterCreate(**hero(request_id=key, base_stats={"dex": 12, "str": 16}))
    reordered = CharacterCreate(**hero(request_id=uuid4(), base_stats={"str": 16, "dex": 12}))
    identity = creation_identity(first)
    assert identity["creation_request_id"] == str(key)
    assert identity["creation_request_hash"] == creation_identity(reordered)["creation_request_hash"]
    assert identity["creation_request_hash"] != creation_identity(first.model_copy(update={"hp_max": 11}))["creation_request_hash"]
    assert creation_identity(CharacterCreate(**hero())) == {"creation_request_id": None, "creation_request_hash": None}


async def test_replay_locks_before_read_and_rejects_changed_intent():
    body = CharacterCreate(**hero(request_id=uuid4()))
    calls = []
    db = SimpleNamespace(
        execute=AsyncMock(side_effect=lambda *_: calls.append("lock")),
        scalar=AsyncMock(side_effect=lambda *_: calls.append("read") or SimpleNamespace(creation_request_hash="different")),
    )
    with pytest.raises(HTTPException) as exc:
        await creation_replay(db, Character, 1, body)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "validation_failed"
    assert calls == ["lock", "read"]


async def test_unkeyed_creation_does_not_acquire_setup_lock():
    db = SimpleNamespace(execute=AsyncMock(), scalar=AsyncMock())
    assert await creation_replay(db, Character, 1, CharacterCreate(**hero())) is None
    db.execute.assert_not_called()
    db.scalar.assert_not_called()


async def test_hero_replay_uses_original_id_and_does_not_overwrite_edits(api_client):
    await api_client.post("/api/seed")
    payload = hero(request_id=str(uuid4()))
    created = await api_client.post("/api/characters", json=payload)
    assert created.status_code == 201
    cid = created.json()["id"]
    # Simulate a lost create response, then an edit elsewhere before the retry arrives.
    await api_client.patch(f"/api/characters/{cid}", json={"name": "Aster the Wise"})
    replay = await api_client.post("/api/characters", json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == cid
    assert replay.json()["name"] == "Aster the Wise"
    changed = await api_client.post("/api/characters", json={**payload, "hp_max": 12})
    assert changed.status_code == 409
    rows = (await api_client.get("/api/characters")).json()
    assert sum(row["id"] == cid for row in rows) == 1
    fresh = await api_client.post("/api/characters", json={**payload, "request_id": str(uuid4())})
    assert fresh.json()["id"] != cid


async def test_save_replay_is_scoped_and_checks_original_payload(api_client, fake_retrieval):
    seed = (await api_client.post("/api/seed")).json()
    cid = seed["campaign_id"]
    character_id = (await api_client.get("/api/characters")).json()[0]["id"]
    payload = {"campaign_id": cid, "active_character_id": character_id, "request_id": str(uuid4())}
    before = len((await api_client.get(f"/api/sessions?campaign_id={cid}")).json())
    created = await api_client.post("/api/sessions", json=payload)
    assert created.status_code == 200, created.text
    sid = created.json()["id"]
    replay = await api_client.post("/api/sessions", json=payload)
    assert replay.status_code == 200
    assert replay.json()["id"] == sid
    assert len((await api_client.get(f"/api/sessions?campaign_id={cid}")).json()) == before + 1
    mismatch = await api_client.post("/api/sessions", json={**payload, "active_character_id": None})
    assert mismatch.status_code == 409
    fresh = await api_client.post("/api/sessions", json={**payload, "request_id": str(uuid4())})
    assert fresh.status_code == 200
    assert fresh.json()["id"] != sid


async def test_concurrent_hero_requests_create_one_row(api_client):
    import asyncio

    from apps.api.routers.character import create_character
    from db.postgres import session as sessions

    await api_client.post("/api/seed")
    owner_id = (await api_client.get("/api/users/me")).json()["id"]
    body = CharacterCreate(**hero(request_id=uuid4()))

    async def create():
        async with sessions.async_session_factory() as db:
            return await create_character(body, db, owner_id)

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id
    async with sessions.async_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Character).where(
            Character.owner_user_id == owner_id,
            Character.creation_request_id == str(body.request_id),
        )) == 1


@pytest.mark.parametrize("schema, payload", [(CharacterCreate, hero()), (SessionCreate, {"campaign_id": 1})])
def test_request_ids_must_be_uuid(schema, payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schema(**payload, request_id="arbitrary-unbounded-client-input")


async def test_concurrent_save_requests_create_one_row(api_client, fake_retrieval):
    import asyncio

    from apps.api.routers.session import create_session
    from db.postgres import session as sessions
    from db.postgres.models import Session as SessionModel

    seed = (await api_client.post("/api/seed")).json()
    character_id = (await api_client.get("/api/characters")).json()[0]["id"]
    body = SessionCreate(
        campaign_id=seed["campaign_id"], active_character_id=character_id, request_id=uuid4(),
    )

    async def create():
        async with sessions.async_session_factory() as db:
            return await create_session(body, db)

    first, second = await asyncio.gather(create(), create())
    assert first.id == second.id
    async with sessions.async_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(SessionModel).where(
            SessionModel.campaign_id == body.campaign_id,
            SessionModel.creation_request_id == str(body.request_id),
        )) == 1
