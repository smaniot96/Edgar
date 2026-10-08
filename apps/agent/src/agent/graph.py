"""LangGraph topology for the DM agent.

Linear path: input_parser -> world_retriever -> rules_adjudicator -> world_state_updater
            -> narrator -> END.

Combat path: input_parser -> combat (subgraph, narrates the round itself) -> END.

The conditional edge after `input_parser` chooses between the two based on intent. Any node
that sets `error` short-circuits to END, so no further LLM calls run and the first error is
the one the API sees.

Two compiled graphs share this topology:
  * `app` runs the whole turn (sync endpoint).
  * `prepare_app` stops before the linear-path narrator, so the SSE runner can stream the
    narrator itself while still parsing the input exactly once.

The rolling memory summary is *not* a graph node: the API updates it after the turn commits
(see `apps.api.services.session_messages.maybe_update_memory_summary`).
"""

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from agent.graph_combat import build_combat_subgraph
from agent.nodes import (
    input_parser_node,
    narrator_node,
    rules_adjudicator_node,
    world_retriever_node,
    world_state_updater_node,
)
from agent.state import AgentState


def route_after_input_parser(state: AgentState) -> str:
    if state.get("error"):
        return "end"
    # Stay in combat until the encounter is resolved, regardless of how this turn's intent was
    # classified — otherwise a non-"combat" line mid-fight would stall the encounter (enemies
    # never act, HP never updates).
    active_combat = state.get("combat_state")
    if active_combat and not active_combat.get("ended"):
        return "combat"
    parsed = state.get("parsed_input")
    if parsed and parsed.intent == "combat":
        return "combat"
    return "normal"


def _next_unless_error(next_node: str) -> Callable[[AgentState], str]:
    def _route(state: AgentState) -> str:
        return "end" if state.get("error") else "next"

    _route.__name__ = f"route_to_{next_node}"
    return _route


def build_graph(*, include_narrator: bool = True):
    """Compile the turn graph; `include_narrator=False` ends the linear path before narration."""
    graph = StateGraph(AgentState)
    graph.add_node("input_parser", input_parser_node)
    graph.add_node("combat", build_combat_subgraph())
    graph.add_node("world_retriever", world_retriever_node)
    graph.add_node("rules_adjudicator", rules_adjudicator_node)
    graph.add_node("world_state_updater", world_state_updater_node)

    graph.add_edge(START, "input_parser")
    graph.add_conditional_edges(
        "input_parser",
        route_after_input_parser,
        {"combat": "combat", "normal": "world_retriever", "end": END},
    )
    graph.add_edge("combat", END)

    chain = ["world_retriever", "rules_adjudicator", "world_state_updater"]
    if include_narrator:
        graph.add_node("narrator", narrator_node)
        chain.append("narrator")
    for node, next_node in zip(chain, chain[1:], strict=False):
        graph.add_conditional_edges(
            node, _next_unless_error(next_node), {"next": next_node, "end": END}
        )
    graph.add_edge(chain[-1], END)
    return graph.compile()


app = build_graph()
prepare_app = build_graph(include_narrator=False)
