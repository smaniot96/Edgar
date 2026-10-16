"""EDG-043 (third slice): save-based monster actions (a breath weapon) against the hero's saving throw.

An exact record's breath weapon is executed only when its text reads as the Monster Manual words it
(Young Red Dragon, Fire Breath, PDF p.99: "must make a DC 17 Dexterity saving throw, taking 56 (16d6)
fire damage on a failed save, or half as much damage on a successful one"). The hero's save is an
engine d20 plus the sheet's modifier (proficiency when the class is proficient, PHB p.46); a failed save
takes the damage, a success half of it rounded down, and a dodging hero has advantage on a Dexterity
save (PHB, PDF p.193). The fixtures are synthetic: their numbers and creature are written for the test.
Dice are scripted (`ScriptedDice`).
"""

import json

import pytest

from agent.combatants import add_effect, save_roll_mode
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.monster_stats import (
    AttackSave,
    MonsterStats,
    attack_plan,
    parse_monster_stats,
)
from tests.agent.test_concentration import _entries, _next_round, _summary
from tests.agent.test_monster_riders import WAIT, _exact, _state
from tests.agent.test_spells import FIGHTER, ScriptedDice
from tools.dice import use_rng

BREATH = (
    "Cinder Breath (Recharge 5–6). The drake exhales fire in a 15-foot cone. Each creature in that "
    "area must make a DC 13 Dexterity saving throw, taking 7 (2d6) fire damage on a failed save, "
    "or half as much damage on a successful one."
)
DRAKE_TEXT = f"""Ember Drake
Large dragon, unaligned
Armor Class 14 (natural armor)
Hit Points 45 (6d10 + 12)
Speed 30 ft., fly 60 ft.
STR DEX CON INT WIS CHA
15 (+2) 12 (+1) 14 (+2) 6 (-2) 10 (+0) 8 (-1)
Challenge 2 (450 XP)
Actions
Multiattack. The drake makes two attacks: one with its bite and one with its claw.
Bite. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 7 (1d8 + 3) piercing damage.
Claw. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.
{BREATH}
"""
NO_EFFECT_TEXT = DRAKE_TEXT.replace(
    "must make a DC 13 Dexterity saving throw, taking 7 (2d6) fire damage on a failed save, or half as "
    "much damage on a successful one.",
    "must succeed on a DC 13 Dexterity saving throw or take 7 (2d6) fire damage.",
)


def _stats(text: str) -> MonsterStats:
    return parse_monster_stats(text, name="Ember Drake")


# --- parsing: exact wording only --------------------------------------------------------------------


def test_exact_breath_weapon_is_read_with_its_save_and_recharge() -> None:
    stats = _stats(DRAKE_TEXT)
    breath = stats.attacks[-1]
    assert (breath.name, breath.recharge, breath.damage_dice, breath.damage_type) == (
        "Cinder Breath",
        5,
        "2d6",
        "fire",
    )
    assert breath.save == AttackSave("DEX", 13, half_on_success=True)
    assert not any("Cinder Breath" in d for d in stats.declared)
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats


def test_a_save_that_negates_the_damage_is_read_without_the_half_clause() -> None:
    breath = _stats(NO_EFFECT_TEXT).attacks[-1]
    assert breath.save == AttackSave("DEX", 13, half_on_success=False)


@pytest.mark.parametrize(
    "wording",
    [
        BREATH.replace("7 (2d6)", "9 (2d6)"),  # the stated average disagrees with the dice
        BREATH.replace("fire damage", "chaos damage"),  # not a damage type
        BREATH.replace("exhales fire", "spits fire, provided that it has no creature grappled"),
        BREATH.replace("successful one.", "successful one, and is knocked prone."),
        BREATH.replace("Each creature in that area", "One creature of the drake's choice"),
        BREATH.replace("(Recharge 5–6)", "(Recharges after a Short or Long Rest)"),
    ],
)
def test_other_wording_stays_declared_and_is_not_executed(wording: str) -> None:
    text = DRAKE_TEXT.replace(BREATH, wording)
    stats = _stats(text)
    assert [a.name for a in stats.attacks] == ["Bite", "Claw"]
    assert any(d.startswith("Cinder Breath") and "not executed" in d for d in stats.declared)


def test_a_save_action_without_recharge_is_used_only_when_multiattack_names_it() -> None:
    stats = _stats(DRAKE_TEXT.replace(" (Recharge 5–6)", ""))
    assert stats.attacks[-1].recharge is None and stats.attacks[-1].save is not None
    assert [a.name for a in attack_plan(stats)] == ["Bite", "Claw"]


# --- the plan -------------------------------------------------------------------------------------


def test_a_ready_breath_replaces_the_multiattack_until_it_is_spent() -> None:
    stats = _stats(DRAKE_TEXT)
    assert [a.name for a in attack_plan(stats)] == ["Cinder Breath"]
    assert [a.name for a in attack_plan(stats, spent=["Cinder Breath"])] == ["Bite", "Claw"]


# --- the hero's save in the round -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_save_takes_the_full_damage_then_the_breath_waits(
    fake_llm_schema, fake_retrieval
) -> None:
    """Dara's DEX save is +1 (DEX 12, a cleric has no DEX proficiency): 8 + 1 = 9 fails DC 13, so the
    2d6 (3 + 4) fire damage is taken in full, 12 - 7 = 5 HP. The breath is spent: next round its d6
    (3) does not recharge it, so the drake makes its bite (1) and claw (1), both natural 1s.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(DRAKE_TEXT, "Ember Drake")], WAIT)
    with use_rng(ScriptedDice([8, 3, 4])):
        first = await combat_round_node(state)
    assert first["combat_state"]["combatants"][0]["hp_current"] == 5
    assert (
        "Dara fails the DEX save against Ember Drake's Cinder Breath (rolled 9 vs DC 13) and takes "
        "7 fire damage." in _summary(first)
    )
    assert _entries(first, "Dara DEX save (Cinder Breath)")[0]["rolls"] == [8]
    assert _entries(first, "Ember Drake Cinder Breath damage")[0]["rolls"] == [3, 4]
    assert first["combat_state"]["combatants"][1]["recharge_spent"] == ["Cinder Breath"]

    follow = _next_round(first, "I wait and watch", WAIT, round_num=2)
    with use_rng(ScriptedDice([3, 1, 1])):
        second = await combat_round_node(follow)
    assert "Ember Drake's Cinder Breath does not recharge yet (rolled 3 on the d6)." in _summary(
        second
    )
    assert _entries(second, "Ember Drake Bite attack") and _entries(
        second, "Ember Drake Claw attack"
    )


@pytest.mark.asyncio
async def test_a_successful_save_takes_half_rounded_down(fake_llm_schema, fake_retrieval) -> None:
    """12 + 1 = 13 meets DC 13: the 7 fire damage (3 + 4) is halved to 3, so 12 - 3 = 9 HP."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(DRAKE_TEXT, "Ember Drake")], WAIT)
    with use_rng(ScriptedDice([12, 3, 4])):
        out = await combat_round_node(state)
    assert out["combat_state"]["combatants"][0]["hp_current"] == 9
    assert (
        "Dara succeeds on the DEX save against Ember Drake's Cinder Breath (rolled 13 vs DC 13) "
        "and takes 3 fire damage." in _summary(out)
    )


@pytest.mark.asyncio
async def test_a_successful_save_against_a_negating_action_takes_no_damage(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(NO_EFFECT_TEXT, "Ember Drake")], WAIT)
    with use_rng(ScriptedDice([12, 3, 4])):
        out = await combat_round_node(state)
    assert out["combat_state"]["combatants"][0]["hp_current"] == 12
    assert "and takes no damage." in _summary(out)


@pytest.mark.asyncio
async def test_the_sheets_save_proficiency_is_added(fake_llm_schema, fake_retrieval) -> None:
    """A fighter is proficient in Constitution saves (PHB p.46): CON 14 (+2) and proficiency (+2) make
    8 + 4 = 12, which fails DC 13; the same roll without proficiency would be 10."""
    text = DRAKE_TEXT.replace("Dexterity", "Constitution").replace("fire", "poison")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(text, "Ember Drake")], WAIT)
    state["character"] = {**FIGHTER, "level": 1}
    with use_rng(ScriptedDice([8, 3, 4])):
        out = await combat_round_node(state)
    assert (
        "fails the CON save against Ember Drake's Cinder Breath (rolled 12 vs DC 13)"
        in _summary(out)
    )


@pytest.mark.asyncio
async def test_dodge_gives_advantage_on_the_dexterity_save(fake_llm_schema, fake_retrieval) -> None:
    """Dodging, the hero rolls two d20s for the DEX save and keeps the higher: 3 and 14 keep 14, so
    14 + 1 = 15 meets DC 13 and half the damage is taken (PHB, PDF p.193)."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I take the dodge action", [_exact(DRAKE_TEXT, "Ember Drake")], WAIT)
    with use_rng(ScriptedDice([3, 14, 3, 4])):
        out = await combat_round_node(state)
    save = _entries(out, "Dara DEX save (Cinder Breath)")[0]
    assert (save["rolls"], save["kept"], save["roll_mode"]) == ([3, 14], 14, "advantage")
    assert "succeeds on the DEX save" in _summary(out) and "at advantage" in _summary(out)
    assert out["combat_state"]["combatants"][0]["hp_current"] == 9


def test_dodge_advantage_applies_to_dexterity_saves_only() -> None:
    hero = {"name": "player"}
    assert save_roll_mode(hero, "DEX") == "normal"
    add_effect(hero, "dodging", source="dodge", round_applied=1)
    assert save_roll_mode(hero, "DEX") == "advantage"
    assert save_roll_mode(hero, "CON") == "normal"
