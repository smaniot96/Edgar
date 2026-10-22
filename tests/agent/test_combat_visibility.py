"""Public descriptions survive hidden canonical names without inventing combatants."""

from copy import deepcopy

import pytest

from agent.combat_visibility import (
    descriptive_actor_mentions,
    refresh_visible_participants,
    visible_descriptive_participants,
)
from agent.graph_combat import combat_round_node
from agent.models.parsed_input import ParsedInput
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from tests.agent.test_combat import _enemy, _player, _state
from tests.agent.test_declaration_confirmation import _hero, _start

PUBLIC = (
    "Howling, angry shouts, and an occasional scream draw nearer. "
    "A hulking half-orc pushes through the crowd with one hand on a greatsword. "
    "Three large wolves wear iron collars; behind him, a huge cloaked figure's heavy "
    "footfalls rise above the commotion. The half-orc fixes his small eyes on the sheep "
    "and says, “That sheep is Master Noke’s... he desires to have it back.”"
)
ACTORS = [
    {"kind": "actor", "entity_id": "wolves", "title": "Guz's Collared Wolves"},
    {"kind": "actor", "entity_id": "bear", "title": "The Cloaked Bear Guard"},
    {"kind": "actor", "entity_id": "guz", "title": "Guz"},
]


def visibility_state():
    manifest = AdventureManifest.model_validate({
        "source": "module.pdf", "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64, "rules_edition": "2014",
        "opening_scene_id": "street", "scenes": [{"scene_id": "street", "title": "Street"}],
        "entities": ACTORS, "reviewed_by": "fixture",
        "passages": [{"passage_id": "public", "page": 1, "start": 0, "end": len(PUBLIC),
                      "text_sha256": text_digest(PUBLIC), "scene_ids": ["street"], "audience": "player"}],
    })
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return {
        "manifest_status": "reviewed", "current_scene_id": "street",
        "adventure_manifests": {"module": manifest.model_dump()}, "discovery_entities": ACTORS,
        "adventure_context": [{"collection": "module", "passage_id": "public", "text": PUBLIC,
                               "audience": "player", "scene_ids": ["street"],
                               "source_sha256": "a" * 64, "extraction_sha256": "b" * 64,
                               "manifest_sha256": manifest.reviewed_sha256}],
    }


def test_exact_public_descriptions_do_not_expose_hidden_identity():
    state = visibility_state()
    assert descriptive_actor_mentions(PUBLIC, ACTORS) == ["Three large wolves", "huge cloaked figure"]
    rows = visible_descriptive_participants(state)
    assert [row["name"] for row in rows] == ["Three large wolves", "huge cloaked figure"]
    assert all(row["id"].startswith("visible_") and row["participation"] == "visible_nonparticipant" for row in rows)
    assert "bear" not in str(rows).lower() and "guard" not in str(rows).lower()


@pytest.mark.parametrize("text", [
    'He says, "Three large wolves wear iron collars."',
    "If three large wolves arrive, run.", "There are no three large wolves here.",
    "Rumours tell of three large wolves.", "A cloaked doorway stands behind him.",
    "Three large wolves are not present.", "Three large wolves aren't here.",
    "Three large wolves are currently not visible.", "Three large wolves are absent.",
    "Three large wolves no longer stand behind him.",
    "A huge cloaked figure is not present.",
])
def test_quoted_uncertain_negative_and_unrelated_descriptions_are_not_visible(text):
    assert descriptive_actor_mentions(text, ACTORS) == []


def test_negated_object_does_not_hide_an_affirmative_actor():
    assert descriptive_actor_mentions("Three large wolves wear iron collars, not chains.", ACTORS) == ["Three large wolves"]


@pytest.mark.parametrize("change", ["wrong_scene", "planner", "unreviewed", "tampered"])
def test_descriptions_require_current_verified_public_source(change):
    state = visibility_state()
    if change == "wrong_scene":
        state["current_scene_id"] = "future"
    elif change == "planner":
        state["adventure_context"][0]["audience"] = "planner"
    elif change == "unreviewed":
        state["manifest_status"] = "unreviewed"
    else:
        state["adventure_context"][0]["text"] += " A secret dragon arrives."
    assert visible_descriptive_participants(state) == []


def test_refresh_preserves_roster_and_is_idempotent():
    combat = _start([_player(), _enemy("Guz")])
    hero = _hero(combat)
    before = deepcopy(combat["combatants"])
    refresh_visible_participants(visibility_state(), combat, hero)
    refresh_visible_participants(visibility_state(), combat, hero)
    assert len(combat["participants"]) == 2
    assert combat["combatants"][1:] == before[1:]
    assert combat["round"] == 1


@pytest.mark.asyncio
async def test_existing_encounter_recovers_visibility_without_a_turn_or_new_initiative(fake_llm_schema, fake_retrieval, monkeypatch):
    async def retrieve(state):
        return visibility_state()
    monkeypatch.setattr("agent.graph_combat.world_retriever_node", retrieve)
    combat = _start([_player(), _enemy("Guz")])
    before = deepcopy(combat["combatants"])
    state = {**_state(combat["combatants"], "I do not attack Guz.", parsed_input=ParsedInput(intent="combat")),
             **visibility_state(), "combat_state": combat}
    out = await combat_round_node(state)
    assert [row["name"] for row in out["combat_state"]["participants"]] == ["Three large wolves", "huge cloaked figure"]
    assert out["combat_state"]["combatants"][1:] == before[1:]
    assert out["combat_state"]["round"] == 1
    assert out["adjudication_result"].dice_log == []
