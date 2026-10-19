"""Initiative order, its public view, and the engine's question to the player (EDG-057 split).

`initiative_view` is what the HUD reads. `_ask_player` answers without rolling dice and without
spending a round, so a refused or unclear action changes nothing.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage

from agent.combatants import view_fields
from agent.models.adjudication import AdjudicationResult
from agent.state import AgentState


def sort_by_initiative(combatants: list[dict]) -> list[dict]:
    return sorted(
        combatants,
        key=lambda c: (
            int(c.get("init", 0)),
            int(c.get("init_bonus", 0)),
            bool(c.get("is_player")),
            # EDG-049: heroes tied on every key go in character order; solo rows carry no id (0).
            -int(c.get("character_id") or 0),
        ),
        reverse=True,
    )


def _display(c: dict) -> str:
    return c.get("display_name") or c.get("name", "?")


def initiative_view(combatants: list[dict]) -> list[dict]:
    return [
        {
            "name": _display(c),
            "init": c.get("init"),
            "is_player": bool(c.get("is_player")),
            "hp": c.get("hp_current"),
            "hp_max": c.get("hp_max"),
            "ac": c.get("ac"),
            # Additive (EDG-049): which hero a party row is (absent on foes and solo rows).
            **({"character_id": c["character_id"]} if c.get("character_id") is not None else {}),
            **view_fields(c),
        }
        for c in sort_by_initiative(combatants)
    ]


def _ask_player(state: AgentState, question: str, *, combat_state: dict | None = None) -> dict:
    """No dice rolled, no round consumed: ask the player to clarify their target."""
    adj = AdjudicationResult(success=True, mechanical_summary=question, completion_status="none")
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=question))
    out: dict[str, Any] = {"narration": question, "adjudication_result": adj, "messages": messages}
    out["source_status"] = state.get("source_status", [])
    out["retrieval_degraded"] = bool(state.get("retrieval_degraded"))
    if combat_state:
        combat_state = dict(combat_state)
        combat_state["initiative"] = initiative_view(combat_state.get("combatants") or [])
        out["combat_state"] = combat_state
    return out
