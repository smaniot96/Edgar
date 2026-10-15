"""EDG-026: Hit Dice are a persisted sheet resource spent by short rests and recovered by long rests.

Pure engine tests: no database. Persistence of the spent count is exercised in
tests/test_hit_dice_persistence.py; the roll record in tests/api/test_roll_record.py.
"""

import random

import pytest

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, RestRecord
from agent.resolution import (
    HIT_DICE_REFUSAL,
    build_rest_record,
    enforce_side_effects,
    heal_from_rest,
    hit_dice_after_long_rest,
    hit_dice_recovered_by_long_rest,
    hit_dice_remaining,
    hit_dice_spent,
    hit_dice_status,
    hit_die_sides,
)


def _char(
    cls: str = "Fighter",
    *,
    level: int = 1,
    hp: int = 5,
    hp_max: int = 12,
    con: int = 14,
    spent: int | None = None,
) -> dict:
    stats = {"STR": 10, "DEX": 10, "CON": con, "INT": 10, "WIS": 10, "CHA": 10}
    if spent is not None:
        stats["hit_dice"] = {"spent": spent}
    return {
        "id": 1,
        "name": "Eda",
        "class": cls,
        "level": level,
        "hp_current": hp,
        "hp_max": hp_max,
        "stats": stats,
        "inventory": {"items": []},
    }


def _rest(character: dict, rest: str, seed: int = 0) -> AdjudicationResult:
    adj = AdjudicationResult(success=True)
    enforce_side_effects(
        adj,
        character=character,
        check_result=None,
        player_input=rest,
        rng=random.Random(seed),
    )
    return adj


def _with_spent(character: dict, adj: AdjudicationResult) -> dict:
    """What the persistence layer does with the engine's count: the next turn reads it back."""
    spent = adj.character_update.hit_dice_spent if adj.character_update else None
    if spent is None:
        return character
    return {**character, "stats": {**character["stats"], "hit_dice": {"spent": spent}}}


@pytest.mark.parametrize(
    ("cls", "sides"),
    [
        ("Sorcerer", 6),
        ("Wizard", 6),
        ("Bard", 8),
        ("Cleric", 8),
        ("Druid", 8),
        ("Monk", 8),
        ("Rogue", 8),
        ("Warlock", 8),
        ("Fighter", 10),
        ("Paladin", 10),
        ("Ranger", 10),
        ("Barbarian", 12),
    ],
)
def test_hit_die_follows_the_class(cls: str, sides: int) -> None:
    character = _char(cls, level=2)

    assert hit_die_sides(character) == sides
    assert hit_dice_status(character) == {"remaining": 2, "total": 2, "die": f"d{sides}"}


def test_legacy_and_malformed_sheets_have_nothing_spent() -> None:
    legacy = _char(level=3)  # no key written yet: all three dice are available
    assert hit_dice_spent(legacy) == 0 and hit_dice_remaining(legacy) == 3

    assert hit_dice_remaining(_char(level=3, spent=None)) == 3
    assert hit_dice_remaining({**_char(level=3), "stats": {"hit_dice": "spent"}}) == 3
    assert hit_dice_remaining({**_char(level=3), "stats": {"hit_dice": {"spent": "two"}}}) == 3
    assert hit_dice_remaining({**_char(level=3), "stats": {"hit_dice": {"spent": True}}}) == 3
    # A count above the level is clamped: it can never show a negative remainder.
    assert hit_dice_remaining(_char(level=3, spent=9)) == 0
    assert hit_dice_remaining(_char(level=3, spent=-4)) == 3
    assert hit_dice_remaining(None) == 1  # no sheet: one level-1 die, never a crash


def test_short_rest_spends_one_die_and_records_the_roll() -> None:
    character = _char(level=1, hp=5)

    adj = _rest(character, "I take a short rest")

    assert adj.character_update is not None and adj.character_update.hit_dice_spent == 1
    record = adj.rest_record
    assert record is not None and record.kind == "short" and not record.refused
    assert record.die == "d10" and record.expression == "1d10+2"
    assert record.rolled is not None and 3 <= record.rolled <= 12
    assert record.healed == min(record.rolled, 7)  # never more than the 7 HP missing
    assert adj.character_update.hp_delta == record.healed
    assert (record.hit_dice_remaining, record.hit_dice_total) == (0, 1)
    rolls = [d for d in adj.dice_log if d.get("purpose") == "short rest hit die"]
    assert len(rolls) == 1 and rolls[0]["total"] == record.rolled
    assert rolls[0]["expression"] == "1d10+2"


def test_short_rest_is_refused_without_a_roll_when_no_dice_are_left() -> None:
    character = _char(level=1, hp=5, spent=1)

    assert heal_from_rest(character, "short", random.Random(1)) is None
    adj = _rest(character, "I take a short rest")

    assert adj.character_update is None  # nothing spent, nothing healed, nothing rolled
    assert not adj.dice_log
    assert HIT_DICE_REFUSAL.format(hero="Eda") in adj.mechanical_summary
    record = adj.rest_record
    assert record is not None and record.refused and record.healed == 0
    assert record.expression is None and record.rolled is None
    assert (record.die, record.hit_dice_remaining, record.hit_dice_total) == ("d10", 0, 1)


def test_repeated_rests_follow_the_ledger_and_the_long_rest_recovers() -> None:
    character = _char(level=3, hp=1, hp_max=60, con=10)  # CON 10: plain die, no modifier

    remaining_after_each_short_rest = []
    for seed in range(4):
        adj = _rest(character, "I take a short rest", seed=seed)
        remaining_after_each_short_rest.append(adj.rest_record.hit_dice_remaining)
        character = _with_spent(character, adj)
    # Three dice are spent one by one; the fourth short rest is refused and writes nothing.
    assert remaining_after_each_short_rest == [2, 1, 0, 0]
    assert character["stats"]["hit_dice"] == {"spent": 3}
    assert adj.rest_record.refused and adj.character_update is None

    long = _rest(character, "I sleep for the night")
    assert long.rest_record.hit_dice_recovered == 1  # half of level 3 is 1
    assert long.rest_record.hit_dice_remaining == 1
    assert long.character_update.hit_dice_spent == 2
    character = _with_spent(character, long)

    again = _rest(character, "I take a short rest", seed=9)
    assert again.rest_record.hit_dice_remaining == 0 and not again.rest_record.refused


@pytest.mark.parametrize(
    ("level", "spent_before", "spent_after"),
    [
        (3, 3, 2),  # half of 3 is 1
        (4, 4, 2),  # half of 4 is 2
        (4, 1, 0),  # recovery never drops the spent count below zero
        (5, 5, 3),  # half of 5 is 2
        (10, 10, 5),  # half of 10 is 5
    ],
)
def test_long_rest_recovers_up_to_half_the_total_rounded_down(
    level: int, spent_before: int, spent_after: int
) -> None:
    # PHB p.187: "up to a number of dice equal to half of the character's total number of them."
    character = _char(level=level, hp=1, hp_max=60, spent=spent_before)

    assert hit_dice_recovered_by_long_rest(character) == level // 2
    adj = _rest(character, "I take a long rest")

    assert adj.character_update.hit_dice_spent == spent_after
    assert adj.rest_record.hit_dice_recovered == spent_before - spent_after
    assert adj.rest_record.hit_dice_remaining == level - spent_after


def test_level_one_long_rest_recovers_no_hit_die() -> None:
    # PHB p.187: "up to a number of dice equal to half of the character's total number of them".
    # Half of one die rounds down to zero, so the spent level-1 die stays spent.
    spent = _char(level=1, hp=1, hp_max=12, spent=1)
    adj = _rest(spent, "I take a long rest")

    assert hit_dice_recovered_by_long_rest(spent) == 0
    assert adj.character_update.hit_dice_spent is None  # the ledger is unchanged, nothing written
    assert adj.rest_record.hit_dice_recovered == 0
    assert adj.rest_record.hit_dice_remaining == 0


def test_long_rest_leaves_untouched_dice_alone() -> None:
    full = _char(level=3, hp=1, hp_max=60)  # no key: nothing spent to recover
    adj = _rest(full, "I take a long rest")

    assert hit_dice_after_long_rest(full) is None
    assert adj.character_update.hit_dice_spent is None  # no write for an unchanged ledger
    assert adj.rest_record.hit_dice_recovered == 0


def test_healing_never_exceeds_max_hp_but_still_spends_the_die() -> None:
    character = _char(level=1, hp=11, hp_max=12)

    for seed in range(8):
        adj = _rest(character, "I take a short rest", seed=seed)
        record = adj.rest_record
        assert record.healed == min(record.rolled, 1)
        assert adj.character_update.hp_delta == record.healed
        assert adj.character_update.hit_dice_spent == 1  # the die is spent even when HP is short

    full = _char(level=1, hp=12, hp_max=12)
    adj = _rest(full, "I take a short rest")
    assert adj.rest_record.healed == 0
    assert adj.character_update.hp_delta is None
    assert adj.character_update.hit_dice_spent == 1


def test_con_modifier_is_applied_with_no_minimum_healing() -> None:
    # PHB p.187: a short rest regains hit points equal to the roll plus CON. There is no minimum of
    # one, so a roll that the CON penalty takes below zero regains nothing (the die is still spent).
    wizard = _char("Wizard", level=1, hp=1, hp_max=30, con=6)  # CON 6 is -2

    amounts = []
    for seed in range(60):
        healed = heal_from_rest(wizard, "short", random.Random(seed))
        assert healed is not None and healed.dice is not None
        assert healed.dice.expression == "1d6-2"
        assert healed.amount == max(0, healed.dice.total)
        amounts.append((healed.dice.total, healed.amount))
    # A d6 rolls 1 or 2 about a third of the time, so the zero floor is exercised.
    assert any(total < 1 and amount == 0 for total, amount in amounts)


def test_model_cannot_propose_hit_dice_or_rest_evidence() -> None:
    character = _char(level=2, hp=5, spent=0)
    adj = AdjudicationResult(
        success=True,
        character_update=CharacterUpdate(hit_dice_spent=0, hp_delta=None),
        rest_record=RestRecord(
            kind="short",
            die="d10",
            healed=50,
            hit_dice_remaining=9,
            hit_dice_total=9,
        ),
    )

    enforce_side_effects(
        adj, character=character, check_result=None, player_input="I drink some water"
    )

    assert adj.rest_record is None
    assert adj.character_update.hit_dice_spent is None


def test_rest_record_is_built_only_from_engine_numbers() -> None:
    character = _char(level=2, hp=5, hp_max=12, spent=1)
    healed = heal_from_rest(character, "short", random.Random(3))

    record = build_rest_record(character, "short", healed, spent_after=2)

    assert record.hit_dice_remaining == 0 and record.hit_dice_total == 2
    assert record.expression == "1d10+2" and record.die == "d10"
    assert record.rolled == healed.dice.total
    assert record.healed == min(healed.amount, 7)


def test_rest_without_a_sheet_neither_rolls_nor_claims_a_refusal() -> None:
    adj = AdjudicationResult(success=True)
    enforce_side_effects(adj, character=None, check_result=None, player_input="I take a short rest")

    assert adj.rest_record is None and not adj.dice_log
    assert "Hit Dice" not in adj.mechanical_summary
