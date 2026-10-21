"""2014 DMG threshold tables and helpers (EDG-153). Expected values are literal fixtures."""

import pytest

from agent.dmg_thresholds import (
    CR_ORDER,
    CR_XP,
    XP_THRESHOLDS,
    adjusted_xp,
    cr_xp,
    deadly_threshold,
    defensive_cr,
    difficulty,
    implied_cr,
    implied_xp,
    offensive_cr,
    printed_challenge,
    single_hero_multiplier,
    thresholds,
)
from agent.encounter_budget import solo_budget
from edgar_core import creature_xp

# --- literal spot checks against DMG p.82 (XP thresholds), p.275 (CR XP), p.82-83 (multipliers) ---


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (1, (25, 50, 75, 100)),
        (5, (250, 500, 750, 1100)),
        (11, (800, 1600, 2400, 3600)),
        (20, (2800, 5700, 8500, 12700)),
    ],
)
def test_xp_thresholds_match_the_dmg_table(level, expected) -> None:
    assert thresholds(level) == expected
    assert deadly_threshold(level) == expected[3]


def test_every_level_has_four_increasing_thresholds() -> None:
    assert sorted(XP_THRESHOLDS) == list(range(1, 21))
    for level in range(1, 21):
        easy, medium, hard, deadly = thresholds(level)
        assert 0 < easy < medium < hard < deadly


@pytest.mark.parametrize("level", [0, 21, -1])
def test_thresholds_refuse_levels_outside_the_table(level) -> None:
    with pytest.raises(ValueError):
        thresholds(level)


@pytest.mark.parametrize(
    ("label", "xp"),
    [("0", 10), ("1/8", 25), ("1/2", 100), ("1", 200), ("4", 1100), ("11", 7200), ("30", 155000)],
)
def test_cr_xp_matches_the_dmg_table(label, xp) -> None:
    assert cr_xp(label) == xp
    assert set(CR_ORDER) == set(CR_XP)


@pytest.mark.parametrize(
    ("monsters", "multiplier"),
    [(1, 1.5), (2, 2), (3, 2.5), (6, 2.5), (7, 3), (10, 3), (11, 4), (14, 4), (15, 5), (40, 5)],
)
def test_single_hero_multiplier_is_one_step_up_from_the_dmg_table(monsters, multiplier) -> None:
    # DMG p.82 gives 1, 1.5, 2, 2.5, 3, 4 for 1, 2, 3-6, 7-10, 11-14, 15+; DMG p.83 moves a party
    # of fewer than three up one step, so 1.5, 2, 2.5, 3, 4, 5.
    assert float(single_hero_multiplier(monsters)) == multiplier


def test_single_hero_multiplier_refuses_an_empty_encounter() -> None:
    with pytest.raises(ValueError):
        single_hero_multiplier(0)


def test_adjusted_xp_is_the_sum_times_the_multiplier() -> None:
    assert adjusted_xp([]) == 0
    assert adjusted_xp([200]) == 300  # one CR 1 monster at x1.5
    assert adjusted_xp([25, 25]) == 100  # two monsters at x2


# --- parity with the independent verified creature parser ---


def test_cr_xp_agrees_with_verified_parser() -> None:
    assert dict(creature_xp._CR_XP) == CR_XP


@pytest.mark.parametrize(
    ("adjusted", "tier"),
    [
        (24, "trivial"),
        (25, "trivial"),  # equal to easy: no threshold is lower, so trivial
        (26, "easy"),
        (50, "easy"),  # equal to medium: stays in the tier below
        (51, "medium"),
        (75, "medium"),
        (76, "hard"),
        (100, "hard"),  # equal to deadly: not above it
        (101, "deadly"),
    ],
)
def test_difficulty_boundaries_for_level_one(adjusted, tier) -> None:
    assert difficulty(adjusted, 1) == tier


@pytest.mark.parametrize(
    ("adjusted", "tier"),
    [
        (2800, "trivial"),
        (2801, "easy"),
        (5700, "easy"),
        (5701, "medium"),
        (8500, "medium"),
        (8501, "hard"),
        (12700, "hard"),
        (12701, "deadly"),
    ],
)
def test_difficulty_boundaries_for_level_twenty(adjusted, tier) -> None:
    assert difficulty(adjusted, 20) == tier


# --- DMG p.274-275 CR derivation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hp", "label"),
    [(1, "0"), (6, "0"), (7, "1/8"), (35, "1/8"), (36, "1/4"), (70, "1/2"), (71, "1")],
)
def test_defensive_cr_boundaries(hp, label) -> None:
    assert defensive_cr(hp) == label


def test_offensive_cr_shifts_one_step_per_two_points_of_attack_bonus() -> None:
    assert offensive_cr(5, 3) == "1/4"  # the table's own attack bonus: no shift
    assert offensive_cr(5, 5) == "1/2"  # +2 over the table: one step up
    assert offensive_cr(5, 1) == "1/8"  # -2 under the table: one step down
    assert offensive_cr(29, 9) == "6"  # table CR 4 (+5); +4 is two steps up


def test_implied_cr_averages_defensive_and_offensive_with_ties_up() -> None:
    assert implied_cr(30, 5, 3) == "1/4"  # 1/8 and 1/4 average to a tie, rounded up
    assert implied_cr(63, 29, 9) == "3"  # 1/2 and 6 average to 3.25


def test_printed_challenge_reads_one_line_and_refuses_a_mismatch() -> None:
    assert printed_challenge("Ogre\nChallenge 1 (200 XP)\nHit Points 7") == ("1", 200)
    assert printed_challenge("no challenge line here") is None
    assert printed_challenge("Ogre\nChallenge 1 (300 XP)") is None  # printed XP disagrees


# --- the budget table is justified against the thresholds (EDG-153) ------------------------------


def _worst_generic_roster_xp(level: int) -> float:
    """Worst kept generic roster at a level: one boss and the other foes at their HP ceilings.

    Every foe takes the attack and damage ceilings. The HP range is scanned in full, so the test
    does not rely on the ceiling being the largest XP.
    """
    budget = solo_budget(level)
    boss_xp = max(
        implied_xp(hp, budget.damage_avg_max, budget.attack_bonus_max)
        for hp in range(budget.boss_hp[0], budget.boss_hp[1] + 1)
    )
    foe_xp = max(
        implied_xp(hp, budget.damage_avg_max, budget.attack_bonus_max)
        for hp in range(budget.foe_hp[0], budget.foe_hp[1] + 1)
    )
    return float(adjusted_xp([boss_xp] + [foe_xp] * (budget.max_foes - 1)))


@pytest.mark.parametrize("level", range(1, 21))
def test_the_max_generic_roster_never_exceeds_the_deadly_threshold(level) -> None:
    assert _worst_generic_roster_xp(level) <= deadly_threshold(level)


@pytest.mark.parametrize("level", [0, -5, 25])
def test_budget_clamps_out_of_range_levels_to_the_table(level) -> None:
    clamped = max(1, min(20, level))
    assert solo_budget(level) == solo_budget(clamped)
    assert _worst_generic_roster_xp(clamped) <= deadly_threshold(clamped)
