"""EDG-182 increment 1: a combat turn is a list of registered declared actions.

The parser proposes the list; the engine adopts a proposal only where its own detection of the
player's words agrees. Disagreement is one question, with no roll, no round and no turn spent.
"""

import random

import pytest

from agent.declarations import REGISTRY, agreement_question, normalise_kind
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from tests.agent.test_action_economy import _foe, _hero
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng


async def _turn(fake, combatants, message, *, parsed: ParsedInput, seed: int = 0) -> dict:
    """One combat round with the parser's output in state, as the graph hands it to the round."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    state["parsed_input"] = parsed
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)

# The brief's fixture: movement, an attack with two weapons and a contingency, as the parser returns it.
MOVE_TWO_WEAPONS_CONTINGENCY = {
    "intent": "combat",
    "actions": [
        {"kind": "Movement", "text": "I move 10 feet toward the goblin", "destination": "10 feet toward the goblin"},
        {"kind": "Attack", "text": "attack the goblin with my longsword", "target": "goblin", "weapon": "longsword"},
        {"kind": "off-hand attack", "text": "off-hand attack with my dagger", "weapon": "dagger"},
        {"kind": "if", "text": "if it drops, attack the other", "target": "the other goblin"},
    ],
}


def test_the_brief_fixture_parses_into_an_ordered_typed_list() -> None:
    parsed = ParsedInput.model_validate(MOVE_TWO_WEAPONS_CONTINGENCY)
    assert [a.kind for a in parsed.actions] == ["move", "attack", "off_hand_attack", "contingency"]
    assert parsed.actions[1].target == "goblin" and parsed.actions[1].weapon == "longsword"
    assert parsed.actions[0].destination == "10 feet toward the goblin"


def test_every_registered_kind_is_its_own_engine_name_and_unknowns_are_not_guessed() -> None:
    for kind, action_type in REGISTRY.items():
        assert action_type.kind == kind
        assert normalise_kind(kind) == kind
    assert normalise_kind("teleport") == "unknown"
    assert normalise_kind(None) == "unknown"
    assert normalise_kind("off hand") == "off_hand_attack"


def test_an_unexecutable_contingency_is_asked_with_alternatives() -> None:
    question = agreement_question([("contingency", "if it drops, attack the other")], set(), "Eda")
    assert question is not None and "contingency" in question and "next turn" in question
    assert question.count("No roll") == 1


def test_agreement_is_silent_when_detected_and_one_question_otherwise() -> None:
    assert agreement_question([("attack", None)], {"attack"}, "Eda") is None
    assert agreement_question([], {"attack"}, "Eda") is None
    question = agreement_question([("dodge", "I dodge")], {"attack"}, "Eda")
    assert question is not None and "Dodge" in question and "No roll is made" in question
    # Several missing kinds are still one question.
    question = agreement_question([("dodge", None), ("dash", None)], set(), "Eda")
    assert question is not None and question.count("No roll") == 1


@pytest.mark.asyncio
async def test_a_proposal_the_player_did_not_say_is_asked_not_adopted(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I attack the goblin",
        parsed=ParsedInput(intent="combat", actions=[{"kind": "dodge", "text": "dodge"}]),
    )
    assert "Dodge" in out["narration"] and "No roll is made" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["hp_current"] == 30
    assert not _hero(out).get("effects")


@pytest.mark.asyncio
async def test_an_unknown_action_asks_one_question_naming_the_options(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I teleport behind the goblin",
        parsed=ParsedInput(intent="combat", actions=[{"kind": "teleport", "text": "teleport behind"}]),
    )
    assert "could not tell" in out["narration"] and "Dodge" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1 and _foe(out, "Goblin")["hp_current"] == 30


@pytest.mark.asyncio
async def test_an_agreeing_proposal_runs_the_turn_as_before(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I attack the goblin",
        parsed=ParsedInput(
            intent="combat", check="attack", target="goblin", actions=[{"kind": "attack", "target": "goblin"}]
        ),
    )
    assert out["combat_state"]["round"] == 2
    assert [d for d in out["adjudication_result"].dice_log if d.get("purpose") == "attack check"]


@pytest.mark.asyncio
async def test_the_brief_fixture_is_held_for_a_question_before_any_die(
    fake_llm_schema, fake_retrieval
) -> None:
    # The contingency is registered but not yet executable (EDG-182 increment 1), so the turn is
    # asked about it rather than silently run without it. No roll, no round, no spend.
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1), _enemy("Other", hp=30, ac=1, init=1)],
        "I move 10 feet toward the goblin and attack it with my longsword, off-hand with my dagger, and if it drops attack the other",
        parsed=ParsedInput.model_validate(MOVE_TWO_WEAPONS_CONTINGENCY),
    )
    assert "contingency" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["hp_current"] == 30 and _foe(out, "Other")["hp_current"] == 30
