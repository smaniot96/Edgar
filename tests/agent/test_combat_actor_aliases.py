"""Public actor names bind exact source identities without global fuzzy matching."""

from copy import deepcopy

import pytest

from agent.combat_identity import canonical_actor_name, scene_actor_aliases
from agent.combat_targeting import select_target
from agent.graph_combat import EncounterSetup, EnemyStat, _build_enemy_roster
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, ManifestError, StatReference, manifest_digest
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


def descriptor_state():
    """A public unnamed figure and its exact private actor stat block."""
    state = alias_state()
    manifest = AdventureManifest.model_validate(state["adventure_manifests"]["module"])
    public = "A hulking half-orc demands the sheep. The half-orc draws a sword."
    stats = "Captain Ash\nMedium humanoid (half-orc), neutral\nArmor Class 15\nHit Points 7"
    manifest.entities[0].stat_reference = StatReference.model_validate({
        "kind": "module", "creature_name": "Captain Ash", "passage_id": "captain_stats",
    })
    manifest.passages[0].text_sha256 = text_digest(public)
    manifest.passages[0].end = len(public)
    manifest.passages.append(type(manifest.passages[0])(
        passage_id="captain_stats", page=2, start=0, end=len(stats),
        text_sha256=text_digest(stats), scene_ids=["hall"], entity_ids=["captain"],
        audience="planner",
    ))
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state["adventure_manifests"]["module"] = manifest.model_dump()
    state["adventure_context"][0].update(text=public, manifest_sha256=manifest.reviewed_sha256)
    state["planner_context"] = [{
        **state["adventure_context"][0], "passage_id": "captain_stats",
        "audience": "planner", "text": stats,
    }]
    return state


@pytest.mark.asyncio
async def test_visible_stat_descriptor_resolves_own_exact_actor_and_keeps_title_private(fake_llm_schema, monkeypatch):
    from agent.combat_identity import public_actor_name

    state = descriptor_state()
    aliases = scene_actor_aliases(state)
    assert aliases == {"Captain Ash": ["half-orc", "the half-orc"]}
    assert canonical_actor_name("the half-orc", aliases) == "Captain Ash"
    assert public_actor_name("Captain Ash", aliases) == "half-orc"
    looked_up = []

    def resolve(name, **kwargs):
        looked_up.append(name)
        return {**verified_record(), "actor_name": "Captain Ash", "actor_id": "captain"}

    monkeypatch.setattr("agent.graph_combat.resolve_creature", resolve)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(enemies=[EnemyStat(name="half-orc", hp_max=7)])
    roster = await _build_enemy_roster(state, ["the half-orc"])
    assert looked_up == ["Captain Ash"]
    assert roster[0]["name"] == "half-orc"
    assert roster[0]["canonical_name"] == "Captain Ash"
    assert roster[0]["source_reference"]["actor_id"] == "captain"
    assert select_target(roster, ["the half-orc"]) == (roster[0], None)


@pytest.mark.parametrize("change", ["not_public", "unassociated", "wrong_stat", "tampered", "prose_only", "locked"])
def test_descriptor_needs_actor_specific_verified_public_and_stat_evidence(change):
    state = descriptor_state()
    manifest = AdventureManifest.model_validate(state["adventure_manifests"]["module"])
    if change == "tampered":
        state["planner_context"][0]["text"] += " changed"
    elif change == "prose_only":
        text = "Captain Ash\nMedium humanoid, neutral\nThe half-orc is nearby."
        state["planner_context"][0]["text"] = text
        manifest.passages[1].text_sha256 = text_digest(text)
    elif change == "not_public":
        text = "A hulking figure demands the sheep."
        state["adventure_context"][0]["text"] = text
        manifest.passages[0].text_sha256 = text_digest(text)
    elif change == "unassociated":
        manifest.passages[0].entity_ids = []
    elif change == "wrong_stat":
        manifest.passages[1].entity_ids = []
    else:
        manifest.passages[1].requires = {"revealed": "true"}
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state["adventure_manifests"]["module"] = manifest.model_dump()
    for chunk in state["adventure_context"] + state["planner_context"]:
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    if change == "wrong_stat":
        with pytest.raises(ManifestError):
            scene_actor_aliases(state)
    else:
        assert scene_actor_aliases(state) == {}


def test_shared_visible_subtype_is_ambiguous_and_does_not_choose_an_actor():
    from agent.combat_identity import public_actor_name

    aliases = {"Captain Ash": ["half-orc", "the half-orc"], "Lieutenant Elm": ["half-orc", "the half-orc"]}
    assert canonical_actor_name("half-orc", aliases) == "half-orc"
    assert public_actor_name("Captain Ash", aliases) is None
    assert canonical_actor_name("orc", aliases) == "orc"
