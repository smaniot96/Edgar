"""Main LangGraph state machine for the DM agent."""

from langgraph.graph import StateGraph, START, END

from agent.state import AgentState
from agent.nodes import (
    input_parser_node,
    world_retriever_node,
    rules_adjudicator_node,
    world_state_updater_node,
    narrator_node,
    memory_summarizer_node,
)

graph = StateGraph(AgentState)
graph.add_node("input_parser", input_parser_node)
graph.add_node("world_retriever", world_retriever_node)
graph.add_node("rules_adjudicator", rules_adjudicator_node)
graph.add_node("world_state_updater", world_state_updater_node)
graph.add_node("narrator", narrator_node)
graph.add_node("memory_summarizer", memory_summarizer_node)

graph.add_edge(START, "input_parser")
graph.add_edge("input_parser", "world_retriever")
graph.add_edge("world_retriever", "rules_adjudicator")
graph.add_edge("rules_adjudicator", "world_state_updater")
graph.add_edge("world_state_updater", "narrator")
graph.add_edge("narrator", "memory_summarizer")
graph.add_edge("memory_summarizer", END)

app = graph.compile()
