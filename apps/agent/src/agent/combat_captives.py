"""The engine's ruling for a captive named outside a fight (EDG-146, EDG-057 split).

Deterministic: no dice, no round and no model narration. Only the captive's own record and the
player's words decide it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from agent.captives import CaptiveTurn, captive_turn, is_attack_text
from agent.combat_intent import _named_targets
from agent.models.adjudication import AdjudicationResult
from agent.resolution import normalise_check
from agent.state import AgentState


def captive_turn_for_state(state: AgentState) -> CaptiveTurn | None:
    """EDG-146: the engine ruling for a captive the player names outside an active fight."""
    combat = state.get("combat_state") or {}
    if combat.get("combatants") and not combat.get("ended"):
        return None
    captives = [
        c
        for c in state.get("combat_captives") or []
        if isinstance(c, dict) and c.get("name") and c.get("combat_id") is not None
    ]
    if not captives:
        return None
    parsed = state.get("parsed_input")
    norm = normalise_check(getattr(parsed, "check", None)) if parsed is not None else None
    attack_intent = bool(
        (norm is not None and norm[1] in ("attack", "spell_attack"))
        or getattr(parsed, "intent", None) == "combat"
        or is_attack_text(state.get("player_input"))
    )
    return captive_turn(
        state.get("player_input", ""), _named_targets(state), captives, attack_intent=attack_intent
    )


def _captive_result(state: AgentState, turn: CaptiveTurn) -> dict:
    """A deterministic engine ruling: no dice, no round, no model narration."""
    adj = AdjudicationResult(success=True, mechanical_summary=turn.text, completion_status="none")
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=turn.text))
    out: dict[str, Any] = {
        "narration": turn.text,
        "adjudication_result": adj,
        "messages": messages,
        "source_status": state.get("source_status", []),
        "retrieval_degraded": bool(state.get("retrieval_degraded")),
    }
    if turn.outcome:
        out["captive_outcome"] = turn.outcome
    return out
