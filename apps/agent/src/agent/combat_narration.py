"""Finalising a combat round's narration, shared by the sync node and the stream (EDG-079, EDG-057).

The mechanics of a round are decided before `PendingRoundNarration` exists. `finalize_round_narration`
is the one place that turns the narrator's text into the round's persisted narration and history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from agent.combat_telemetry import combat_round_event
from agent.combatants import display_name, roster_violations
from agent.models.adjudication import AdjudicationResult
from agent.nodes.narrator import narration_profile
from agent.supported_rules import (
    actions_for_state,
    apply_narration_boundary,
    engine_applied_for_state,
)

# Same log channel as before the EDG-057 split, so existing log filters keep matching.
log = logging.getLogger("agent.graph_combat")


PENDING_ROUND_NARRATION_KEY = "pending_round_narration"


@dataclass(frozen=True)
class PendingRoundNarration:
    """A combat round whose mechanics are final and whose one narrator call has not run (EDG-079).

    Dice, HP, dispositions, the adjudication, completion and the round's telemetry inputs are all
    decided before this exists. `narr_state` is the exact state the narrator prompt and the
    boundary are built from, so the streamed call and the sync call see identical input.
    """

    narr_state: dict[str, Any]
    combatants: list[dict]
    adjudication: AdjudicationResult
    outcome: str | None
    base_update: dict[str, Any]  # round state to persist, without narration/messages/telemetry
    round_event: dict[str, Any]  # combat_round_event kwargs, except the stage timings
    stage_ms: dict[str, int]  # stages measured before narration

    @property
    def profile(self) -> str:
        return narration_profile(self.narr_state)


def _roster_problem_labels(problems: list[str], combatants: list[dict]) -> list[str]:
    """Labels safe to log: roster display names only.

    `roster_violations` returns a foe's name for a revived foe, but for an invented attacker it
    returns the first two words of the narration itself. Those words are player-visible prose, so
    anything that is not a roster name is logged as a fixed label (EDG-063).
    """
    names = {display_name(c).lower() for c in combatants if not c.get("is_player")}
    return [p if p.lower() in names else "unnamed_attacker" for p in problems[:4]]


def finalize_round_narration(
    pending: PendingRoundNarration, narration: str, *, bound: bool, narrating_ms: int
) -> dict[str, Any]:
    """The one finalisation of a combat round's narration, shared by the sync node and the stream.

    `narration` is the complete narrator text (or the deterministic text when `bound` is False).
    Streamed tokens are provisional and never reach this function; the text decided here is the
    authoritative `done.narration`. It applies, in order:
      1. the narration boundary to model text (the engine owns effects: a claimed effect the
         engine did not apply is dropped, and the ruling stated),
      2. the roster check: prose that revives an inactive foe or invents an attacker is replaced
         by the engine's mechanical summary,
      3. the victory/defeat/fled suffix,
      4. the AIMessage history, and
      5. the round telemetry row, with the measured narrating stage.
    """
    text = narration
    if bound:
        text = apply_narration_boundary(
            text,
            actions_for_state(pending.narr_state),
            applied=engine_applied_for_state(pending.narr_state),
        )
    messages = [*(pending.narr_state.get("messages") or []), AIMessage(content=text)]
    problems = roster_violations(text, pending.combatants)
    if problems:
        # Prose that revives an inactive foe or invents an attacker is replaced by the engine's log.
        log.warning(
            "narration_roster_violation",
            extra={"problems": _roster_problem_labels(problems, pending.combatants)},
        )
        text = pending.adjudication.mechanical_summary or ""
        messages = [*messages[:-1], AIMessage(content=text)]
    party = sum(1 for c in pending.combatants if c.get("is_player")) >= 2
    if pending.outcome == "victory":
        text += "\n\n**Victory!** The way forward is clear."
    elif party and pending.outcome == "defeat":
        text += "\n\n**The party falls.** Every hero is down; the story is not over, but the party is at the enemy's mercy."
    elif party and pending.outcome == "fled":
        text += "\n\n*(The party has left the fight.)*"
    elif pending.outcome == "defeat" and any(
        c.get("is_player") and c.get("disposition") == "dead" for c in pending.combatants
    ):
        text += "\n\n**Dead.** Your hero has died. The story can continue with a new hero."
    elif pending.outcome == "defeat":
        text += "\n\n**Subdued.** You are overwhelmed and knocked unconscious — your story is not over, but you are at the enemy's mercy."
    elif pending.outcome == "fled":
        text += "\n\n*(You have left the fight.)*"

    stage_ms = {**pending.stage_ms, "narrating_ms": int(narrating_ms)}
    round_event = combat_round_event(**pending.round_event, stage_ms=stage_ms)
    return {
        **pending.base_update,
        "messages": messages,
        "narration": text,
        # Written in the turn transaction by persist_turn (EDG-031); never committed here.
        "telemetry_events": [round_event],
    }
