"""Shared TypedDict for the agent graph.

Every key is optional (`total=False`); each node only writes the keys it owns and reads the
keys it needs. The API populates the inputs (player_input, session_id, campaign_id, messages,
character, world_flags, etc.) before invoking the graph; the API also persists side effects
after the graph returns (see services.world_writes and services.combat).
"""

from typing import Any, TypedDict

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput


class AgentState(TypedDict, total=False):
    # Inputs from the API
    player_input: str
    session_id: int
    campaign_id: int
    messages: list  # langchain BaseMessage list, oldest first; rebuilt per turn from event_log
    character: dict | None  # {id, name, class, level, hp_current, hp_max, stats, inventory}
    adventure_collections: list[str]  # campaigns.adventure_collections
    world_flags: dict[str, str]
    current_scene_id: str | None
    npcs: list  # [{name, disposition}] loaded from DB; used to keep allies out of enemy rosters
    combat_state: dict  # loaded from DB if an encounter is in progress

    # Produced by nodes
    parsed_input: ParsedInput  # InputParser
    rules_context: list  # WorldRetriever (rules bucket)
    adventure_context: list  # WorldRetriever (adventure bucket)
    retrieved_context: list  # WorldRetriever (legacy merged view; nodes prefer the split)
    adjudication_result: AdjudicationResult  # RulesAdjudicator
    narration: str  # Narrator

    # Reserved / advisory
    state_updates_applied: bool  # written by API after apply_adjudication
    qdrant_client: Any  # tests inject a fake; production uses get_qdrant_client()

    # Set by any node on failure; the API turns this into a 503.
    error: str
