"""EDG-146: a captive after its fight is released, killed only by declaration, or answered by ruling.

Pure engine tests: no database, no model, no dice. The captive rows are combat-owned NPCs as
`load_combat_captives` returns them.
"""

import pytest

from agent.captives import captive_turn
from agent.combatants import INERT_NPC_DISPOSITIONS
from agent.graph import route_after_input_parser
from agent.graph_combat import (
    EncounterSetup,
    _inert_npc_names,
    captive_turn_for_state,
    combat_round_node,
)
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import _state


def _captive(name="Bandit", disposition="captive", combat_id=7, combatant_id=None):
    return {
        "name": name,
        "disposition": disposition,
        "combat_id": combat_id,
        "combatant_id": combatant_id or name,
        "combat_round": 4,
    }


def _turn(text, captives=None, named=None, attack_intent=False):
    captives = [_captive()] if captives is None else captives
    named = ["Bandit"] if named is None else named
    return captive_turn(text, named, captives, attack_intent=attack_intent)


def test_release_is_a_typed_outcome_that_never_starts_a_fight() -> None:
    turn = _turn("I release the bandit")
    assert turn.kind == "release"
    assert turn.outcome == {
        "action": "release",
        "npc_disposition": "released",
        "combat_id": 7,
        "combatant_id": "Bandit",
        "combat_round": 4,
        "name": "Bandit",
    }
    assert "not an enemy" in turn.text


def test_kill_needs_the_players_own_lethal_declaration_not_the_parser() -> None:
    assert _turn("I kill the bandit").kind == "kill"
    assert _turn("I execute him").kind == "kill"
    # An attack alone, however the parser labelled lethality, is only a ruling.
    assert _turn("I attack the bandit", attack_intent=True).kind == "ruling"
    assert _turn("I knock the bandit out, I don't want to kill him", attack_intent=True).kind == (
        "ruling"
    )


def test_attacking_a_captive_is_a_transparent_ruling_not_a_silent_no_op() -> None:
    turn = _turn("I stab the bandit", attack_intent=True)
    assert turn.kind == "ruling" and turn.outcome is None
    assert turn.text == (
        "Bandit is your captive; do you release, question, or kill them? No attack is made "
        "on a captive without a declared kill."
    )


def test_talking_to_a_captive_is_left_to_narration() -> None:
    assert _turn("I ask the bandit where the ledger is") is None


def test_a_captive_outcome_is_a_single_decision() -> None:
    turn = _turn("I release him and then kill him", named=["Bandit"], attack_intent=True)
    assert turn.kind == "ruling" and turn.outcome is None


def test_dead_and_released_names_never_start_a_fight() -> None:
    dead = _turn("I attack the bandit", captives=[_captive(disposition="dead")], attack_intent=True)
    released = _turn(
        "I attack the bandit", captives=[_captive(disposition="released")], attack_intent=True
    )
    assert dead.kind == "ruling" and "already dead" in dead.text
    assert released.kind == "ruling" and "no longer your captive" in released.text
    assert dead.outcome is None and released.outcome is None


def test_two_similar_captives_are_matched_exactly_and_ambiguity_is_asked() -> None:
    captives = [
        _captive("Raider", combat_id=7),
        _captive("Raider Captain", combat_id=7, combatant_id="Raider Captain"),
    ]
    released = _turn("I release the raider", captives=captives, named=["Raider"])
    assert released.kind == "release" and released.outcome["name"] == "Raider"
    assert released.outcome["combat_id"] == 7

    twins = [_captive("Raider", combat_id=7), _captive("Raider", combat_id=9)]
    asked = _turn("I release the raider", captives=twins, named=["Raider"])
    assert asked.kind == "ruling" and asked.outcome is None
    assert "Which one" in asked.text


def test_a_name_that_is_not_a_captive_is_not_intercepted() -> None:
    assert _turn("I release the wolf", captives=[_captive()], named=["Wolf"]) is None


def test_released_captive_stays_inert_and_is_never_hostile() -> None:
    assert "released" in INERT_NPC_DISPOSITIONS
    state = {"npcs": [{"name": "Bandit", "disposition": "released"}]}
    assert _inert_npc_names(state) == ["Bandit"]


def test_captive_turn_for_state_ignores_active_fights_and_manual_npcs() -> None:
    parsed = ParsedInput(intent="normal", target="Bandit")
    fight = {
        "player_input": "I release the bandit",
        "parsed_input": parsed,
        "combat_captives": [_captive()],
        "combat_state": {"combatants": [{"name": "Wolf"}], "ended": False},
        "npcs": [],
    }
    assert captive_turn_for_state(fight) is None
    # Manual NPCs have no combat owner, so they never reach the captive path.
    manual = {
        "player_input": "I release the bandit",
        "parsed_input": parsed,
        "npcs": [{"name": "Bandit", "disposition": "captive"}],
    }
    assert captive_turn_for_state(manual) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("I release the bandit", "combat"), ("I look around the cave", "normal")],
)
def test_route_sends_captive_actions_through_the_engine(text, expected) -> None:
    parsed = ParsedInput(intent="normal", target="Bandit")
    state = {
        "player_input": text,
        "parsed_input": parsed,
        "combat_captives": [_captive()],
    }
    assert route_after_input_parser(state) == expected


@pytest.mark.asyncio
async def test_attack_on_a_captive_no_longer_asks_who_you_are_fighting(
    fake_llm_schema, fake_retrieval
) -> None:
    """Reproduces the EDG-146 gap: an attack on a captive used to start no combat at all."""
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="Bandit"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(None, "I attack the bandit")
    state["npcs"] = [{"name": "Bandit", "disposition": "captive"}]
    state["combat_captives"] = [_captive()]
    state["parsed_input"] = fake_llm_schema.responses[ParsedInput]
    out = await combat_round_node(state)
    assert "do you release, question, or kill them" in out["narration"]
    assert "combat_state" not in out and "captive_outcome" not in out
    assert not any(schema is EncounterSetup for schema, _ in fake_llm_schema.calls)


@pytest.mark.asyncio
async def test_release_turn_returns_engine_outcome_without_dice_or_combat(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="normal", target="Bandit")
    state = _state(None, "I release the bandit")
    state["npcs"] = [{"name": "Bandit", "disposition": "captive"}]
    state["combat_captives"] = [_captive()]
    state["parsed_input"] = fake_llm_schema.responses[ParsedInput]
    out = await combat_round_node(state)
    assert out["captive_outcome"]["action"] == "release"
    assert out["narration"].startswith("You release Bandit.")
    assert "combat_state" not in out
    assert out["adjudication_result"].dice_log == []
