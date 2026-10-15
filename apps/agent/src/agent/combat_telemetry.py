"""Developer evidence for one combat round (EDG-031).

`combat_round_node` stages one `combat_round` telemetry row per resolved round. The API writes
it with the rest of the turn (`persist_turn`), so it commits or rolls back with the turn and a
retry of the same turn cannot duplicate it.

The row is a whitelist built here, never a dump of the combatant dicts: the roster's
`source_reference` and `creature_id` stay out of every stored and streamed developer payload.
Stage timings are included only when the round actually measured them. A stage that did not run
(for example no adjudication after a flee) is listed in `timings_unavailable`, never zeroed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

COMBAT_ROUND_EVENT = "combat_round"
# Stages the combat round measures itself. Parsing is timed by the SSE runner, not the round.
COMBAT_STAGES: tuple[str, ...] = ("retrieving_ms", "adjudicating_ms", "narrating_ms")
_PAYLOAD_VERSION = 1


def combat_round_event(
    *,
    turn_id: str | None,
    round_num: int,
    ended: bool,
    outcome: str | None,
    actors: list[dict[str, Any]],
    player_action: dict[str, Any],
    outcomes: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    dice_log: Iterable[Mapping[str, Any]],
    stage_ms: Mapping[str, int],
) -> dict[str, Any]:
    """The telemetry row (`{event_type, payload}`) for a resolved round."""
    measured = {k: int(stage_ms[k]) for k in COMBAT_STAGES if stage_ms.get(k) is not None}
    return {
        "event_type": COMBAT_ROUND_EVENT,
        "payload": {
            "version": _PAYLOAD_VERSION,
            "turn_id": turn_id,
            "round": round_num,
            "ended": ended,
            "outcome": outcome,
            # Enemy attackers this round: stable encounter ids (unique per roster), then names.
            "actors": actors,
            "player_action": player_action,
            # Enemy attack resolutions in initiative order. The player's own action is above.
            "outcomes": outcomes,
            # Every combatant's disposition after the round (defeat and capture live here).
            "dispositions": dispositions,
            # The engine's original rolls, exactly as adjudicated.
            "dice_log": [dict(d) for d in dice_log],
            "timings_ms": measured,
            "timings_unavailable": [k for k in COMBAT_STAGES if k not in measured],
        },
    }


def combat_round_public(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Developer view of a stored round. Rows written by this module need no further filtering."""
    if not payload:
        return None
    return dict(payload)
