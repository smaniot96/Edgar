import pytest

from agent.class_features import holds_feature
from agent.spells import slot_totals
from apps.api.services.character_presets import kit_error, preset_for_class, presets_at_level


@pytest.mark.parametrize("level", range(1, 6))
def test_starting_presets_are_independent_coherent_progression_builds(level):
    for preset in presets_at_level(level):
        assert preset["level"] == level
        assert kit_error(preset["character_class"], preset["stats"], preset["inventory"], level) is None
        base = preset_for_class(preset["character_class"])
        assert preset["hp_max"] >= base["hp_max"]
        if level >= 4:
            assert sum(preset["stats"].values()) == sum(base["stats"].values()) + 2
        preset["inventory"].clear()
        assert preset_for_class(preset["character_class"], level)["inventory"]


def test_recommended_fighter_build_has_hp_asi_and_derived_resources():
    preset = preset_for_class("Fighter", 5)
    assert preset["hp_max"] == 44
    assert preset["stats"]["STR"] == 18
    assert "Extra Attack" in preset["engine_support"]
    assert "not automated" in preset["engine_support"]
    assert holds_feature({"class": "Fighter", "level": 5}, "action_surge")
    assert slot_totals({"class": "Wizard", "level": 5}) == {1: 4, 2: 3, 3: 2}
    assert kit_error("Fighter", preset_for_class("Fighter")["stats"], preset["inventory"], 5)


@pytest.mark.asyncio
async def test_higher_level_creation_assignment_sheet_and_reload_agree(api_client):
    await api_client.post("/api/seed")
    response = await api_client.get("/api/character-presets?level=4")
    assert response.status_code == 200
    assert len(response.json()) == 5
    preset = next(item for item in response.json() if item["character_class"] == "Wizard")
    created = await api_client.post("/api/characters", json={
        "name": "Fourth-level wizard", "character_class": "Wizard", "level": 4,
        "hp_max": preset["hp_max"], "base_stats": preset["stats"], "base_inventory": preset["inventory"],
    })
    assert created.status_code == 201
    char_id = created.json()["id"]
    assert (await api_client.get(f"/api/characters/{char_id}")).json()["base_stats"]["INT"] == 19
    campaign = await api_client.post("/api/campaigns", json={"title": "Level test", "system": "5e", "source_mode": "homebrew"})
    campaign_id = campaign.json()["id"]
    assigned = await api_client.post(f"/api/campaigns/{campaign_id}/characters", json={"character_id": char_id})
    assert assigned.status_code == 201
    history = (await api_client.get(f"/api/characters/{char_id}/assignments")).json()[0]
    assert history["hp_max"] == preset["hp_max"]
    assert history["stats"]["INT"] == 19
    assert history["spell_slots"]["1"] == {"total": 4, "remaining": 4}
    assert history["spell_slots"]["2"] == {"total": 3, "remaining": 3}


@pytest.mark.asyncio
async def test_starting_level_cannot_bypass_kit_validation(api_client):
    await api_client.post("/api/seed")
    preset = preset_for_class("Fighter")
    body = {"name": "Wrong build", "character_class": "Fighter", "level": 4, "hp_max": 36,
            "base_stats": preset["stats"], "base_inventory": preset["inventory"]}
    assert (await api_client.post("/api/characters", json=body)).status_code == 422
    assert (await api_client.post("/api/characters", json={**body, "level": 6})).status_code == 422
    assert (await api_client.get("/api/character-presets?level=6")).status_code == 422
