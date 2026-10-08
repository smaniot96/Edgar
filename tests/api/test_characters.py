import pytest


@pytest.mark.asyncio
async def test_character_delete_requires_awaited_delete(api_client) -> None:
    """Regression guard: AsyncSession.delete is awaitable (plan 08 verification)."""
    await api_client.post("/api/seed")
    listed = await api_client.get("/api/characters")
    assert listed.status_code == 200
    rows = listed.json()
    assert rows
    cid = rows[0]["id"]

    d = await api_client.delete(f"/api/characters/{cid}")
    assert d.status_code == 204

    missing = await api_client.get(f"/api/characters/{cid}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_list_characters_includes_current_assignment(api_client) -> None:
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]
    free = await api_client.post(
        "/api/characters",
        json={"name": "Free", "character_class": "Wizard", "level": 1, "hp_max": 6},
    )
    assert free.status_code == 201

    rows = {r["name"]: r for r in (await api_client.get("/api/characters")).json()}
    assert rows["Eda"]["current_assignment"]["campaign_id"] == cid
    assert rows["Free"]["current_assignment"] is None


@pytest.mark.asyncio
async def test_patch_assignment_hp_cannot_exceed_max(api_client) -> None:
    seed = await api_client.post("/api/seed")
    cid = seed.json()["campaign_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]
    url = f"/api/campaigns/{cid}/characters/{char_id}"

    assert (await api_client.patch(url, json={"hp_current": 99})).status_code == 422
    assert (await api_client.patch(url, json={"hp_current": 5, "hp_max": 4})).status_code == 422
    assert (await api_client.patch(url, json={"hp_current": None})).status_code == 422
    ok = await api_client.patch(url, json={"hp_current": 3})
    assert ok.status_code == 200
    assert ok.json()["hp_current"] == 3
