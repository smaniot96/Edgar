"""Combat subgraph: initiative on first entry, then one round-robin turn per main-graph invocation.

The subgraph is invoked once per `app.ainvoke` from the main graph; the multi-turn loop is
driven by repeated player turns through the API. `combat_state.id` is preserved across turns
by spreading the existing dict, so `services.combat.persist_combat` updates the same row.
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from tools import roll

from agent.llm import make_chat_model
from agent.nodes import (
    input_parser_node,
    narrator_node,
    rules_adjudicator_node,
    world_retriever_node,
    world_state_updater_node,
)
from agent.state import AgentState

# Hard cap so a runaway encounter does not loop forever; the LLM can also end combat earlier
# by writing "combat ends" into mechanical_summary (see below).
MAX_COMBAT_ROUNDS = 10
ENEMY_ACTION_PROMPT = (
    'Generate a single short D&D combat action for {actor} '
    '(e.g. "The goblin attacks with its scimitar"). One sentence only.'
)


async def combat_initiative_node(state: AgentState) -> dict:
    """Seed initiative on the first turn of an encounter; no-op once initiative exists.

    Targets come from ParsedInput.entities; we accept either `targets` (list) or a single `target`.
    Each participant gets a 1d20 (no DEX modifier yet; see plan 13 for replay-seeded RNG).
    """
    combat_state = state.get("combat_state") or {}
    if combat_state.get("initiative_order"):
        return {}

    parsed_input = state.get("parsed_input")
    entities = parsed_input.entities if parsed_input else {}
    targets = entities.get("targets", entities.get("target", "enemy"))
    if isinstance(targets, str):
        targets = [targets]

    participants = [{"name": "player", "initiative": roll("1d20").total}]
    for t in targets:
        participants.append({"name": str(t), "initiative": roll("1d20").total})

    participants.sort(key=lambda p: p["initiative"], reverse=True)
    initiative_order = [p["name"] for p in participants]

    return {
        "combat_state": {
            **combat_state,
            "initiative_order": initiative_order,
            "round": 1,
            "current_turn_index": 0,
            "ended": False,
        }
    }


async def _get_turn_input(state: AgentState) -> str:
    """Player's turn uses their typed message; enemy turns get a one-line LLM-generated action."""
    combat_state = state.get("combat_state") or {}
    initiative_order = combat_state.get("initiative_order", [])
    current = combat_state.get("current_turn_index", 0)
    actor = initiative_order[current] if current < len(initiative_order) else "unknown"

    if actor == "player":
        return state.get("player_input", "I wait.")

    llm = make_chat_model(temperature=0.7)
    response = await llm.ainvoke(
        [
            SystemMessage(content=ENEMY_ACTION_PROMPT.format(actor=actor)),
            HumanMessage(content="Generate the action."),
        ]
    )
    return response.content if hasattr(response, "content") else str(response)


async def combat_turn_node(state: AgentState) -> dict:
    """Run one combat actor's turn through the same node sequence as the linear path.

    Stops early on the first node that returns `{"error": ...}` so the caller can surface it.
    """
    turn_input = await _get_turn_input(state)
    merged = dict(state)
    merged["player_input"] = turn_input

    for node in (
        input_parser_node,
        world_retriever_node,
        rules_adjudicator_node,
        world_state_updater_node,
        narrator_node,
    ):
        updates = await node(merged)
        if updates.get("error"):
            return updates
        merged.update(updates)

    combat_state = merged.get("combat_state") or {}
    initiative_order = combat_state.get("initiative_order", [])
    current = combat_state.get("current_turn_index", 0)
    round_num = combat_state.get("round", 1)

    next_index = (current + 1) % len(initiative_order) if initiative_order else 0
    next_round = round_num + 1 if next_index == 0 else round_num
    ended = next_round > MAX_COMBAT_ROUNDS

    # Allow the adjudicator to end combat narratively (e.g. all enemies down).
    adjudication = merged.get("adjudication_result")
    if adjudication and "combat ends" in (adjudication.mechanical_summary or "").lower():
        ended = True

    combat_state = {
        **combat_state,
        "current_turn_index": next_index,
        "round": next_round if not ended else round_num,
        "ended": ended,
    }

    # Narrator and adjudicator run inside this node (not as graph nodes), so their outputs
    # exist only on `merged` unless we copy them here. Without this, combat turns produce
    # empty narration in SSE/`done` and skip `apply_adjudication` in the API.
    out: dict[str, Any] = {
        "combat_state": combat_state,
        "messages": list(merged.get("messages", [])),
        "narration": merged.get("narration", ""),
    }
    adj = merged.get("adjudication_result")
    if adj is not None:
        out["adjudication_result"] = adj
    return out


def build_combat_subgraph():
    """Compile the two-node subgraph (initiative -> turn -> END). One actor per invocation."""
    from langgraph.graph import END, START, StateGraph

    combat_graph = StateGraph(AgentState)
    combat_graph.add_node("initiative", combat_initiative_node)
    combat_graph.add_node("turn", combat_turn_node)

    combat_graph.add_edge(START, "initiative")
    combat_graph.add_edge("initiative", "turn")
    combat_graph.add_edge("turn", END)

    return combat_graph.compile()
