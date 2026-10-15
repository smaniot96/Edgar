"""EDG-043 (second slice): exact monster riders and recharge attacks.

A poisoned rider on a hit is executed only when the attack text reads exactly as the Monster Manual
words it: the hero makes the stated Constitution save, a failure poisons it (disadvantage on its own
attack rolls), and the hero repeats the save at the end of its turn. A Recharge X-6 attack is used once
and then waits for a d6 roll at the start of the creature's turns. Any other wording stays declared.
Dice are scripted where the order matters (`ScriptedDice`).
"""

import json

import pytest

from agent.combatants import attack_roll_mode, effects_of, narrator_roster
from agent.conditions import rider_save
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    AttackRider,
    MonsterStats,
    attack_plan,
    parse_monster_stats,
    recharge_turn_start,
    spend_recharge,
    stats_of,
)
from tests.agent.test_concentration import _entries, _next_round, _summary
from tests.agent.test_spells import CLERIC, ScriptedDice, _combat_state, _foe, _hero
from tools.dice import use_rng

CLERIC_3 = {**CLERIC, "level": 3}
POISON_RIDER = "and the target must succeed on a DC 12 Constitution saving throw or be poisoned for 1 minute. The target can repeat the saving throw at the end of each of its turns, ending the effect on a success."
SPITTER_TEXT = f"""Spitter
Small humanoid (gnome), neutral good
Armor Class 15 (chain shirt)
Hit Points 16 (3d6 + 6)
Speed 20 ft.
STR DEX CON INT WIS CHA
15 (+2) 14 (+2) 14 (+2) 12 (+1) 10 (+0) 9 (-1)
Challenge 1/2 (100 XP)
Actions
Hand Crossbow. Ranged Weapon Attack: +4 to hit, range 30/120 ft., one creature. Hit: 4 (1d4 + 2) piercing damage, {POISON_RIDER}
"""
EXTRA_DAMAGE_TEXT = SPITTER_TEXT.replace(
    "Hit: 4 (1d4 + 2) piercing damage, and the target must succeed",
    "Hit: 4 (1d4 + 2) piercing damage plus 4 (1d8) poison damage. The target must succeed",
)
RECHARGE_TEXT = """Acid Spitter
Medium monstrosity, unaligned
Armor Class 13
Hit Points 22 (4d8 + 4)
Speed 30 ft.
STR DEX CON INT WIS CHA
12 (+1) 14 (+2) 12 (+1) 2 (-4) 10 (+0) 6 (-2)
Challenge 1 (200 XP)
Actions
Acid Spit (Recharge 5-6). Ranged Weapon Attack: +4 to hit, range 30/60 ft., one target. Hit: 7 (2d6) acid damage.
Claw. Melee Weapon Attack: +3 to hit, reach 5 ft., one target. Hit: 3 (1d4 + 1) slashing damage.
"""


def _exact(text: str, name: str, hp: int = 16) -> dict:
    stats = parse_monster_stats(text, name=name)
    return {**_foe(name, hp=hp, ac=stats.ac), "monster_stats": stats.to_dict()}


def _state(message: str, foes: list[dict], parsed: ParsedInput, hp: int = 12) -> dict:
    state = _combat_state(CLERIC_3, message, parsed=parsed)
    state["character"] = {**state["character"], "hp_current": hp}
    state["combat_state"]["combatants"] = [
        {**state["combat_state"]["combatants"][0], "hp_current": hp},
        *foes,
    ]
    return state


WAIT = ParsedInput(intent="other")


# --- parsing: exact wording only -------------------------------------------------------------------


def test_exact_poison_rider_is_read_and_nothing_is_declared() -> None:
    stats = parse_monster_stats(SPITTER_TEXT, name="Spitter")
    (attack,) = stats.attacks
    assert attack.rider == AttackRider("poisoned", 12, "CON", 10)
    assert not any("rider" in d for d in stats.declared)


def test_a_rider_with_extra_poison_damage_is_declared_not_executed() -> None:
    stats = parse_monster_stats(EXTRA_DAMAGE_TEXT, name="Spitter")
    assert stats.attacks[0].rider is None
    assert any("damage rider or extra effect not executed" in d for d in stats.declared)


def test_recharge_attack_is_parsed_and_the_claw_is_not_a_recharge() -> None:
    stats = parse_monster_stats(RECHARGE_TEXT, name="Acid Spitter")
    spit, claw = stats.attacks
    assert (spit.name, spit.recharge, spit.damage_dice, spit.damage_type) == (
        "Acid Spit",
        5,
        "2d6",
        "acid",
    )
    assert claw.recharge is None and claw.rider is None
    assert not any("recharge" in d for d in stats.declared)


def test_stored_rows_before_riders_and_recharge_read_with_neither_and_round_trip() -> None:
    legacy = {
        "name": "Spitter",
        "hp_max": 16,
        "ac": 15,
        "attacks": [
            {"name": "Hand Crossbow", "bonus": 4, "damage_dice": "1d4+2", "damage_type": "piercing"}
        ],
    }
    stats = stats_of({"monster_stats": legacy})
    assert (
        stats is not None and stats.attacks[0].rider is None and stats.attacks[0].recharge is None
    )
    parsed = parse_monster_stats(SPITTER_TEXT, name="Spitter")
    assert MonsterStats.from_dict(json.loads(json.dumps(parsed.to_dict()))) == parsed


# --- the poisoned rider in the round ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_poisoned_rider_on_a_hit_saves_and_the_hero_repeats_it(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 1: the Spitter hits (13 + 4 = 17 vs AC 14) for 3 + 2 = 5, and the hero's CON save fails
    (8 + 2 = 10 vs DC 12), poisoning it. Round 2: the hero's attack has disadvantage (2 and 9 kept at
    4 + 0 + 2 = 6 vs AC 15, a miss). The hero's own save at the end of its turn succeeds (16 + 2 = 18),
    and the Spitter's own attack misses (2 + 4 = 6 vs AC 14).
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(SPITTER_TEXT, "Spitter")], WAIT)
    with use_rng(ScriptedDice([13, 3, 8])):
        first = await combat_round_node(state)
    hero = first["combat_state"]["combatants"][0]
    assert effects_of(hero) == [
        {
            "kind": "poisoned",
            "source": "Spitter",
            "applied_round": 1,
            "expires_after_round": 10,
            "save": {"ability": "CON", "dc": 12},
        }
    ]
    assert (
        "Dara fails the CON save against Hand Crossbow (rolled 10 vs DC 12) and is poisoned."
        in _summary(first)
    )
    assert _entries(first, "Dara CON save (poisoned)")[0]["rolls"] == [8]

    follow = _next_round(
        first,
        "I attack the spitter",
        ParsedInput(intent="combat", check="attack", target="spitter"),
        round_num=2,
    )
    with use_rng(ScriptedDice([9, 4, 16, 2])):
        second = await combat_round_node(follow)
    hero_after_round_one = first["combat_state"]["combatants"][0]
    assert (
        attack_roll_mode(hero_after_round_one, first["combat_state"]["combatants"][1])
        == "disadvantage"
    )
    assert _entries(second, "attack check")[0]["roll_mode"] == "disadvantage"
    assert (
        "Dara repeats its CON save against poisoned (rolled 18 vs DC 12) and ends the effect."
        in _summary(second)
    )
    assert effects_of(second["combat_state"]["combatants"][0]) == []


def test_the_rider_save_is_engine_rolled_with_the_sheet_modifier() -> None:
    """The CON save is +2 (CON 14, no proficiency). A failed save poisons, a success records nothing."""
    player = {"name": "player"}
    rider = AttackRider("poisoned", 12, "CON", 10)
    with use_rng(ScriptedDice([9])):
        failed = rider_save(player, rider, character=CLERIC_3, source="Spitter", round_applied=3)
    assert (
        failed.failed and failed.total == 11 and effects_of(player)[0]["expires_after_round"] == 12
    )
    with use_rng(ScriptedDice([10])):
        held = rider_save(
            {"name": "player"}, rider, character=CLERIC_3, source="Spitter", round_applied=3
        )
    assert not held.failed and held.total == 12


def test_two_foes_with_the_same_record_each_keep_their_own_recharge() -> None:
    first = parse_monster_stats(RECHARGE_TEXT, name="Acid Spitter")
    a = {**_exact(RECHARGE_TEXT, "Acid Spitter")}
    b = {**_exact(RECHARGE_TEXT, "Acid Spitter 2")}
    spend_recharge(a, first.attacks[0])
    assert attack_plan(stats_of(a), spent=a["recharge_spent"])[0].name == "Claw"
    assert attack_plan(stats_of(b), spent=b.get("recharge_spent") or ())[0].name == "Acid Spit"


# --- recharge ---------------------------------------------------------------------------------------


def test_recharge_attack_waits_for_its_d6_roll() -> None:
    """Used once, it is left out of the plan. The d6 at the start of the turn restores it on 5 or 6."""
    foe = _exact(RECHARGE_TEXT, "Acid Spitter")
    stats = stats_of(foe)
    assert [a.name for a in attack_plan(stats)] == [
        "Acid Spit"
    ]  # a ready recharge attack goes first
    spend_recharge(foe, stats.attacks[0])
    assert [a.name for a in attack_plan(stats, spent=foe["recharge_spent"])] == ["Claw"]

    with use_rng(ScriptedDice([4])):
        (miss,) = recharge_turn_start(foe)
    assert miss[0] == "Acid Spit" and not miss[2] and miss[1].total == 4
    assert foe["recharge_spent"] == ["Acid Spit"]

    with use_rng(ScriptedDice([5])):
        (hit,) = recharge_turn_start(foe)
    assert hit[2] and foe["recharge_spent"] == []
    assert [a.name for a in attack_plan(stats, spent=foe["recharge_spent"])] == ["Acid Spit"]


@pytest.mark.asyncio
async def test_recharge_attack_is_used_once_then_waits_in_the_round(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 1: the spitter uses Acid Spit (17 + ... hits AC 14 for 7 with 3 + 4 on 2d6). Round 2: its
    d6 at the start of its turn does not recharge (4), so it makes its Claw (1 misses) instead."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact(RECHARGE_TEXT, "Acid Spitter")], WAIT)
    with use_rng(ScriptedDice([15, 3, 4])):
        first = await combat_round_node(state)
    spitter = first["combat_state"]["combatants"][1]
    assert spitter["recharge_spent"] == ["Acid Spit"]
    assert "Acid Spitter's Acid Spit hits Dara for 7 acid damage (rolled 19 vs AC 14)." in _summary(
        first
    )

    follow = _next_round(first, "I wait and watch", WAIT, round_num=2)
    with use_rng(ScriptedDice([4, 1])):
        second = await combat_round_node(follow)
    assert "Acid Spitter's Acid Spit does not recharge yet (rolled 4 on the d6)." in _summary(
        second
    )
    assert second["combat_state"]["combatants"][1]["recharge_spent"] == ["Acid Spit"]
    assert [
        d["purpose"]
        for d in second["adjudication_result"].dice_log
        if "Acid Spitter" in d.get("purpose", "")
    ][-1] == "Acid Spitter Claw attack"


def test_the_roster_states_whether_a_recharge_attack_is_ready_or_spent() -> None:
    foe = _exact(RECHARGE_TEXT, "Acid Spitter")
    assert "Acid Spit recharges on 5-6 (ready)" in narrator_roster([_hero(CLERIC_3), foe], [])
    spend_recharge(foe, stats_of(foe).attacks[0])
    assert "Acid Spit recharges on 5-6 (spent until its d6 recharge)" in narrator_roster(
        [_hero(CLERIC_3), foe], []
    )
