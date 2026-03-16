"""Agent state schema for the LangGraph flow."""

from typing import TypedDict

from agent.models.parsed_input import ParsedInput
from agent.models.adjudication import AdjudicationResult


class AgentState(TypedDict, total=False):
    """State passed through the graph. Keys are add-only."""

    messages: list
    player_input: str
    parsed_input: ParsedInput
    retrieved_context: list
    adjudication_result: AdjudicationResult
    session_id: int
    campaign_id: int
    combat_state: dict
    narration: str
    error: str
