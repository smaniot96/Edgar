"""Exact creatures above a solo hero's deadly threshold (EDG-153): a warning, never a rescale."""

import pytest

from agent.encounter_budget import (
    TIER_ABOVE_DEADLY,
    TIER_FAR_ABOVE_DEADLY,
    apply_solo_budget,
    solo_budget,
)


def exact(name, *, xp, cr="1/8", hp=7, boss=False):
    return {
        "name": name,
        "creature_id": "c" * 64,
        "source_reference": {"creature_id": "c" * 64},
        "hp_max": hp,
        "hp_current": hp,
        "ac": 13,
        "attack_bonus": 3,
        "damage_dice": "1d6+1",
        "initiative_bonus": 1,
        "is_boss": boss,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "challenge_cr": cr,
        "challenge_xp": xp,
    }


def generic(name, *, hp=9, bonus=3, dice="1d4", boss=False):
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp,
        "ac": 12,
        "attack_bonus": bonus,
        "damage_dice": dice,
        "initiative_bonus": 1,
        "is_boss": boss,
        "is_player": False,
        "disposition": "active",
        "alive": True,
    }


# --- tiers and their exact boundaries at level 1 (deadly threshold 100) ----------------------


@pytest.mark.parametrize(
    ("raw", "tier"),
    [
        (50, None),  # adjusted 100 == deadly: informational, not a warning
        (51, TIER_ABOVE_DEADLY),  # adjusted 102: just above deadly
        (99, TIER_ABOVE_DEADLY),  # adjusted 198: still below twice deadly
        (100, TIER_FAR_ABOVE_DEADLY),  # adjusted 200 == twice deadly
    ],
)
def test_two_exact_foes_at_the_warning_boundaries(raw, tier) -> None:
    low, high = raw // 2, raw - raw // 2
    roster = [exact("Ghoul A", xp=low), exact("Ghoul B", xp=high)]
    out, summary = apply_solo_budget(roster, 1)
    assert out == roster  # exact records are never changed
    check = summary["xp_check"]
    assert check["raw_xp"] == raw and check["multiplier"] == 2.0
    assert check["warning"] == tier
    assert (summary["warning"] or {}).get("tier") == tier


def test_one_exact_foe_at_the_boundary_has_no_warning() -> None:
    # One monster at x1.5: 66 XP is 99 adjusted (below deadly), 67 XP is 100.5 (above).
    _, below = apply_solo_budget([exact("Ogre", xp=66)], 1)
    _, above = apply_solo_budget([exact("Ogre", xp=67)], 1)
    assert below["xp_check"]["warning"] is None and below["warning"] is None
    assert above["xp_check"]["warning"] == TIER_ABOVE_DEADLY


def test_a_cr_one_boss_alone_is_far_above_a_level_one_deadly_threshold() -> None:
    # Goblin Boss, CR 1, 200 XP, alone at x1.5 is 300, which is 3 x the deadly threshold of 100.
    roster = [exact("Goblin Boss", xp=200, cr="1", hp=21, boss=True)]
    out, summary = apply_solo_budget(roster, 1)
    assert out == roster
    check = summary["xp_check"]
    assert check["adjusted_xp"] == 300.0 and check["difficulty"] == "deadly"
    assert check["exact_foes"] == [{"name": "Goblin Boss", "cr": "1", "xp": 200}]
    assert summary["warning"]["tier"] == TIER_FAR_ABOVE_DEADLY


def test_the_warning_text_names_the_exact_foes_and_the_numbers() -> None:
    _, summary = apply_solo_budget([exact("Goblin Boss", xp=200, cr="1", boss=True)], 1)
    text = summary["warning"]["text"]
    assert text.startswith(
        "Warning: this fight is far above a solo level 1 hero's deadly threshold"
    )
    assert "Goblin Boss (CR 1, 200 XP)" in text
    assert "deadly threshold of 100 XP" in text
    assert "300 adjusted XP" in text
    assert "Foes: Goblin Boss (CR 1, 200 XP)." in text
    assert "not rescaled" in text and "not refused" in text


def test_an_above_deadly_warning_says_above_not_far_above() -> None:
    _, summary = apply_solo_budget([exact("Ghoul", xp=100, cr="1/2")], 1)
    assert summary["warning"]["tier"] == TIER_ABOVE_DEADLY
    assert "this fight is above a solo level 1 hero" in summary["warning"]["text"]


def test_budgeted_foes_are_counted_in_the_warning_text() -> None:
    roster = [exact("Goblin Boss", xp=200, cr="1", boss=True), generic("Goblin", hp=7)]
    _, summary = apply_solo_budget(roster, 1)
    text = summary["warning"]["text"]
    assert "and 1 budgeted foe" in text
    assert summary["xp_check"]["foes"] == 2 and summary["xp_check"]["multiplier"] == 2.0


def test_an_exact_foe_with_unreadable_xp_is_unknown_not_zero() -> None:
    roster = [exact("Mystery", xp=None)]
    out, summary = apply_solo_budget(roster, 1)
    assert out == roster
    assert summary["xp_check"] is None and summary["warning"] is None


def test_an_exact_foe_without_challenge_fields_is_unknown() -> None:
    record = {k: v for k, v in exact("Legacy", xp=0).items() if not k.startswith("challenge")}
    _, summary = apply_solo_budget([record], 1)
    assert summary["xp_check"] is None and summary["warning"] is None


def test_a_generic_only_max_roster_never_warns_at_any_level() -> None:
    for level in range(1, 21):
        budget = solo_budget(level)
        roster = [
            generic("Boss", hp=999, bonus=99, dice="9d20+9", boss=True),
            *[generic(f"Foe {i}", hp=999, bonus=99, dice="9d20+9") for i in range(budget.max_foes)],
        ]
        _, summary = apply_solo_budget(roster, level)
        assert summary["xp_check"]["warning"] is None, level
        assert summary["warning"] is None, level


