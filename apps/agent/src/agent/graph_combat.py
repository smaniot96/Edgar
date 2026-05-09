"""Combat subgraph: initiative, turn loop, exit condition."""

from langchain_core.messages import SystemMessage, HumanMessage

from tools import roll

from agent.llm import make_chat_model
from agent.state import AgentState
from agent.nodes import (
    input_parser_node,
    world_retriever_node,
    rules_adjudicator_node,
    world_state_updater_node,
    narrator_node,
)

MAX_COMBAT_ROUNDS = 10
ENEMY_ACTION_PROMPT = """Generate a single short D&D combat action for {actor} (e.g. "The goblin attacks with its scimitar"). One sentence only."""


async def combat_initiative_node(state: AgentState) -> dict:
    """Roll initiative and set turn order. Runs when combat_state is empty."""
    combat_state = state.get("combat_state") or {}
    if combat_state.get("initiative_order"):
        return {}  # Already have initiative

    parsed_input = state.get("parsed_input")
    entities = parsed_input.entities if parsed_input else {}
    targets = entities.get("targets", entities.get("target", "enemy"))
    if isinstance(targets, str):
        targets = [targets]

    # Roll initiative: player + each enemy
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
    """Get input for current turn: player_input for player, LLM-generated for enemies."""
    combat_state = state.get("combat_state") or {}
    initiative_order = combat_state.get("initiative_order", [])
    current = combat_state.get("current_turn_index", 0)
    actor = initiative_order[current] if current < len(initiative_order) else "unknown"

    if actor == "player":
        return state.get("player_input", "I wait.")

    # Generate enemy action
    llm = make_chat_model(temperature=0.7)
    response = await llm.ainvoke([
        SystemMessage(content=ENEMY_ACTION_PROMPT.format(actor=actor)),
        HumanMessage(content="Generate the action."),
    ])
    return response.content if hasattr(response, "content") else str(response)


async def combat_turn_node(state: AgentState) -> dict:
    """Process one combat turn: parser -> retriever -> adjudicator -> updater -> narrator."""
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

    # Advance turn
    combat_state = merged.get("combat_state") or {}
    initiative_order = combat_state.get("initiative_order", [])
    current = combat_state.get("current_turn_index", 0)
    round_num = combat_state.get("round", 1)

    next_index = (current + 1) % len(initiative_order) if initiative_order else 0
    next_round = round_num + 1 if next_index == 0 else round_num
    ended = next_round > MAX_COMBAT_ROUNDS

    adjudication = merged.get("adjudication_result")
    if adjudication and "combat ends" in (adjudication.mechanical_summary or "").lower():
        ended = True

    combat_state = {
        **combat_state,
        "current_turn_index": next_index,
        "round": next_round if not ended else round_num,
        "ended": ended,
    }

    return {
        "combat_state": combat_state,
        "messages": list(merged.get("messages", [])),
    }


def build_combat_subgraph():
    """Build and compile the combat subgraph. One turn per invocation; loop is via main app."""
    from langgraph.graph import StateGraph, START, END

    combat_graph = StateGraph(AgentState)
    combat_graph.add_node("initiative", combat_initiative_node)
    combat_graph.add_node("turn", combat_turn_node)

    combat_graph.add_edge(START, "initiative")
    combat_graph.add_edge("initiative", "turn")
    combat_graph.add_edge("turn", END)

    return combat_graph.compile()
