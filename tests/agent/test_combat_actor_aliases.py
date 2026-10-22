"""Public actor names bind exact source identities without global fuzzy matching."""

from copy import deepcopy

import pytest

from agent.combat_identity import canonical_actor_name, scene_actor_aliases
from agent.combat_targeting import select_target
from agent.graph_combat import EncounterSetup, EnemyStat, _build_enemy_roster
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from tests.agent.test_creature_encounters import verified_record


def alias_state():
    text = "Ash waits beside the desk in the hall."
    manifest = AdventureManifest.model_validate({
        "source": "fixture", "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64, "rules_edition": "2014",
        "opening_scene_id": "hall", "scenes": [{"scene_id": "hall", "title": "Hall"}],
        "entities": [{"kind": "actor", "entity_id": "captain", "title": "Captain Ash"}],
        "passages": [{"passage_id": "public", "page": 1, "start": 0,
                      "end": len(text), "text_sha256": text_digest(text),
                      "scene_ids": ["hall"], "entity_ids": ["captain"], "audience": "player"}],
        "reviewed_by": "fixture",
    })
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return {
        "manifest_status": "reviewed", "current_scene_id": "hall",
        "adventure_manifests": {"module": manifest.model_dump()},
        "adventure_context": [{"collection": "module", "passage_id": "public",
                               "text": text, "audience": "player",
                               "source_sha256": "a" * 64, "extraction_sha256": "b" * 64,
                               "manifest_sha256": manifest.reviewed_sha256}],
    }


@pytest.mark.asyncio
async def test_earned_short_name_uses_exact_actor_record_for_setup_and_targeting(
    fake_llm_schema, monkeypatch,
):
    state = alias_state()
    record = {**verified_record(), "actor_name": "Captain Ash", "actor_id": "captain"}
    looked_up = []

    def resolve(name, **kwargs):
        looked_up.append(name)
        return record

    monkeypatch.setattr("agent.graph_combat.resolve_creature", resolve)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Ash", hp_max=8)]
    )
    roster = await _build_enemy_roster(state, ["Ash"])
    assert looked_up == ["Captain Ash"]
    assert roster[0]["name"] == "Ash"
    assert roster[0]["canonical_name"] == "Captain Ash"
    assert roster[0]["source_reference"]["actor_id"] == "captain"
    assert roster[0]["creature_id"] == record["creature_id"]
    assert roster[0]["target_aliases"] == ["ash"]
    assert select_target(roster, ["Ash"]) == (roster[0], None)
    assert roster[0]["hp_max"] == 7


@pytest.mark.parametrize("change", ["unreviewed", "private", "tampered", "unassociated", "locked"])
def test_private_or_unearned_short_name_is_not_an_alias(change):
    state = alias_state()
    if change == "unreviewed":
        state["manifest_status"] = "unreviewed"
    elif change == "private":
        state["adventure_context"][0]["audience"] = "planner"
    elif change == "tampered":
        state["adventure_context"][0]["text"] += " The captain's name is revealed."
    else:
        manifest = AdventureManifest.model_validate(state["adventure_manifests"]["module"])
        if change == "unassociated":
            manifest.passages[0].entity_ids = []
        else:
            manifest.passages[0].requires = {"revealed": "true"}
            # Keep an unconditional player opening for the publication contract.
            opening = manifest.passages[0].model_copy(deep=True)
            opening.passage_id = "opening"
            opening.entity_ids = []
            opening.requires = {}
            manifest.passages.append(opening)
        manifest.reviewed_sha256 = manifest_digest(manifest)
        state["adventure_manifests"]["module"] = manifest.model_dump()
        state["adventure_context"][0]["manifest_sha256"] = manifest.reviewed_sha256
    assert scene_actor_aliases(state) == {}


def test_ambiguous_earned_names_ask_for_identity_and_never_use_substrings():
    aliases = {"Captain Ash": ["ash"], "Lieutenant Ash": ["ash"]}
    assert canonical_actor_name("Ash", aliases) == "Ash"
    first = {"name": "Captain Ash", "target_aliases": ["ash"], "hp_current": 7}
    second = {**deepcopy(first), "name": "Lieutenant Ash"}
    target, question = select_target([first, second], ["Ash"])
    assert target is None and "Which Ash" in question
    target, question = select_target([first], ["Captain"])
    assert target is None and question
    target, question = select_target([{"name": "Hobgoblin", "hp_current": 7}], ["Goblin"])
    assert target is None and question


def test_full_public_name_takes_priority_over_another_actors_alias():
    from agent.combat_identity import unique_roster_target
    aliases = {"Captain Ash": ["ash"], "Ash": []}
    assert canonical_actor_name("Ash", aliases) == "Ash"
    captain = {"name": "Captain Ash", "target_aliases": ["ash"], "hp_current": 7}
    ash = {"name": "Ash", "hp_current": 7}
    assert select_target([captain, ash], ["Ash"]) == (ash, None)
    assert unique_roster_target([captain, ash], "Ash") is ash


@pytest.mark.asyncio
async def test_earned_alias_move_and_attack_survives_confirmation_reload(fake_llm_schema, fake_retrieval):
    from agent.combat_confirmation import PENDING_KEY
    from agent.models.parsed_input import ParsedInput
    from tests.agent.test_combat import _enemy, _player
    from tests.agent.test_declaration_confirmation import _hero, _rolled, _say, _start

    combat = _start([
        _player(init=20, ac=99),
        {**_enemy("Captain Ash", hp=100, ac=1, init=1), "target_aliases": ["ash"], "band": "ranged", "distance_ft": 30},
        {**_enemy("Guard", hp=100, ac=1, init=2), "band": "ranged", "distance_ft": 30},
    ])
    parsed = ParsedInput(intent="combat", check="attack", target="Ash", actions=[
        {"kind": "move", "destination": "melee with Ash"}, {"kind": "attack", "target": "Ash"},
    ])
    asked = await _say(fake_llm_schema, combat, "I move back into melee with Ash and attack Ash.", parsed=parsed)
    assert "Confirm before any dice" in asked["narration"]
    assert _rolled(asked) == []
    plan = _hero(asked["combat_state"])[PENDING_KEY]["execution_plan"]
    assert plan["target_id"] == "Captain Ash"
    assert plan["moves"] == [{"band": "melee", "target_ids": ["Captain Ash"]}]
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert any(roll["purpose"] == "attack check" for roll in _rolled(final))
    assert PENDING_KEY not in _hero(final["combat_state"])


@pytest.mark.asyncio
async def test_public_confirmation_keeps_hidden_title_private(fake_llm_schema, fake_retrieval, monkeypatch):
    import json

    from agent.combat_confirmation import PENDING_KEY
    from agent.models.parsed_input import ParsedInput
    from apps.api.services.turn_runner import combat_state_public
    from tests.agent.test_combat import _player
    from tests.agent.test_declaration_confirmation import _hero, _rolled, _say, _start

    state = alias_state()
    record = {**verified_record(), "actor_name": "Captain Ash", "actor_id": "captain"}
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *a, **k: record)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(enemies=[EnemyStat(name="Captain Ash", hp_max=8)])
    roster = await _build_enemy_roster(state, ["Ash"])
    combat = _start([_player(init=20, ac=99), {**roster[0], "init": 1, "band": "ranged", "distance_ft": 30}])
    parsed = ParsedInput(intent="combat", check="attack", target="Ash", actions=[
        {"kind": "move", "destination": "melee with Ash"}, {"kind": "attack", "target": "Ash"},
    ])
    asked = await _say(fake_llm_schema, combat, "I move back into melee with Ash and attack Ash.", parsed=parsed)
    assert "move then attack Ash" in asked["narration"]
    assert "Captain" not in asked["narration"]
    assert "Captain" not in json.dumps(combat_state_public(asked["combat_state"]))
    assert _hero(asked["combat_state"])[PENDING_KEY]["execution_plan"]["target_id"] == "Ash"
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert any(roll["purpose"] == "attack check" for roll in _rolled(final))
    assert "Captain" not in final["adjudication_result"].mechanical_summary
    assert final["combat_state"]["combatants"][1]["creature_id"] == record["creature_id"]


def test_public_full_actor_name_has_priority_over_anothers_suffix():
    from agent.combat_identity import public_actor_name

    state = alias_state()
    manifest = AdventureManifest.model_validate(state["adventure_manifests"]["module"])
    manifest.entities.append(type(manifest.entities[0])(kind="actor", entity_id="ash", title="Ash"))
    manifest.passages[0].entity_ids.append("ash")
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state["adventure_manifests"]["module"] = manifest.model_dump()
    state["adventure_context"][0]["manifest_sha256"] = manifest.reviewed_sha256
    aliases = scene_actor_aliases(state)
    assert canonical_actor_name("Ash", aliases) == "Ash"
    assert public_actor_name("Captain Ash", aliases) is None
    assert public_actor_name("Ash", aliases) == "Ash"
