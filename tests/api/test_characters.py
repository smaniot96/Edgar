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
        json={
            "name": "Free",
            "character_class": "Wizard",
            "level": 1,
            "hp_max": 6,
            "base_stats": {"STR": 8, "DEX": 14, "CON": 12, "INT": 17, "WIS": 13, "CHA": 10},
            "base_inventory": {"weapons": ["quarterstaff", "dagger"], "armor": "none", "spellbook": True},
        },
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


WIZARD_KIT = {
    "base_stats": {"STR": 8, "DEX": 14, "CON": 12, "INT": 17, "WIS": 13, "CHA": 10},
    "base_inventory": {"weapons": ["quarterstaff", "dagger"], "armor": "none", "spellbook": True},
}
FIGHTER_KIT = {
    "base_stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
    "base_inventory": {"weapons": ["longsword", "shield"], "armor": "chain mail"},
}


@pytest.mark.asyncio
async def test_unsupported_class_with_another_presets_kit_is_rejected(api_client) -> None:
    """EDG-174: 'Bard' with the Wizard kit (the playtest case) is refused, and nothing is saved."""
    await api_client.post("/api/seed")
    before = (await api_client.get("/api/characters")).json()
    response = await api_client.post(
        "/api/characters",
        json={"name": "Lute", "character_class": "Bard", "level": 1, "hp_max": 8, **WIZARD_KIT},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "validation_failed"
    assert "Class is not supported" in detail["message"]
    assert "Wizard" in detail["message"] and "Bard" not in detail["message"]
    assert (await api_client.get("/api/characters")).json() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"character_class": "Fighter", **WIZARD_KIT},  # supported class, another preset's kit
        {"character_class": "Fighter", "base_stats": {}, "base_inventory": {}},  # empty kit
        {"character_class": "Fighter", **FIGHTER_KIT, "base_stats": {**FIGHTER_KIT["base_stats"], "STR": 18}},
        {"character_class": "Fighter", **FIGHTER_KIT, "base_inventory": {"weapons": ["longsword"]}},
    ],
)
async def test_supported_class_with_incoherent_kit_is_rejected(api_client, payload) -> None:
    await api_client.post("/api/seed")
    response = await api_client.post(
        "/api/characters", json={"name": "Mismatch", "level": 1, "hp_max": 12, **payload}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "validation_failed"


@pytest.mark.asyncio
async def test_supported_class_saves_with_its_preset_kit_and_canonical_name(api_client) -> None:
    await api_client.post("/api/seed")
    response = await api_client.post(
        "/api/characters",
        json={"name": "Brannoc", "character_class": " fighter ", "level": 1, "hp_max": 12, **FIGHTER_KIT},
    )
    assert response.status_code == 201
    assert response.json()["character_class"] == "Fighter"
    assert response.json()["base_stats"] == FIGHTER_KIT["base_stats"]


@pytest.mark.asyncio
async def test_patch_rejects_class_or_kit_change_that_breaks_the_preset(api_client) -> None:
    await api_client.post("/api/seed")
    created = await api_client.post(
        "/api/characters",
        json={"name": "Sable", "character_class": "Wizard", "level": 1, "hp_max": 6, **WIZARD_KIT},
    )
    cid = created.json()["id"]
    url = f"/api/characters/{cid}"
    assert (await api_client.patch(url, json={"character_class": "Bard"})).status_code == 422
    assert (await api_client.patch(url, json={"character_class": "Fighter"})).status_code == 422
    assert (await api_client.patch(url, json={"base_stats": {}})).status_code == 422
    assert (await api_client.get(url)).json()["character_class"] == "Wizard"
    switched = await api_client.patch(url, json={"character_class": "Fighter", **FIGHTER_KIT})
    assert switched.status_code == 200
    assert switched.json()["character_class"] == "Fighter"


@pytest.mark.asyncio
async def test_legacy_unsupported_row_stays_readable_and_renamable(api_client, db_session) -> None:
    """A row saved before this rule (class 'Bard' with a Wizard kit) is not broken by it."""
    await api_client.post("/api/seed")
    from sqlalchemy import update

    from db.postgres.models import Character

    created = await api_client.post(
        "/api/characters",
        json={"name": "Old", "character_class": "Wizard", "level": 1, "hp_max": 6, **WIZARD_KIT},
    )
    cid = created.json()["id"]
    await db_session.execute(update(Character).where(Character.id == cid).values(character_class="Bard"))
    await db_session.commit()

    assert (await api_client.get(f"/api/characters/{cid}")).json()["character_class"] == "Bard"
    renamed = await api_client.patch(f"/api/characters/{cid}", json={"name": "Old Bard", "character_class": "Bard"})
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Old Bard"
    assert renamed.json()["character_class"] == "Bard"
