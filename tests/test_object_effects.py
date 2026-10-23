"""Stable reviewed object availability uses ledger identity, never display-name inference."""

from copy import deepcopy

import pytest

from agent.object_effects import object_state, validate_object_effect
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import StoryEffect, StoryRef, accept_story_effect, story_target_id


@pytest.fixture
def object_state_fixture():
    text = "The note says “The lens tracks reflections, not the sky.”"
    manifest = AdventureManifest(
        source="fixture",
        source_sha256="a" * 64,
        extraction_sha256="b" * 64,
        opening_scene_id="room",
        scenes=[{"scene_id": "room", "title": "Room"}],
        passages=[
            {
                "passage_id": "note_text",
                "page": 1,
                "start": 0,
                "end": len(text),
                "text_sha256": text_digest(text),
                "scene_ids": ["room"],
                "audience": "player",
            }
        ],
        objects=[
            {
                "canonical_id": "note",
                "title": "Lens note",
                "scene_ids": ["room"],
                "passage_ids": ["note_text"],
            },
            {
                "canonical_id": "cord",
                "title": "Spare cord",
                "scene_ids": ["room"],
                "passage_ids": ["note_text"],
                "initial_owner": "character",
                "consumable": True,
                "operations": ["tether"],
            },
            {
                "canonical_id": "brace",
                "title": "Brace",
                "scene_ids": ["room"],
                "passage_ids": ["note_text"],
                "portable": False,
                "operations": ["secure"],
            },
        ],
        reviewed_by="synthetic fixture",
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    state = {
        "adventure_manifests": {"fixture": manifest.model_dump()},
        "current_scene_id": "room",
        "character": {"id": 7, "inventory": {"items": ["Spare cord"]}},
        "story_state": {"facts": []},
    }

    def effect(kind, obj="note", **kw):
        return StoryEffect(
            kind=kind,
            target=StoryRef(
                collection="fixture",
                source_sha256=manifest.source_sha256,
                manifest_sha256=manifest.reviewed_sha256,
                kind="object",
                canonical_id=obj,
            ),
            **kw,
        )

    return state, effect


def seal(effect):
    return accept_story_effect(effect, turn_id="test-turn", source_refs=[], display_name="Fixture")


def test_pickup_once_transfer_reacquire_and_consumed_identity(object_state_fixture):
    state, effect = object_state_fixture
    acquired = validate_object_effect(effect("acquire"), state)
    assert acquired.destination == "character:7"
    assert acquired.quantity == 1
    assert validate_object_effect(effect("acquire"), state, [seal(acquired)]) is None
    moved = validate_object_effect(effect("transfer", destination="room"), state, [seal(acquired)])
    assert moved.status == "moved"
    assert validate_object_effect(effect("acquire"), state, [seal(acquired), seal(moved)])
    consumed = validate_object_effect(effect("consume", "cord"), state)
    state["story_state"]["facts"] = [seal(consumed).model_dump()]
    assert object_state(consumed.target, state)["consumed"] is True
    assert validate_object_effect(effect("acquire", "cord"), state) is None


def test_gear_environment_operations_require_owned_supported_objects(object_state_fixture):
    state, effect = object_state_fixture
    use = validate_object_effect(
        effect("use", "cord", operation="tether", destination="brace"), state
    )
    assert use.status == "used"
    assert validate_object_effect(effect("use", "cord", operation="gain_bonus"), state) is None
    assert validate_object_effect(effect("acquire", "brace"), state) is None
    assert validate_object_effect(effect("consume", "note"), state) is None
    assert validate_object_effect(effect("acquire", quantity=100), state) is None
    earned = seal(use)
    change = effect(
        "environment", "brace", operation="secure", knowledge_fact_ids=[earned.effect_id]
    )
    assert validate_object_effect(change, state) is None
    assert validate_object_effect(change, state, [earned]).status == "changed"
    other = deepcopy(state)
    other["character"]["inventory"] = {}
    assert validate_object_effect(effect("use", "cord", operation="tether"), other) is None


def test_shared_assignment_stable_identity_blocks_second_session_pickup(object_state_fixture):
    state, effect = object_state_fixture
    target = effect("acquire").target
    state["character"]["inventory"]["items"].append(
        {"name": "Renamed", "story_object_id": story_target_id(target)}
    )
    assert validate_object_effect(effect("acquire"), state) is None


def test_planner_custody_bindings_support_drop_and_reacquire_without_accepting_location_prose(object_state_fixture):
    from agent.nodes.story_effects import story_candidates

    state, effect = object_state_fixture
    candidate = next(c for c in story_candidates(state) if c["target"]["canonical_id"] == "note")
    acquired = seal(validate_object_effect(effect("acquire", destination=candidate["acquire_destination"]), state))
    assert validate_object_effect(effect("transfer", destination="table beside the hero"), state, [acquired]) is None
    dropped = seal(validate_object_effect(
        effect("transfer", destination=candidate["transfer_destination"]), state, [acquired],
    ))
    assert dropped.destination == "room"
    assert validate_object_effect(effect("acquire"), state, [acquired, dropped]).destination == "character:7"


@pytest.mark.parametrize("destination", [
    "Hazel", "Hazel Preparation Replay Peace L4", "character:7", None,
    "Hazel's pack", "Hazel’s backpack", "Hazel Preparation Replay Peace L4's inventory",
])
def test_pickup_normalizes_only_the_active_hero_binding(object_state_fixture, destination):
    state, effect = object_state_fixture
    state["character"]["name"] = "Hazel Preparation Replay Peace L4"
    acquired = validate_object_effect(effect("acquire", destination=destination), state)
    assert acquired.destination == "character:7"


@pytest.mark.parametrize("destination,npcs", [
    ("Mara", []), ("Hazel", [{"id": 7, "name": "Hazel the ferryman"}]),
    ("Mara's pack", []), ("Hazel's pack", [{"id": 8, "name": "Hazel the ferryman"}]),
    ("Hazel's pack and Mara's pack", []), ("Hazel's bag of holding", []),
])
def test_pickup_does_not_normalize_another_actor_or_ambiguous_alias(object_state_fixture, destination, npcs):
    state, effect = object_state_fixture
    state["character"]["name"] = "Hazel Preparation Replay Peace L4"
    state["npcs"] = npcs
    assert validate_object_effect(effect("acquire", destination=destination), state) is None
