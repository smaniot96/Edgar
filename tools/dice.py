"""Deterministic dice tool used by the agent.

The LLM never picks the number; the adjudicator emits a `dice_expression` and the agent
calls `roll(expression)` to get the authoritative outcome. Tests inject `random.Random(seed)`
for reproducibility; production uses `random.SystemRandom()`.
"""

import random
import re
from typing import NamedTuple

from pydantic import BaseModel

# Bounds chosen to reject obvious abuse (e.g. "100000d100"). 100d100 is already absurd for D&D.
MAX_DICE = 100
MAX_SIDES = 100
MIN_SIDES = 2

_DICE_PATTERN = re.compile(r"^(\d+)d(\d+)([+-]\d+)?$")


class ParsedDice(NamedTuple):
    count: int
    sides: int
    modifier: int


class DiceOutcome(BaseModel):
    """Public dice result. Total = sum(rolls) + modifier; modifier may be negative."""

    expression: str
    total: int
    rolls: list[int]
    modifier: int


def parse_dice(expression: str) -> ParsedDice:
    """Parse `NdM` or `NdM+K` / `NdM-K`. Raises ValueError with a hint on invalid input."""
    expression = expression.strip()
    match = _DICE_PATTERN.fullmatch(expression)
    if not match:
        raise ValueError(
            f"Invalid dice expression: {expression!r}. Use format NdM or NdM+K (e.g. 2d6+3)."
        )

    count = int(match.group(1))
    sides = int(match.group(2))
    modifier = int(match.group(3)) if match.group(3) else 0

    if not 1 <= count <= MAX_DICE:
        raise ValueError(f"Dice count must be 1-{MAX_DICE}, got {count}")
    if not MIN_SIDES <= sides <= MAX_SIDES:
        raise ValueError(f"Dice sides must be {MIN_SIDES}-{MAX_SIDES}, got {sides}")

    return ParsedDice(count=count, sides=sides, modifier=modifier)


def _roll_dice(count: int, sides: int, rng: random.Random | None = None) -> list[int]:
    """Default RNG is SystemRandom (cryptographic) so production rolls are not predictable."""
    if rng is None:
        rng = random.SystemRandom()
    return [rng.randint(1, sides) for _ in range(count)]


def roll(expression: str, rng: random.Random | None = None) -> DiceOutcome:
    parsed = parse_dice(expression)
    rolls = _roll_dice(parsed.count, parsed.sides, rng)
    return DiceOutcome(
        expression=expression,
        total=sum(rolls) + parsed.modifier,
        rolls=rolls,
        modifier=parsed.modifier,
    )
