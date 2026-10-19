"""EDG-182 increment 2: completeness, confirmation before any die, and a persisted pending declaration.

Each turn's combat state is round-tripped through JSON between turns, as the database does on a
reload, so a pending declaration that survives here survives in storage.
"""

import json
import random

import pytest

from agent.combat_confirmation import PENDING_KEY, control_word, needs_confirmation
from agent.combat_declaration import Declaration
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from apps.api.services.turn_runner import combat_state_public
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng


async def _say(fake, state_combat, message, *, parsed: ParsedInput, seed: int = 0) -> dict:
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, message)
    if state_combat is not None:
        state["combat_state"] = json.loads(json.dumps(state_combat))  # the reload
    state["parsed_input"] = parsed
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)


def _start(combatants):
    return {"id": 7, "round": 1, "ended": False, "combatants": combatants}


def _hero(combat) -> dict:
    return next(c for c in combat["combatants"] if c.get("is_player"))


def _foe(combat, name: str) -> dict:
    return next(c for c in combat["combatants"] if c["name"] == name)


def _rolled(out: dict) -> list[dict]:
    return list(out["adjudication_result"].dice_log or [])


ATTACK_GOBLIN = ParsedInput(intent="combat", check="attack", target="goblin", actions=[{"kind": "attack", "target": "goblin", "text": "attack the goblin"}])
MOVE_AND_ATTACK = ParsedInput(
    intent="combat",
    check="attack",
    target="goblin",
    actions=[
        {"kind": "move", "text": "step 10 feet closer", "destination": "10 feet closer"},
        {"kind": "attack", "text": "attack it", "target": "goblin"},
    ],
)
COMBATANTS = lambda: [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)]  # noqa: E731


def test_control_words_are_exact_replies_only() -> None:
    assert control_word("yes") == "confirm" and control_word("Yes.") == "confirm"
    assert control_word("go ahead") == "confirm"
    assert control_word("no") == "cancel" and control_word("cancel") == "cancel"
    assert control_word("yes, attack the other goblin instead") is None
    assert control_word("no, I want to dodge") is None
    assert control_word("always confirm") == "always_on"
    assert control_word("stop confirming") == "always_off"


def test_only_multi_part_typed_turns_and_chosen_turns_need_confirmation() -> None:
    attack = Declaration.__new__(Declaration)  # only the fields the gate reads
    object.__setattr__(attack, "declared", [])
    object.__setattr__(attack, "moving", False)
    object.__setattr__(attack, "is_attack", True)
    object.__setattr__(attack, "is_spell", False)
    assert needs_confirmation(attack, always=False, typed=True) is False
    assert needs_confirmation(attack, always=True, typed=False) is True
    object.__setattr__(attack, "moving", True)
    assert needs_confirmation(attack, always=False, typed=True) is True
    assert needs_confirmation(attack, always=False, typed=False) is False  # legacy path unchanged


@pytest.mark.asyncio
async def test_a_single_typed_action_runs_without_a_confirmation(fake_llm_schema, fake_retrieval) -> None:
    out = await _say(fake_llm_schema, _start(COMBATANTS()), "I attack the goblin", parsed=ATTACK_GOBLIN)
    assert _rolled(out) and out["combat_state"]["round"] == 2
    assert PENDING_KEY not in _hero(out["combat_state"])


@pytest.mark.asyncio
async def test_a_multi_part_turn_is_repeated_back_before_any_die(fake_llm_schema, fake_retrieval) -> None:
    out = await _say(fake_llm_schema, _start(COMBATANTS()), "I close in on the goblin and attack it", parsed=MOVE_AND_ATTACK)
    assert "Confirm before any dice" in out["narration"]
    assert "Reply yes to commit" in out["narration"]
    assert _rolled(out) == []
    assert out["combat_state"]["round"] == 1
    assert _foe(out["combat_state"], "Goblin")["hp_current"] == 30
    assert _hero(out["combat_state"])[PENDING_KEY]["summary"] == out["narration"]
    public = combat_state_public(out["combat_state"])
    assert public["pending_declaration"] == {"summary": out["narration"]}


@pytest.mark.asyncio
async def test_yes_after_a_reload_runs_the_stored_declaration(fake_llm_schema, fake_retrieval) -> None:
    first = await _say(fake_llm_schema, _start(COMBATANTS()), "I close in on the goblin and attack it", parsed=MOVE_AND_ATTACK)
    # The reply is "yes"; the parser's reading of it is irrelevant to the stored declaration.
    out = await _say(fake_llm_schema, first["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert any(d.get("purpose") == "attack check" for d in _rolled(out))
    assert out["combat_state"]["round"] == 2
    assert PENDING_KEY not in _hero(out["combat_state"])


@pytest.mark.asyncio
async def test_a_reply_that_changes_the_turn_is_a_new_declaration_not_a_confirmation(
    fake_llm_schema, fake_retrieval
) -> None:
    roster = [*COMBATANTS(), _enemy("Wolf", hp=30, ac=1, init=2)]
    first = await _say(fake_llm_schema, _start(roster), "I close in on the goblin and attack it", parsed=MOVE_AND_ATTACK)
    changed = ParsedInput(intent="combat", check="attack", target="wolf", actions=[
        {"kind": "move", "text": "close in on the wolf", "destination": "melee"},
        {"kind": "attack", "text": "attack it", "target": "wolf"},
    ])
    reply = "yes, close in on the wolf and attack it"
    out = await _say(fake_llm_schema, first["combat_state"], reply, parsed=changed)
    assert _rolled(out) == [], "the stored declaration must not run when the reply changes it"
    assert out["combat_state"]["round"] == 1
    assert "Confirm before any dice" in out["narration"]
    assert _hero(out["combat_state"])[PENDING_KEY]["player_input"] == reply
    assert _foe(out["combat_state"], "Goblin")["hp_current"] == 30 and _foe(out["combat_state"], "Wolf")["hp_current"] == 30


@pytest.mark.asyncio
async def test_no_cancels_the_pending_declaration_without_a_roll(fake_llm_schema, fake_retrieval) -> None:
    first = await _say(fake_llm_schema, _start(COMBATANTS()), "I close in on the goblin and attack it", parsed=MOVE_AND_ATTACK)
    out = await _say(fake_llm_schema, first["combat_state"], "no", parsed=ParsedInput(intent="combat"))
    assert "Cancelled" in out["narration"] and _rolled(out) == []
    assert out["combat_state"]["round"] == 1
    assert PENDING_KEY not in _hero(out["combat_state"])


@pytest.mark.asyncio
async def test_always_confirm_asks_about_a_simple_turn_too(fake_llm_schema, fake_retrieval) -> None:
    on = await _say(fake_llm_schema, _start(COMBATANTS()), "always confirm", parsed=ParsedInput(intent="combat"))
    assert "Always confirm is on" in on["narration"] and _rolled(on) == []
    asked = await _say(fake_llm_schema, on["combat_state"], "I attack the goblin", parsed=ATTACK_GOBLIN)
    assert "Confirm before any dice" in asked["narration"] and _rolled(asked) == []
    done = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert _rolled(done) and done["combat_state"]["round"] == 2


@pytest.mark.asyncio
async def test_a_proposed_attack_with_two_foes_and_no_target_is_one_question(
    fake_llm_schema, fake_retrieval
) -> None:
    combatants = [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1), _enemy("Wolf", hp=30, ac=1, init=2)]
    parsed = ParsedInput(intent="combat", check="attack", actions=[{"kind": "attack", "text": "attack"}])
    out = await _say(fake_llm_schema, _start(combatants), "I attack", parsed=parsed)
    assert out["narration"].count("No roll is made") == 1
    assert "Goblin" in out["narration"] and "Wolf" in out["narration"]
    assert _rolled(out) == [] and out["combat_state"]["round"] == 1
    assert PENDING_KEY not in _hero(out["combat_state"])
