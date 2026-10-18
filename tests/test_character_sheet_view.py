"""EDG-173: the character sheet shows the engine's AC, castable spells and proficiencies.

Every number is read from the engine's own helpers, so the five level-1 presets must match what the
combat and check rolls use. Nothing here is stored or rewritten.
"""

from datetime import UTC, datetime

import pytest

from agent.combat_setup import _player_ac
from agent.spells import castable_spells
from apps.api.schemas.character import CharacterAssignmentHistoryRead
from apps.api.services import character_assignments as assignments
from apps.api.services.character_presets import CHARACTER_PRESETS


def _sheet(preset: dict) -> dict:
    return {
        "class": preset["character_class"],
        "level": preset["level"],
        "stats": dict(preset["stats"]),
        "inventory": dict(preset["inventory"]),
    }


PRESETS = {preset["key"]: preset for preset in CHARACTER_PRESETS}


@pytest.mark.parametrize(
    ("key", "armor_class"),
    [
        ("fighter_l1", 18),  # chain mail 16 + shield 2 (DEX +1 is not added to heavy armor)
        ("wizard_l1", 12),  # unarmoured: 10 + DEX +2
        ("rogue_l1", 14),  # leather 11 + DEX +3
        ("cleric_l1", 14),  # scale mail 13 + DEX (0, capped at +2) + 1
        ("ranger_l1", 14),  # leather 11 + DEX +3
    ],
)
def test_armor_class_matches_the_engine_for_each_preset(key: str, armor_class: int) -> None:
    sheet = _sheet(PRESETS[key])
    view = assignments.sheet_view(sheet)
    assert view["armor_class"] == armor_class
    assert view["armor_class"] == _player_ac(sheet)


def test_cleric_lists_only_the_spells_its_slots_allow() -> None:
    # Level 1 cleric: cantrip and first-level spells only, not Hold Person or Blindness (2nd level).
    view = assignments.sheet_view(_sheet(PRESETS["cleric_l1"]))
    assert [spell["name"] for spell in view["spells"]] == [
        "Sacred Flame",
        "Cure Wounds",
        "Guiding Bolt",
        "Healing Word",
    ]
    healing_word = next(spell for spell in view["spells"] if spell["name"] == "Healing Word")
    assert healing_word == {"name": "Healing Word", "level": 1, "action": "bonus_action"}


def test_non_casters_have_no_spell_list_and_paladins_wait_for_first_level_slots() -> None:
    assert "spells" not in assignments.sheet_view(_sheet(PRESETS["fighter_l1"]))
    # A level 1 paladin has no slots yet, so no leveled spell is castable.
    assert castable_spells({"class": "Paladin", "level": 1, "stats": {}}) == []


def test_proficiencies_carry_engine_modifiers_for_saves_and_skills() -> None:
    cleric = assignments.sheet_view(_sheet(PRESETS["cleric_l1"]))["proficiencies"]
    # WIS 16 (+3) + proficiency +2 for the cleric's WIS save; CHA 12 (+1) + 2.
    assert cleric["saving_throws"] == [
        {"ability": "WIS", "modifier": 5},
        {"ability": "CHA", "modifier": 3},
    ]
    # Religion is INT (10, +0) with proficiency; insight is WIS (+3) with proficiency.
    assert cleric["skills"] == [
        {"skill": "insight", "modifier": 5},
        {"skill": "religion", "modifier": 2},
    ]


def test_sheet_view_is_read_only() -> None:
    preset = _sheet(PRESETS["wizard_l1"])
    before = repr(preset)
    assignments.sheet_view(preset)
    assert repr(preset) == before


def test_assignment_history_row_carries_the_sheet_view() -> None:
    sheet = _sheet(PRESETS["cleric_l1"])
    row = CharacterAssignmentHistoryRead(
        id=1,
        campaign_id=1,
        campaign_title="Mines",
        hp_current=8,
        hp_max=8,
        stats=sheet["stats"],
        inventory=sheet["inventory"],
        assigned_at=datetime(2026, 10, 1, tzinfo=UTC),
        ended_at=None,
        status="active",
        **assignments.sheet_view(sheet),
    ).model_dump()
    assert row["armor_class"] == 14
    assert [spell["name"] for spell in row["spells"]][0] == "Sacred Flame"
    assert row["proficiencies"]["skills"][0] == {"skill": "insight", "modifier": 5}


def test_fighter_row_has_no_spells_key() -> None:
    sheet = _sheet(PRESETS["fighter_l1"])
    row = CharacterAssignmentHistoryRead(
        id=1,
        campaign_id=1,
        campaign_title="Mines",
        hp_current=12,
        hp_max=12,
        stats=sheet["stats"],
        inventory=sheet["inventory"],
        assigned_at=datetime(2026, 10, 1, tzinfo=UTC),
        ended_at=None,
        status="active",
        **assignments.sheet_view(sheet),
    ).model_dump()
    assert row["spells"] is None
    assert row["armor_class"] == 18
