"""Answer the owner-rule confirmation (EDG-182) in tests that run a combat round.

A multi-part or conditional turn is repeated back before any die. `confirm_turn` replies "yes" to
that question with the same state (the pending declaration and the rest of the encounter), so the
round's dice and outcome are asserted as before. Any other output is returned unchanged.
"""

from __future__ import annotations

from typing import Any

CONFIRMATION_OPENER = "Confirm before any dice"


async def confirm_turn(out: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if not str(out.get("narration") or "").startswith(CONFIRMATION_OPENER):
        return out
    from agent.graph_combat import combat_round_node
    from agent.models.parsed_input import ParsedInput

    follow = {
        **state,
        "player_input": "yes",
        "parsed_input": ParsedInput(intent="combat"),
        "combat_state": out["combat_state"],
    }
    return await combat_round_node(follow)
