"""Dice parsing and rolling (tools.dice)."""

import random

import pytest

from tools.dice import DiceOutcome, parse_dice, roll


def test_parse_simple_roll() -> None:
    p = parse_dice("2d6")
    assert p.count == 2
    assert p.sides == 6
    assert p.modifier == 0


def test_parse_with_modifier() -> None:
    p = parse_dice("1d20+5")
    assert p.count == 1
    assert p.sides == 20
    assert p.modifier == 5


def test_parse_negative_modifier() -> None:
    p = parse_dice("3d4-2")
    assert p.count == 3
    assert p.sides == 4
    assert p.modifier == -2


def test_parse_invalid_expression() -> None:
    with pytest.raises(ValueError, match="Invalid dice expression"):
        parse_dice("not-dice")


def test_parse_invalid_count() -> None:
    with pytest.raises(ValueError, match="Dice count"):
        parse_dice("0d6")


def test_parse_invalid_sides() -> None:
    with pytest.raises(ValueError, match="Dice sides"):
        parse_dice("1d1")


def test_roll_deterministic_rng() -> None:
    rng = random.Random(42)
    out = roll("2d6+1", rng=rng)
    assert isinstance(out, DiceOutcome)
    assert out.expression == "2d6+1"
    assert len(out.rolls) == 2
    assert all(1 <= r <= 6 for r in out.rolls)
    assert out.total == sum(out.rolls) + 1


def test_roll_modifier_only_applied_once() -> None:
    rng = random.Random(0)
    out = roll("1d4+10", rng=rng)
    assert out.total == out.rolls[0] + 10


# --- Lenient parsing: the adjudicator must not 503 a turn over cosmetic formatting. ---

def test_parse_implicit_single_die() -> None:
    p = parse_dice("d20")
    assert p.count == 1
    assert p.sides == 20
    assert p.modifier == 0


def test_parse_uppercase_d() -> None:
    p = parse_dice("1D8")
    assert p.count == 1
    assert p.sides == 8


def test_parse_whitespace_around_modifier() -> None:
    p = parse_dice("2d6 + 3")
    assert p.count == 2
    assert p.sides == 6
    assert p.modifier == 3


def test_parse_implicit_die_with_spaced_negative() -> None:
    p = parse_dice("d20 - 1")
    assert p.count == 1
    assert p.sides == 20
    assert p.modifier == -1
