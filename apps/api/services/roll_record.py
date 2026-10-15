"""Player-facing roll record for one turn, stored when the turn commits (EDG-030).

`persist_turn` builds the record from the turn's own adjudication and writes it onto the turn's
narration row, so reload returns exactly the ruling and dice the live turn showed. Nothing here
rolls dice: the stored numbers are the engine's original ones, copied from the adjudication.

The record is a whitelist. Citations, story effects, flags, scene ids and source references never
enter it, so it is safe for the player-mode history. Developer detail stays in the debug stream.

Statuses stored on narration rows (`roll_status`):
  captured   the turn rolled dice; `roll` holds the record
  no_roll    the turn was committed with no dice (for example a clarifying question)
  not_captured  no status on the row: a legacy row written before this record existed; never inferred
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent.models.adjudication import AdjudicationResult

ROLL_CAPTURED = "captured"
ROLL_NO_ROLL = "no_roll"
ROLL_NOT_CAPTURED = "not_captured"

_DICE_KEYS = ("expression", "rolls", "modifier", "total", "purpose", "roll_mode", "kept")


def _positive(value: int | None) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def build_roll_record(
    adjudication: AdjudicationResult | None, turn_id: str | None
) -> dict[str, Any] | None:
    """The stored roll for a turn, or None when the turn rolled no dice."""
    if adjudication is None:
        return None
    dice_log = list(adjudication.dice_log or [])
    if adjudication.dice_result is None and not dice_log:
        return None
    update = adjudication.character_update
    return {
        "turn_id": turn_id,
        "check": adjudication.check,
        "success": bool(adjudication.success),
        "dc": adjudication.dc,
        "against": adjudication.against,
        "modifier": adjudication.modifier,
        "roll_expression": adjudication.roll_expression,
        "natural_roll": adjudication.natural_roll,
        "dice_result": adjudication.dice_result,
        "damage": _positive(adjudication.damage),
        # Healing is the applied hp gain on this turn, never an unrolled proposal.
        "healing": _positive(update.hp_delta) if update is not None else None,
        "conditions": [c for c in adjudication.conditions or [] if c],
        "mechanical_summary": adjudication.mechanical_summary or "",
        "dice_log": [{key: d.get(key) for key in _DICE_KEYS} for d in dice_log],
    }


def roll_status_for(roll: Mapping[str, Any] | None) -> str:
    return ROLL_CAPTURED if roll is not None else ROLL_NO_ROLL


def stored_roll(payload: Mapping[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Read a narration row's stored roll. Rows without a status are legacy: not captured."""
    if not payload or "roll_status" not in payload:
        return ROLL_NOT_CAPTURED, None
    status = payload.get("roll_status")
    if status == ROLL_NO_ROLL:
        return ROLL_NO_ROLL, None
    roll = payload.get("roll")
    if status == ROLL_CAPTURED and isinstance(roll, dict):
        return ROLL_CAPTURED, roll
    return ROLL_NOT_CAPTURED, None
