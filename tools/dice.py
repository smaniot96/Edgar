import re
import random
from typing import NamedTuple

from pydantic import BaseModel

MAX_DICE = 100
MAX_SIDES = 100
MIN_SIDES = 2

_DICE_PATTERN = re.compile(r"^(\d+)d(\d+)([+-]\d+)?$")


class ParsedDice(NamedTuple):
    count: int
    sides: int
    modifier: int


class DiceOutcome(BaseModel):
    """Structured result of a dice roll."""
    expression: str
    total: int
    rolls: list[int]
    modifier: int


def parse_dice(expression: str) -> ParsedDice:
    """
    Parse a dice expression like "2d6+3" or "1d20".
    Raises ValueError if the expression is invalid.
    """
    expression = expression.strip()
    match = _DICE_PATTERN.fullmatch(expression)
    if not match:
        raise ValueError(f"Invalid dice expression: {expression!r}. Use format NdM or NdM+K (e.g. 2d6+3).")

    count = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0

    if count < 1 or count > MAX_DICE:
        raise ValueError(f"Dice count must be 1–{MAX_DICE}, got {count}")
    if sides < MIN_SIDES or sides > MAX_SIDES:
        raise ValueError(f"Dice sides must be {MIN_SIDES}–{MAX_SIDES}, got {sides}")

    return ParsedDice(count=count, sides=sides, modifier=modifier)


def _roll_dice(count: int, sides: int, rng: random.Random | None = None) -> list[int]:
    """
    Roll `count` dice with `sides` each.
    Returns list of individual results in [1, sides].
    """
    if rng is None:
        rng = random.SystemRandom()
    return [rng.randint(1, sides) for _ in range(count)]


def roll(expression: str, rng: random.Random | None = None) -> DiceOutcome:
    """
    Roll dice from expression (e.g. "2d6+3").
    Returns DiceOutcome with total, individual rolls, and modifier.
    Raises ValueError if expression is invalid.
    """
    parsed = parse_dice(expression)
    rolls = _roll_dice(parsed.count, parsed.sides, rng)
    total = sum(rolls) + parsed.modifier
    return DiceOutcome(
        expression=expression,
        total=total,
        rolls=rolls,
        modifier=parsed.modifier,
    )
