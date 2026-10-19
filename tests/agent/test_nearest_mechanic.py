"""EDG-182 nearest supported mechanic: an action the engine does not read is not dropped or run silently.

"grab" and "seize" are the grapple (PHB p.196); "push" and "shove" are the shove that knocks a foe prone
(PHB p.196). The turn is repeated back with the nearest action named, and it runs only on "yes". A word
with no supported neighbour (cover, a teleport) is asked with the supported alternatives instead.
"""

import pytest

from agent.declarations import nearest_maneuver, object_is_foe
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import _enemy, _player
from tests.agent.test_declaration_confirmation import _say


def test_the_nearest_maneuver_rewrites_only_what_the_engine_reads() -> None:
    assert nearest_maneuver("I grab the goblin") == ("grab", "I grapple the goblin", "a grapple (PHB p.196)")
    assert nearest_maneuver("I push the goblin")[1] == "I knock the goblin prone"
    assert nearest_maneuver("I take cover behind the crate") is None
    assert nearest_maneuver("I teleport behind the goblin") is None


def test_a_push_away_is_not_mapped_to_a_knock_prone() -> None:
    # A push away from the hero is its own unsupported shove, ruled in the contract; it is not a prone.
    assert nearest_maneuver("I shove the raider away from me") is None


def test_the_word_must_take_a_foe_as_its_object() -> None:
    assert object_is_foe("grab", "I grab the goblin", ["Goblin"])
    assert object_is_foe("grab", "I grab him", ["Goblin"])
    assert not object_is_foe("grab", "I grab my sword and attack the goblin", ["Goblin"])


@pytest.mark.asyncio
async def test_a_grab_is_repeated_back_as_a_grapple_and_runs_only_on_yes(fake_llm_schema, fake_retrieval) -> None:
    combat = {"id": 7, "round": 1, "ended": False, "combatants": [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)]}
    out = await _say(fake_llm_schema, combat, "I grab the goblin", parsed=ParsedInput(intent="combat"))
    assert out["narration"].startswith("Confirm before any dice")
    assert "nearest one: a grapple" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1
    done = await _say(fake_llm_schema, out["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    # The engine's grapple: an athletics contest against the foe (PHB p.196), rolled only after "yes".
    purposes = [d.get("purpose") for d in done["adjudication_result"].dice_log]
    assert "athletics contest" in purposes and "Goblin contest" in purposes, purposes


@pytest.mark.asyncio
async def test_an_unmapped_word_is_asked_with_the_alternatives(fake_llm_schema, fake_retrieval) -> None:
    combat = {"id": 7, "round": 1, "ended": False, "combatants": [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)]}
    # The parser proposes what it could not name (an unknown kind); the engine asks, never drops it.
    parsed = ParsedInput(intent="combat", actions=[{"kind": "teleport", "text": "teleport behind the goblin"}])
    out = await _say(fake_llm_schema, combat, "I teleport behind the goblin", parsed=parsed)
    assert "could not tell which action" in out["narration"] and "Dodge" in out["narration"]
    assert out["adjudication_result"].dice_log == []
