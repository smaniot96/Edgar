"""EDG-045: death saving throws and instant death (death_mode 'death_saves'), with scripted dice.

Book rules are PHB, 2014, PDF pp.198-199 ("Damage at 0 Hit Points", "Instant Death", "Death Saving
Throws", "Stabilizing a Creature") and the unconscious creature's critical hit rule in Appendix A.
Every d20 and damage die here comes from `ScriptedDice`, so each sequence is exact. The subdued
mode (the default) is exercised too: a defeat must still leave the hero at 1 HP, unchanged.
"""

from __future__ import annotations

import random

import pytest

from agent.combat_enemy_turns import EnemyTurns
from agent.combat_finalise import settle_round
from agent.combat_player_phase import _death_save_at_turn_start
from agent.combat_round_record import RoundRecord
from agent.death import (
    DEAD,
    DYING,
    STABLE,
    DeathRecord,
    death_save,
    record_from,
    take_damage,
)
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.resolution import DEATH_SAVES_KEY, CheckResult, enforce_side_effects
from tests.agent.test_spells import ScriptedDice
from tools.dice import use_rng

HERO_MAX = 12


def _hero(hp: int, record: dict | None = None) -> dict:
    player = {
        "name": "Eda",
        "display_name": "Eda",
        "is_player": True,
        "init": 10,
        "init_bonus": 2,
        "ac": 14,
        "hp_current": hp,
        "hp_max": HERO_MAX,
        "disposition": "unconscious" if hp <= 0 else "active",
        "alive": True,
    }
    if record is not None:
        player[DEATH_SAVES_KEY] = record
    return player


def _foe() -> dict:
    return {
        "id": "goblin-1",
        "name": "Goblin",
        "is_player": False,
        "init": 5,
        "init_bonus": 1,
        "ac": 13,
        "hp_current": 7,
        "hp_max": 7,
        "disposition": "active",
        "alive": True,
    }


def _round(hp: int, *, death_mode: str = "death_saves", record: dict | None = None) -> RoundRecord:
    player = _hero(hp, record)
    foe = _foe()
    character = {
        "id": 1,
        "name": "Eda",
        "class": "Fighter",
        "level": 1,
        "hp_current": hp,
        "hp_max": HERO_MAX,
        "stats": {"STR": 16, "DEX": 12, "CON": 14},
        "inventory": {"items": []},
    }
    return RoundRecord(
        hero="Eda",
        character=character,
        player=player,
        enemies=[foe],
        combatants=[player, foe],
        player_ac=14,
        round_num=1,
        combat_state={"round": 1, "combatants": [player, foe]},
        hp=hp,
        sw_left=1,
        fled=False,
        second_wind_auto_at_zero_hp=False,
        log_lines=[],
        dice_log=[],
        round_effects=[],
        round_modes=[],
        death_mode=death_mode,
    )


# --- The rules themselves (pure) ---


def test_damage_that_leaves_the_hero_above_zero_changes_no_death_state() -> None:
    out = take_damage(12, HERO_MAX, 5)
    assert (out.hp, out.record, out.failures_added, out.dropped) == (7, None, 0, False)


def test_damage_to_zero_falls_unconscious_unless_the_leftover_kills_outright() -> None:
    # Damage reduces the hero to 0 with 0 left over: unconscious, not dead.
    down = take_damage(12, HERO_MAX, 12)
    assert down.hp == 0 and down.record == DeathRecord(DYING) and down.dropped
    assert down.failures_added == 0 and not down.instant_death
    # PHB example (PDF p.198): a cleric with 12 maximum and 6 current takes 18. The 12 left over
    # equal her maximum, so she dies outright.
    massive = take_damage(6, 12, 18)
    assert massive.record == DeathRecord(DEAD) and massive.instant_death


def test_damage_at_zero_hp_is_a_failure_and_a_critical_hit_is_two() -> None:
    once = take_damage(0, HERO_MAX, 3, record=DeathRecord(DYING))
    assert once.failures_added == 1 and once.record == DeathRecord(DYING, 0, 1)
    twice = take_damage(0, HERO_MAX, 3, critical=True, record=DeathRecord(DYING))
    assert twice.failures_added == 2 and twice.record == DeathRecord(DYING, 0, 2)
    # The third failure is death, whichever way it arrives.
    dead = take_damage(0, HERO_MAX, 3, record=DeathRecord(DYING, 0, 2))
    assert dead.record is not None and dead.record.state == DEAD


def test_damage_that_equals_the_maximum_at_zero_hp_is_instant_death() -> None:
    out = take_damage(0, HERO_MAX, HERO_MAX, record=DeathRecord(DYING, 2, 0))
    assert out.instant_death and out.record == DeathRecord(DEAD)


def test_a_stable_creature_that_takes_damage_makes_death_saves_again() -> None:
    # PHB, PDF p.198: a stable creature that takes damage starts making death saves again.
    out = take_damage(0, HERO_MAX, 2, record=DeathRecord(STABLE))
    assert out.record == DeathRecord(DYING, 0, 1)


def test_a_death_save_of_ten_or_more_succeeds_and_nine_fails() -> None:
    won = death_save(DeathRecord(DYING), ScriptedDice([10]))
    assert won.success and won.record == DeathRecord(DYING, 1, 0)
    lost = death_save(DeathRecord(DYING), ScriptedDice([9]))
    assert not lost.success and lost.record == DeathRecord(DYING, 0, 1)


def test_a_natural_twenty_regains_one_hit_point_and_clears_both_counts() -> None:
    save = death_save(DeathRecord(DYING, 2, 2), ScriptedDice([20]))
    assert save.regained and save.hp == 1 and save.record is None


def test_a_natural_one_counts_as_two_failures() -> None:
    save = death_save(DeathRecord(DYING, 0, 0), ScriptedDice([1]))
    assert not save.success and save.record == DeathRecord(DYING, 0, 2)


def test_three_successes_make_the_hero_stable_and_three_failures_kill() -> None:
    # Successes need not be consecutive: a 9 between two 10s still counts toward three.
    record = DeathRecord(DYING)
    for face in (10, 9, 12, 15):
        save = death_save(record, ScriptedDice([face]))
        record = save.record
        assert record is not None
    assert record == DeathRecord(STABLE)

    record = DeathRecord(DYING)
    for face in (2, 5):
        record = death_save(record, ScriptedDice([face])).record
    final = death_save(record, ScriptedDice([1]))  # a natural 1 is two failures: 1 + 2 = 3
    assert final.record is not None and final.record.state == DEAD


def test_only_a_dying_creature_rolls_death_saves() -> None:
    with pytest.raises(ValueError):
        death_save(DeathRecord(STABLE), random.Random(0))


def test_a_malformed_record_is_no_record() -> None:
    assert record_from({"state": "bogus"}) is None
    assert record_from("dying") is None
    assert record_from({"state": "dying", "successes": True, "failures": 9}) == DeathRecord(
        DYING, 0, 3
    )


# --- The round (scripted dice through the combat record) ---


def test_the_hero_rolls_a_death_save_at_the_start_of_a_turn_at_zero_hp() -> None:
    rec = _round(0)
    with use_rng(ScriptedDice([11])):
        _death_save_at_turn_start(rec)
    assert rec.hp == 0 and not rec.dead
    assert rec.player[DEATH_SAVES_KEY] == {"state": DYING, "successes": 1, "failures": 0}
    assert rec.dice_log and rec.dice_log[-1]["rolls"] == [11]


def test_a_natural_twenty_at_the_turn_start_puts_the_hero_back_on_its_feet() -> None:
    rec = _round(0, record={"state": DYING, "successes": 0, "failures": 2})
    with use_rng(ScriptedDice([20])):
        _death_save_at_turn_start(rec)
    assert rec.hp == 1 and rec.player[DEATH_SAVES_KEY] is None


def test_a_third_failed_save_kills_the_hero_and_the_encounter_is_a_defeat() -> None:
    rec = _round(0, record={"state": DYING, "successes": 0, "failures": 2})
    with use_rng(ScriptedDice([4])):
        _death_save_at_turn_start(rec)
    assert rec.dead
    end = settle_round(rec, declared=[], player_acted=False, max_rounds=10)
    assert end.ended and end.outcome == "defeat"
    assert rec.player["disposition"] == "dead" and rec.player["alive"] is False
    assert any("dies" in line for line in rec.log_lines)


def test_a_foe_hits_an_unconscious_hero_from_five_feet_as_a_critical_hit() -> None:
    # Appendix A: a hit on an unconscious creature within 5 feet is a critical hit. The attack
    # has advantage (two d20 faces, the higher kept): 15 hits AC 14. The critical doubles the
    # 1d6 to 2d6 (2 + 4), and the damage at 0 HP is two failures.
    rec = _round(0, record={"state": DYING, "successes": 0, "failures": 0})
    foe = rec.enemies[0]
    with use_rng(ScriptedDice([15, 3, 2, 4])):
        EnemyTurns(rec).enemy_attack(foe, label=None, bonus=5, expr="1d6", within_five_feet=True)
    assert rec.player[DEATH_SAVES_KEY] == {"state": DYING, "successes": 0, "failures": 2}
    assert rec.hp == 0 and not rec.dead
    assert rec.outcomes[-1]["critical"] is True


def test_massive_damage_at_zero_hp_ends_the_hero_outright() -> None:
    rec = _round(0, record={"state": DYING, "successes": 0, "failures": 0})
    EnemyTurns(rec).damage_player(HERO_MAX, critical=False)
    assert rec.dead and rec.hp == 0
    assert rec.player[DEATH_SAVES_KEY] == {"state": DEAD, "successes": 0, "failures": 0}


def test_a_stable_hero_takes_no_death_save_at_the_turn_start() -> None:
    rec = _round(0, record={"state": STABLE, "successes": 0, "failures": 0})
    before = list(rec.dice_log)
    with use_rng(ScriptedDice([])):  # any roll would exhaust the empty script and fail
        _death_save_at_turn_start(rec)
    assert rec.dice_log == before and rec.hp == 0


def test_subdued_mode_still_leaves_the_hero_at_one_hp_after_a_defeat() -> None:
    # Regression guard: the default campaign behaviour is unchanged (PHB is not applied).
    rec = _round(0, death_mode="subdued")
    end = settle_round(rec, declared=[], player_acted=False, max_rounds=10)
    assert end.outcome == "defeat"
    assert rec.hp == 1 and rec.player["hp_current"] == 1
    assert rec.player["disposition"] == "unconscious"
    assert DEATH_SAVES_KEY not in rec.player or rec.player.get(DEATH_SAVES_KEY) is None


# --- Outside combat (enforce_side_effects) ---


def _sheet(hp: int, stats: dict | None = None) -> dict:
    return {
        "id": 1,
        "name": "Eda",
        "class": "Fighter",
        "level": 1,
        "hp_current": hp,
        "hp_max": HERO_MAX,
        "stats": {"STR": 16, "DEX": 12, "CON": 14, **(stats or {})},
        "inventory": {"items": []},
    }


def test_outside_combat_a_dying_hero_rolls_at_the_start_of_the_turn() -> None:
    adj = AdjudicationResult(success=True, character_update=CharacterUpdate())
    enforce_side_effects(
        adj,
        character=_sheet(0),
        check_result=None,
        player_input="I look around the room",
        in_combat=False,
        death_mode="death_saves",
        rng=ScriptedDice([12]),
    )
    cu = adj.character_update
    assert cu.death_saves == {"state": DYING, "successes": 1, "failures": 0}
    assert cu.hp_delta is None  # no regain, no change to HP
    assert adj.dice_log and adj.dice_log[-1]["rolls"] == [12]


def test_outside_combat_a_natural_twenty_regains_one_hp_and_clears_the_record() -> None:
    adj = AdjudicationResult(success=True, character_update=CharacterUpdate())
    enforce_side_effects(
        adj,
        character=_sheet(0, {DEATH_SAVES_KEY: {"state": DYING, "successes": 1, "failures": 1}}),
        check_result=None,
        player_input="I look around the room",
        in_combat=False,
        death_mode="death_saves",
        rng=ScriptedDice([20]),
    )
    assert adj.character_update.hp_delta == 1
    assert adj.character_update.death_saves == {}


def test_outside_combat_damage_at_zero_hp_is_a_failure_under_death_saves() -> None:
    check = CheckResult(
        check="dexterity", kind="check", ability="DEX", modifier=0, dc=15, against="DC",
        roll_expression="1d20", natural=5, total=5, success=False,
        dice_log=[], natural_rolls=[5],
    )  # fmt: skip
    adj = AdjudicationResult(
        success=False,
        character_update=CharacterUpdate(damage_dice="1d6", hp_delta=-3),
    )
    # The 1d6 trap hits: the death save is rolled first (a 5, a failure), then the damage (a
    # failure more). Both are on the record; the hero stays at 0 HP.
    enforce_side_effects(
        adj,
        character=_sheet(0),
        check_result=check,
        player_input="I step on the pressure plate",
        in_combat=False,
        death_mode="death_saves",
        rng=ScriptedDice([5, 3]),
    )
    cu = adj.character_update
    assert cu.hp_delta is None or cu.hp_delta == 0
    assert cu.death_saves == {"state": DYING, "successes": 0, "failures": 2}


def test_outside_combat_subdued_leaves_death_fields_untouched() -> None:
    adj = AdjudicationResult(success=True, character_update=CharacterUpdate())
    enforce_side_effects(
        adj,
        character=_sheet(0),
        check_result=None,
        player_input="I look around the room",
        in_combat=False,
        death_mode="subdued",
        rng=ScriptedDice([]),  # no death save may be rolled in subdued mode
    )
    assert adj.character_update.death_saves is None
    assert adj.dice_log == []
