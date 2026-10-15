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
    story_reveal_context: list  # Exact reviewed self-gated player spans; validator-only
    story_state: dict  # Serialized edgar_core.story.StoryState; committed session facts only
    exact_recall: dict  # Exact text/source from one committed public clue, narrator-only
    policy_rejection: bool  # Safe generic rejection for unavailable reviewed actions
    policy_narration: str  # Deterministic narration for an engine-resolved reviewed policy
    authorized_policy_effect_keys: list[str]  # Validator-only policy consequence receipts
    policy_reveal_authorizations: list[dict]  # Exact policy-authorized player reveal passages
    turn_id: str  # Server-generated per-turn provenance identity
    turn_snapshot: dict  # Initial position/active character used for atomic stale-state checks
    player_input: str
    session_id: int
    campaign_id: int
    messages: list  # Uncovered narration events after memory_summary coverage, oldest first
    character: dict | None  # {id, name, class, level, hp_current, hp_max, stats, inventory}
    adventure_collections: list[str]  # campaigns.adventure_collections
    world_flags: dict[str, str]
    current_scene_id: str | None
    rules_edition: str
    adventure_manifests: dict[str, dict]  # validated manifests from existing adventure metadata
    npcs: list  # [{name, disposition}] loaded from DB; used to keep allies out of enemy rosters
    resolved_combat_outcomes: list[dict]  # Session-owned authoritative ended combat rows
    combat_state: dict  # loaded from DB if an encounter is in progress
    memory_summary: str | None  # rolling story summary immediately before `messages`

    # Produced by nodes
    parsed_input: ParsedInput  # InputParser
    rules_context: list  # WorldRetriever (rules bucket)
    rule_queries: list[dict]  # independent mechanics/reference retrieval plan
    adventure_context: list  # WorldRetriever (adventure bucket)
    planner_context: list  # relevant reviewed private passages; never narrator/player context
    allowed_scene_ids: list[str]  # current scene and prerequisite-unlocked branch destinations
    discovery_entities: list[dict]  # canonical IDs and their existing WorldFlag discovery keys
    manifest_status: str  # reviewed | review_required | homebrew
    retrieved_context: list  # WorldRetriever (legacy merged view; nodes prefer the split)
    error_code: str
    source_mode: str
    source_status: list[dict[str, Any]]
    retrieval_degraded: bool  # WorldRetriever: a bucket failed and came back empty
    adjudication_result: AdjudicationResult  # RulesAdjudicator
    narration: str  # Narrator
    # WorldStateUpdater: event_log rows ({event_type, payload}) the API writes in the turn's
    # single transaction (nodes never commit on their own).
    telemetry_events: list[dict]

    # Test injection
    qdrant_client: Any  # tests inject a fake; production uses get_qdrant_client()

    # Set by any node on failure; the API turns this into a 503.
    error: str
