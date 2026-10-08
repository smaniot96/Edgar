"""Deterministic dice tool used by the agent.

The LLM never picks the number; the engine builds the expression (e.g. "1d20+3" from the
character sheet) and calls `roll(expression)` to get the authoritative outcome. Every outcome
carries the individual dice (`rolls`) so a turn can be logged and replayed.

RNG selection, in priority order:
  1. an explicit `rng=` argument;
  2. a turn-scoped RNG installed with `use_rng(random.Random(seed))` (a context manager backed
     by a ContextVar, so concurrent turns never share one);
  3. `random.SystemRandom()` (production default: rolls are not predictable).
"""

import contextlib
import contextvars
import random
import re
from collections.abc import Iterator
from typing import NamedTuple

from pydantic import BaseModel

# Bounds chosen to reject obvious abuse (e.g. "100000d100"). 100d100 is already absurd for D&D.
MAX_DICE = 100
MAX_SIDES = 100
MIN_SIDES = 2

# Leading count is optional (`d20` == `1d20`); whitespace around the modifier is tolerated
# (`2d6 + 3`); case-insensitive on the `d`. Normalisation happens in `parse_dice`.
_DICE_PATTERN = re.compile(r"^(\d*)d(\d+)\s*([+-]\s*\d+)?$", re.IGNORECASE)


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
    count: int = 1
    sides: int = 20

    def to_log(self, purpose: str = "") -> dict:
        """Compact, JSON-safe record for event logs / replay."""
        out = {
            "expression": self.expression,
            "rolls": list(self.rolls),
            "modifier": self.modifier,
            "total": self.total,
        }
        if purpose:
            out["purpose"] = purpose
        return out


_TURN_RNG: contextvars.ContextVar[random.Random | None] = contextvars.ContextVar(
    "edgar_turn_rng", default=None
)


@contextlib.contextmanager
def use_rng(rng: random.Random) -> Iterator[random.Random]:
    """Route every `roll()` in this context (that has no explicit `rng=`) through `rng`.

    Use `with use_rng(random.Random(seed)):` to make a whole turn reproducible.
    """
    token = _TURN_RNG.set(rng)
    try:
        yield rng
    finally:
        _TURN_RNG.reset(token)


def current_rng() -> random.Random | None:
    """The turn-scoped RNG installed by `use_rng`, if any."""
    return _TURN_RNG.get()


def parse_dice(expression: str) -> ParsedDice:
    """Parse `NdM` or `NdM+K` / `NdM-K`. Raises ValueError with a hint on invalid input."""
    expression = expression.strip()
    match = _DICE_PATTERN.fullmatch(expression)
    if not match:
        raise ValueError(
            f"Invalid dice expression: {expression!r}. Use format NdM or NdM+K (e.g. 2d6+3)."
        )

    count = int(match.group(1)) if match.group(1) else 1
    sides = int(match.group(2))
    modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0

    if not 1 <= count <= MAX_DICE:
        raise ValueError(f"Dice count must be 1-{MAX_DICE}, got {count}")
    if not MIN_SIDES <= sides <= MAX_SIDES:
        raise ValueError(f"Dice sides must be {MIN_SIDES}-{MAX_SIDES}, got {sides}")

    return ParsedDice(count=count, sides=sides, modifier=modifier)


def _roll_dice(count: int, sides: int, rng: random.Random | None = None) -> list[int]:
    """Default RNG is SystemRandom (cryptographic) so production rolls are not predictable."""
    if rng is None:
        rng = _TURN_RNG.get() or random.SystemRandom()
    return [rng.randint(1, sides) for _ in range(count)]


def roll(expression: str, rng: random.Random | None = None) -> DiceOutcome:
    parsed = parse_dice(expression)
    rolls = _roll_dice(parsed.count, parsed.sides, rng)
    return DiceOutcome(
        expression=expression,
        total=sum(rolls) + parsed.modifier,
        rolls=rolls,
        modifier=parsed.modifier,
        count=parsed.count,
        sides=parsed.sides,
    )


def try_roll(expression: str | None, rng: random.Random | None = None) -> DiceOutcome | None:
    """Like `roll`, but returns None for a missing/invalid expression instead of raising.

    Used wherever an LLM-proposed expression is rolled: a bad expression drops the roll rather
    than failing the turn.
    """
    if not expression or not isinstance(expression, str):
        return None
    try:
        return roll(expression, rng)
    except ValueError:
        return None
