"""Narrator node: produce immersive narration from adjudicated outcome."""

from agent.state import AgentState


def narrator_node(state: AgentState) -> dict:
    """Generate narration. TODO: Call LLM with adjudication + lore."""
    return {"narration": ""}
