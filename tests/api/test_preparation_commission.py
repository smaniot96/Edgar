"""Commissioning freezes owned hero data without retaining a DB connection."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.api.schemas.preparation_setup import PreparationSetup
from apps.api.services.preparation_commission import capture_setup


@pytest.mark.asyncio
async def test_capture_owns_snapshot_and_releases_transaction(monkeypatch):
    from db.postgres import session

    hero = SimpleNamespace(
        id=7, name="Alba", character_class="Fighter", level=8, hp_max=61,
        base_stats={"str": 18}, base_inventory={"items": ["rope"]},
    )
    closed = False
    calls = []

    class DB:
        async def execute(self, query):
            calls.append(query)
            if len(calls) == 1:
                return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=1))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [hero]))

    @asynccontextmanager
    async def factory():
        nonlocal closed
        try:
            yield DB()
        finally:
            closed = True

    monkeypatch.setattr(session, "async_session_factory", factory)
    result = await capture_setup(PreparationSetup(character_ids=[7], max_cost_microusd=1000000))
    assert closed
    assert result["heroes"][0]["level"] == 8
    hero.base_stats["str"] = 4
    hero.base_inventory["items"].append("secret")
    assert result["heroes"][0]["stats"] == {"str": 18}
    assert result["heroes"][0]["inventory"] == {"items": ["rope"]}
    # Ownership is a SQL predicate, not a later client-side filter.
    assert "characters.owner_user_id" in str(calls[1])


@pytest.mark.asyncio
async def test_missing_setup_retains_no_invented_hero():
    assert await capture_setup(None) is None


@pytest.mark.asyncio
async def test_duplicate_heroes_rejected_before_database():
    with pytest.raises(HTTPException) as error:
        await capture_setup(PreparationSetup(character_ids=[7, 7], max_cost_microusd=1000000))
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "values",
    [
        {"max_image_calls": 2},
        {"max_image_cost_microusd": 100},
        {"max_image_calls": 2, "max_image_cost_microusd": 1_000_001},
    ],
)
def test_image_budget_requires_paired_caps_within_total(values):
    with pytest.raises(ValueError):
        PreparationSetup(character_ids=[7], max_cost_microusd=1_000_000, **values)
