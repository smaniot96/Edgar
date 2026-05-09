"""Agent state schema for the LangGraph flow."""

from typing import Any, TypedDict

from agent.models.parsed_input import ParsedInput
from agent.models.adjudication import AdjudicationResult


class AgentState(TypedDict, total=False):
    """State passed through the graph. Keys are add-only."""

    messages: list
    player_input: str
    parsed_input: ParsedInput
    retrieved_context: list
    rules_context: list
    adventure_context: list
    adjudication_result: AdjudicationResult
    session_id: int
    campaign_id: int
    adventure_collections: list[str]
    world_flags: dict[str, str]
    current_scene_id: str | None
    character: dict | None
    combat_state: dict
    narration: str
    error: str
    state_updates_applied: bool  # reserved; API applies writes outside the graph
    qdrant_client: Any  # optional QdrantClient; tests inject a fake client
