"""Engine dice for d20 rolls with advantage or disadvantage (EDG-021, EDG-057 split from graph_combat).

Enemy and player rolls share these helpers. The engine rolls every die through `tools.dice`; no model
output chooses a die or a number.
"""

from __future__ import annotations

from tools.dice import DiceOutcome, roll


def _roll_with_mode(expression: str, mode: str) -> tuple[DiceOutcome, list[int]]:
    """A d20 expression at `mode`: returns the kept roll and both natural d20 values."""
    first = roll(expression)
    if mode == "normal":
        return first, [first.rolls[0]]
    second = roll(expression)
    pick = max if mode == "advantage" else min
    kept = pick(first, second, key=lambda outcome: outcome.rolls[0])
    return kept, [first.rolls[0], second.rolls[0]]


def _mode_log(outcome: DiceOutcome, purpose: str, mode: str, draws: list[int]) -> dict:
    entry = outcome.to_log(purpose)
    if mode == "normal":
        return entry
    return {**entry, "rolls": list(draws), "kept": outcome.rolls[0], "roll_mode": mode}


def _contest(
    attacker_bonus: int, defender_bonus: int, *, attacker_mode: str = "normal"
) -> tuple[bool, DiceOutcome, DiceOutcome, list[int]]:
    """A contested check: the higher total wins, so a tie leaves the attacker without the effect."""
    mine, draws = _roll_with_mode(f"1d20{attacker_bonus:+d}", attacker_mode)
    theirs = roll(f"1d20{defender_bonus:+d}")
    return mine.total > theirs.total, mine, theirs, draws
