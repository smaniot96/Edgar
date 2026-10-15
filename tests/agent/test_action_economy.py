"""EDG-044: one action and one bonus action per player turn, Dodge, and the turn's recorded spending.

Pure classification and refusal first, then the combat round as the graph runs it. Dice are
engine-rolled; the tests assert what the engine recorded, never a particular roll.
"""

import json
import random

import pytest

from agent.action_economy import (
    ACTION,
    BONUS_ACTION,
    DODGE,
    SECOND_WIND,
    Declared,
    dodge_declared,
    economy_record,
    economy_text,
    has_attack_words,
    refusal_for,
)
from agent.combatants import attack_roll_mode, effects_of, has_effect
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.supported_rules import (
    SUPPORTED_MECHANICS,
    UNSUPPORTED_MECHANICS,
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
    contract_payload,
)
from apps.api.services.turn_runner import combat_state_public
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng

CLERIC = {
    "id": 2,
    "name": "Ila",
    "class": "Cleric",
    "level": 1,
    "hp_current": 10,
    "hp_max": 30,
    "stats": {"STR": 10, "DEX": 12, "CON": 14, "INT": 10, "WIS": 14, "CHA": 8},
    "inventory": {"weapons": ["mace"], "armor": "scale mail"},
}


async def _turn(fake, combatants, message, *, character=None, parsed=None, seed=0) -> dict:
    fake.responses[ParsedInput] = parsed or ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    if character is not None:
        state["character"] = character
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


def _foe(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _hero(out: dict) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))


# --- pure classification and refusal ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "declared"),
    [
        ("I dodge", True),
        ("I take the dodge action", True),
        ("I try to dodge", True),
        ("I attack and dodge", True),
        ("dodge", True),
        ("The goblin tries to dodge my blade", False),
        ("I dodge the arrow", False),
        ("I dodge out of the way", False),
        ("the goblin dodges", False),
        ("I attack the goblin", False),
    ],
)
def test_dodge_is_declared_only_in_the_players_own_clause(text, declared) -> None:
    assert dodge_declared(text) is declared


@pytest.mark.parametrize(
    ("text", "attack"),
    [
        ("I attack the goblin", True),
        ("I swing at the goblin and use second wind", True),
        ("I hit the goblin", True),
        ("I dodge the attack", False),
        ("I use second wind to recover my hit points", False),
        ("I use second wind", False),
    ],
)
def test_attack_words_come_from_the_players_words(text, attack) -> None:
    assert has_attack_words(text) is attack


def test_two_actions_or_two_bonus_actions_in_one_turn_are_refused() -> None:
    refused = refusal_for([DODGE, Declared("attack", ACTION, "attack")], "Eda")
    assert refused is not None and "one action per turn" in refused
    assert "Dodge and attack" in refused
    refused = refusal_for([SECOND_WIND, Declared("spell", BONUS_ACTION, "Healing Word")], "Ila")
    assert refused is not None and "one bonus action per turn" in refused
    # An action and a bonus action together are the rule, not a refusal.
    assert refusal_for([SECOND_WIND, Declared("attack", ACTION, "attack")], "Eda") is None
    assert refusal_for([], "Eda") is None


def test_economy_record_names_only_what_was_spent() -> None:
    record = economy_record(3, [SECOND_WIND, Declared("attack", ACTION, "attack")], dodging=False)
    assert record == {
        "round": 3,
        "action": "attack",
        "bonus_action": "Second Wind",
        "dodging": False,
    }
    assert economy_record(1, [], dodging=True) == {
        "round": 1,
        "action": None,
        "bonus_action": None,
        "dodging": True,
    }


def test_narrator_context_states_the_recorded_turn_only() -> None:
    hero = {
        "name": "player",
        "is_player": True,
        "action_economy": economy_record(2, [DODGE], dodging=True),
    }
    text = economy_text([hero, _enemy("Goblin", hp=5)])
    assert "action used: Dodge" in text and "bonus action used: none" in text
    assert "disadvantage" in text
    # Legacy hero rows (no record) and rows without a hero add no line at all.
    assert economy_text([{"name": "player", "is_player": True}]) is None
    assert economy_text([_enemy("Goblin", hp=5)]) is None


# --- contract and classifier ------------------------------------------------------------------


def test_action_economy_moves_from_unsupported_to_supported() -> None:
    supported = {m.id for m in SUPPORTED_MECHANICS}
    unsupported = {m.id: m for m in UNSUPPORTED_MECHANICS}
    assert "action_economy" in supported
    assert "action_economy" not in unsupported
    remainder = unsupported["action_economy_remainder"]
    assert remainder.owner == "EDG-044"
    assert "Dash" in remainder.boundary and "Second Wind" in remainder.boundary
    payload = contract_payload()
    assert "Dodge" in repr(payload["engine_executed"])
    assert "action_economy" not in {m["id"] for m in payload["not_supported"]}


@pytest.mark.parametrize(
    ("text", "effects", "kinds"),
    [
        ("I take the dodge action", (), []),
        ("I take the dodge action", ("dodge",), []),
        ("I take the dash action", (), ["action_economy"]),
        ("I use my bonus action to hide", (), ["action_economy"]),
        ("I use Second Wind as a bonus action", ("bonus_action",), []),
        ("I use my bonus action to hide", ("bonus_action",), ["action_economy"]),
        ("The goblin tries to dodge my blade", (), []),
        ("I take the dash action", ("dodge",), ["action_economy"]),
    ],
)
def test_executed_economy_is_not_unsupported(text, effects, kinds) -> None:
    got = classify_unsupported_actions(text, applied=EngineApplied(effects=effects))
    assert [a.kind for a in got] == kinds


def test_executed_dodge_and_bonus_narration_survives_the_boundary() -> None:
    actions = classify_unsupported_actions(
        "I dodge and take the dash action", applied=EngineApplied(effects=("dodge",))
    )
    narration = "You dodge the blow. You dodge aside again. The goblin lunges and misses."
    kept = apply_narration_boundary(narration, actions, applied=EngineApplied(effects=("dodge",)))
    assert "You dodge the blow. You dodge aside again." in kept
    # An unexecuted claim of the same kind is still struck.
    stripped = apply_narration_boundary(
        "The hero takes the Dodge action. You use a bonus action to hide.",
        classify_unsupported_actions("I use my bonus action to hide"),
    )
    assert "You use a bonus action to hide" not in stripped
    assert "Narrative ruling" in stripped


# --- the combat round ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attack_and_second_wind_spend_the_action_and_the_bonus_action(
    fake_llm_schema, fake_retrieval
) -> None:
    fighter = {**_state([], "")["character"], "hp_current": 6}
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I attack the goblin and use second wind",
        character=fighter,
    )
    assert _entries(out, "second wind"), "Second Wind was not spent beside the attack"
    assert _entries(out, "attack check"), "the attack was not rolled beside Second Wind"
    assert out["combat_state"]["round"] == 2
    assert out["combat_state"]["economy"] == {
        "round": 1,
        "action": "attack",
        "bonus_action": "Second Wind",
        "dodging": False,
    }
    assert out["combat_state"]["second_wind_uses_remaining"] == 0


@pytest.mark.asyncio
async def test_second_action_is_refused_before_any_roll(fake_llm_schema, fake_retrieval) -> None:
    combatants = [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)]
    out = await _turn(fake_llm_schema, combatants, "I attack the goblin and dodge")
    assert "only one action per turn" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    # No roll, no round, no effect, no spending.
    assert out["combat_state"]["round"] == 1
    assert _foe(out, "Goblin")["hp_current"] == 30
    assert not has_effect(_hero(out), "dodging")
    assert "economy" not in out["combat_state"]


@pytest.mark.asyncio
async def test_dodge_gives_disadvantage_to_foes_until_the_heros_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    # The goblin acts after the hero this round: it attacks at disadvantage, once.
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I take the dodge action",
    )
    assert _entries(out, "Goblin attack")[0]["roll_mode"] == "disadvantage"
    hero = _hero(out)
    assert hero["effects"] == [{"kind": "dodging", "source": "dodge", "applied_round": 1}]
    assert out["combat_state"]["economy"] == {
        "round": 1,
        "action": "Dodge",
        "bonus_action": None,
        "dodging": True,
    }
    # Round 2: the goblin acts after the hero, whose next turn has begun, so the Dodge has ended.
    second = {**out["combat_state"], "round": 2}
    out2 = await _turn(fake_llm_schema, second["combatants"], "I attack the goblin")
    assert _entries(out2, "Goblin attack") and "roll_mode" not in _entries(out2, "Goblin attack")[0]
    assert _hero(out2)["effects"] == []
    assert out2["combat_state"]["economy"]["dodging"] is False


@pytest.mark.asyncio
async def test_dodge_still_applies_to_a_foe_that_acts_before_the_hero_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=10, ac=13), _enemy("Goblin", hp=30, ac=1, init=20)],
        "I take the dodge action",
    )
    # The goblin acts before the hero this round, so the Dodge was not yet in force for it.
    assert "roll_mode" not in _entries(out, "Goblin attack")[0]
    out2 = await _turn(
        fake_llm_schema,
        out["combat_state"]["combatants"],
        "I attack the goblin",
    )
    # Round 2: the goblin acts before the hero's new turn, so the Dodge is still in force for it.
    assert _entries(out2, "Goblin attack")[0]["roll_mode"] == "disadvantage"
    assert out2["combat_state"]["economy"]["action"] == "attack"


@pytest.mark.asyncio
async def test_two_similarly_named_foes_both_face_the_dodge(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [
            _player(init=20, ac=13),
            _enemy("Goblin", hp=30, ac=1, init=1),
            _enemy("Goblin 2", hp=30, ac=1, init=2),
        ],
        "I take the dodge action",
    )
    modes = {
        d["purpose"]: d.get("roll_mode")
        for d in out["adjudication_result"].dice_log
        if "attack" in d["purpose"]
    }
    assert modes == {"Goblin attack": "disadvantage", "Goblin 2 attack": "disadvantage"}


@pytest.mark.asyncio
async def test_healing_word_is_a_bonus_action_beside_an_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I attack the goblin and cast healing word",
        character=CLERIC,
    )
    heal = _entries(out, "Healing Word healing")
    assert len(heal) == 1
    assert out["combat_state"]["economy"]["action"] == "attack"
    assert out["combat_state"]["economy"]["bonus_action"] == "Healing Word"
    cu = out["adjudication_result"].character_update
    assert cu.spell_slots_expended == {"1": 1}
    # The engine's healing is the only heal; the goblin's engine-rolled damage is the only loss.
    lost = sum(max(1, d["total"]) for d in _entries(out, "Goblin damage"))
    assert _hero(out)["hp_current"] == CLERIC["hp_current"] + heal[0]["total"] - lost


@pytest.mark.asyncio
async def test_healing_word_alone_leaves_the_action_free(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I cast healing word on myself",
        character=CLERIC,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
    )
    assert _entries(out, "Healing Word healing") and not _entries(out, "player attack")
    assert out["combat_state"]["economy"] == {
        "round": 1,
        "action": None,
        "bonus_action": "Healing Word",
        "dodging": False,
    }
    assert out["combat_state"]["round"] == 2


@pytest.mark.asyncio
async def test_second_wind_alone_rolls_only_its_own_dice(fake_llm_schema, fake_retrieval) -> None:
    fighter = {**_state([], "")["character"], "hp_current": 6}
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I use second wind",
        character=fighter,
        parsed=ParsedInput(intent="combat", check="athletics"),
    )
    purposes = [d["purpose"] for d in out["adjudication_result"].dice_log]
    assert "second wind" in purposes
    # The parsed athletics check is not rolled: the bonus action is the whole of the hero's turn.
    assert set(purposes) <= {"second wind", "Goblin attack", "Goblin damage"}
    assert out["combat_state"]["economy"]["bonus_action"] == "Second Wind"
    assert out["combat_state"]["economy"]["action"] is None


@pytest.mark.asyncio
async def test_economy_and_dodge_survive_a_json_reload(fake_llm_schema, fake_retrieval) -> None:
    out = await _turn(
        fake_llm_schema,
        [_player(init=20, ac=13), _enemy("Goblin", hp=30, ac=1, init=1)],
        "I take the dodge action",
    )
    stored = json.loads(json.dumps(out["combat_state"]["combatants"]))
    hero = next(c for c in stored if c.get("is_player"))
    goblin = next(c for c in stored if c["name"] == "Goblin")
    assert attack_roll_mode(goblin, hero) == "disadvantage"
    assert effects_of(hero) == [{"kind": "dodging", "source": "dodge", "applied_round": 1}]
    payload = combat_state_public({**out["combat_state"], "combatants": stored})
    assert payload["economy"]["dodging"] is True and payload["economy"]["action"] == "Dodge"


def test_legacy_rows_read_without_economy_or_effects() -> None:
    legacy = {"name": "player", "is_player": True, "hp_current": 4, "alive": True}
    goblin = {"name": "Goblin", "hp_current": 5, "alive": True}
    assert effects_of(legacy) == [] and not has_effect(legacy, "dodging")
    assert attack_roll_mode(goblin, legacy) == "normal"
    assert economy_text([legacy, goblin]) is None
    payload = combat_state_public({"round": 1, "ended": False, "combatants": [legacy, goblin]})
    assert payload["economy"] is None
