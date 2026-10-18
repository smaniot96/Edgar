"""EDG-045: milestone advancement tables, level-up rules and the ASI choice, checked against the PHB.

Book values are read from the class tables of the 2014 Player's Handbook (PDF page numbers, as
elsewhere in this repo): Cleric p.58, Fighter p.72, Ranger p.91, Rogue p.96, Wizard p.114, and the
class "Hit Points at Higher Levels" lines on the same pages. The engine's own numbers come from
agent.progression, agent.spells.slot_totals and agent.class_features, never from these tests.
"""

from __future__ import annotations

import pytest

from agent.class_features import max_uses
from agent.progression import (
    MAX_ADVANCED_LEVEL,
    ProgressionError,
    apply_asi_to_stats,
    hit_point_gain,
    level_features,
    milestone_granted,
    plan_level_up,
    validate_asi,
)
from agent.resolution import proficiency_bonus
from agent.spells import slot_totals
from tests.agent.test_spells import ScriptedDice
from tools.dice import use_rng


def _sheet(cls: str, level: int = 1, con: int = 14, **stats: int) -> dict:
    base = {"STR": 10, "DEX": 10, "CON": con, "INT": 10, "WIS": 10, "CHA": 10}
    base.update(stats)
    return {"id": 1, "name": "Hero", "class": cls, "level": level, "stats": base}


# PHB, PDF p.72 (Fighter, Hit Points at Higher Levels: 1d10 (or 6)), p.96 (Rogue: 1d8 (or 5)),
# p.91 (Ranger: 1d10 (or 6)), p.58 (Cleric: 1d8 (or 5)), p.114 (Wizard: 1d6 (or 4)).
@pytest.mark.parametrize(
    ("cls", "book_fixed_value"),
    [("fighter", 6), ("rogue", 5), ("ranger", 6), ("cleric", 5), ("wizard", 4)],
)
def test_average_hit_points_are_the_class_table_fixed_value(
    cls: str, book_fixed_value: int
) -> None:
    gain = hit_point_gain(_sheet(cls, con=14), "average")  # CON 14 gives +2
    assert gain.amount == book_fixed_value + 2
    assert gain.dice is None and gain.method == "average"


def test_rolled_hit_points_use_the_engine_die_and_the_constitution_modifier() -> None:
    with use_rng(ScriptedDice([7])):  # a d10 face of 7 on a fighter, plus CON +2
        gain = hit_point_gain(_sheet("fighter", con=14), "roll")
    assert gain.amount == 9
    assert gain.dice is not None and gain.dice.rolls == [7] and gain.dice.expression == "1d10"


def test_a_level_never_adds_negative_hit_points() -> None:
    with use_rng(ScriptedDice([1])):  # a d6 face of 1 with CON 3 (-4)
        gain = hit_point_gain(_sheet("wizard", con=3), "roll")
    assert gain.amount == 0


def test_an_unknown_hit_point_method_is_refused() -> None:
    with pytest.raises(ProgressionError):
        hit_point_gain(_sheet("fighter"), "max")


# PHB proficiency bonus column, PDF pp.58, 72, 91, 96, 114: +2 at levels 1-4, +3 at level 5.
@pytest.mark.parametrize("cls", ["fighter", "rogue", "wizard", "cleric", "ranger"])
def test_proficiency_bonus_follows_the_book_to_level_five(cls: str) -> None:
    assert [proficiency_bonus(level) for level in range(1, 6)] == [2, 2, 2, 2, 3]


# PHB Wizard and Cleric slot tables (full casters, PDF pp.114 and 58), Ranger table (half caster,
# PDF p.91). Totals are per spell level 1, 2, 3, read for each character level 1-5.
@pytest.mark.parametrize(
    ("cls", "level", "expected"),
    [
        ("wizard", 1, {1: 2}),
        ("wizard", 2, {1: 3}),
        ("wizard", 3, {1: 4, 2: 2}),
        ("wizard", 4, {1: 4, 2: 3}),
        ("wizard", 5, {1: 4, 2: 3, 3: 2}),
        ("cleric", 1, {1: 2}),
        ("cleric", 2, {1: 3}),
        ("cleric", 3, {1: 4, 2: 2}),
        ("cleric", 4, {1: 4, 2: 3}),
        ("cleric", 5, {1: 4, 2: 3, 3: 2}),
        ("ranger", 1, {}),
        ("ranger", 2, {1: 2}),
        ("ranger", 3, {1: 3}),
        ("ranger", 4, {1: 3}),
        ("ranger", 5, {1: 4, 2: 2}),
    ],
)
def test_spell_slot_totals_follow_the_phb_tables(cls: str, level: int, expected: dict) -> None:
    assert slot_totals(_sheet(cls, level=level)) == expected


def test_a_fighter_gains_action_surge_at_level_two_and_no_extra_use() -> None:
    # PHB Fighter table, PDF p.72: Action Surge (one use) from 2nd level.
    assert max_uses(_sheet("fighter", level=1), "action_surge") == 0
    assert max_uses(_sheet("fighter", level=2), "action_surge") == 1
    assert max_uses(_sheet("fighter", level=5), "action_surge") == 1


def test_level_features_name_the_book_features_and_mark_what_the_engine_runs() -> None:
    # Fighter, PDF p.72: Action Surge (2) is executed; Martial Archetype (3) and Extra Attack (5)
    # are named, not executed.
    assert level_features("fighter", 2) == [{"name": "Action Surge", "executed": True}]
    assert level_features("fighter", 3) == [{"name": "Martial Archetype", "executed": False}]
    assert level_features("fighter", 5) == [{"name": "Extra Attack", "executed": False}]
    # Rogue, PDF p.96: Sneak Attack dice rise to 2d6 at 3rd level and 3d6 at 5th (executed).
    assert {"name": "Sneak Attack (2d6)", "executed": True} in level_features("rogue", 3)
    assert {"name": "Sneak Attack (3d6)", "executed": True} in level_features("rogue", 5)
    # Every supported class gains an Ability Score Improvement at 4th level (PHB, PDF p.73).
    for cls in ("fighter", "rogue", "wizard", "cleric", "ranger"):
        assert {"name": "Ability Score Improvement", "executed": True} in level_features(cls, 4)


def test_ability_score_improvement_is_two_points_at_level_four_only() -> None:
    scores = {"STR": 15, "DEX": 14, "CON": 14, "INT": 8, "WIS": 10, "CHA": 8}
    assert validate_asi(4, {"dex": 2}, scores) == {"DEX": 2}
    assert validate_asi(4, {"STR": 1, "con": 1}, scores) == {"STR": 1, "CON": 1}
    with pytest.raises(ProgressionError):
        validate_asi(4, None, scores)  # the choice is required at level 4
    with pytest.raises(ProgressionError):
        validate_asi(4, {"DEX": 1}, scores)  # one point is not the rule
    with pytest.raises(ProgressionError):
        validate_asi(4, {"DEX": 3}, scores)  # no score takes more than two points
    with pytest.raises(ProgressionError):
        validate_asi(4, {"DEX": 2, "STR": 1}, scores)  # three points in total
    with pytest.raises(ProgressionError):
        validate_asi(4, {"LUCK": 2}, scores)  # not an ability
    with pytest.raises(ProgressionError):
        validate_asi(4, {"STR": 2}, {**scores, "STR": 19})  # would exceed 20
    with pytest.raises(ProgressionError):
        validate_asi(2, {"DEX": 2}, scores)  # not at level 2


def test_a_level_up_plan_checks_level_milestone_class_and_cap() -> None:
    fighter = _sheet("fighter", level=1)
    plan = plan_level_up(fighter, from_level=1, milestones_pending=1, hp_method="average")
    assert (plan.from_level, plan.to_level) == (1, 2)
    assert plan.hit_points.amount == 6 + 2

    with pytest.raises(ProgressionError):  # the client saw an older level
        plan_level_up(fighter, from_level=2, milestones_pending=1)
    with pytest.raises(ProgressionError):  # no milestone waiting
        plan_level_up(fighter, from_level=1, milestones_pending=0)
    with pytest.raises(ProgressionError):  # a class the engine does not advance
        plan_level_up(_sheet("barbarian"), from_level=1, milestones_pending=1)
    capped = _sheet("fighter", level=MAX_ADVANCED_LEVEL)
    with pytest.raises(ProgressionError):
        plan_level_up(capped, from_level=MAX_ADVANCED_LEVEL, milestones_pending=1)


def test_milestones_stop_once_the_cap_is_reachable() -> None:
    # A milestone is granted only while level + pending stays below the cap (5).
    assert milestone_granted(1, 0) is True
    assert milestone_granted(1, 3) is True  # 1 + 3 = 4: one more can still be spent
    assert milestone_granted(1, 4) is False  # 1 + 4 = 5: the cap is already covered
    assert milestone_granted(4, 0) is True
    assert milestone_granted(4, 1) is False


def test_asi_keeps_the_sheet_spelling_and_caps_at_twenty() -> None:
    stats = apply_asi_to_stats({"dex": 14, "STR": 8}, {"DEX": 2, "CON": 1})
    # A score the sheet does not list starts from the default of 10.
    assert stats == {"dex": 16, "STR": 8, "CON": 11}
    assert apply_asi_to_stats({"STR": 19}, {"STR": 2}) == {"STR": 20}
