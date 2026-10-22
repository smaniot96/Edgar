"""Shared TypedDict for the agent graph.

Every key is optional (`total=False`); each node only writes the keys it owns and reads the
keys it needs. The API populates the inputs (player_input, session_id, campaign_id, messages,
character, world_flags, etc.) before invoking the graph; the API also persists side effects
after the graph returns (see services.world_writes and services.combat).

The record TypedDicts below name the shapes that nodes and the API agree on. They are typing
only: LangGraph reads the annotations for channels, and the runtime dicts are unchanged.
"""

from typing import Any, TypedDict

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput

# `class` is a Python keyword, so this record uses the functional form.
CharacterState = TypedDict(
    "CharacterState",
    {
        "id": int,
        "name": str,
        "class": str,
        "level": int,
        "hp_current": int,
        "hp_max": int,
        "stats": dict[str, Any],
        "inventory": list[Any] | dict[str, Any],
    },
    total=False,
)


class NpcRecord(TypedDict, total=False):
    """A roster NPC loaded from the DB; `disposition` decides whether it is an ally or enemy."""

    name: str
    disposition: str


class CombatantRecord(TypedDict, total=False):
    """One persisted combatant row. Public projections are in apps.api.schemas.stream."""

    id: str
    name: str
    display_name: str | None
    is_player: bool
    character_id: int  # EDG-049: the hero a party-fight row belongs to
    fled: bool  # EDG-049: the hero left the fight
    creature_id: str | None
    hp_current: int
    hp_max: int
    ac: int | None
    init: int
    init_bonus: int
    alive: bool
    disposition: str
    reaction_used: bool
    source_reference: Any  # private engine binding; never player-visible


class CombatOutcomeRecord(TypedDict, total=False):
    """An ended encounter as the session stores it (`load_resolved_combat_outcomes`)."""

    id: int
    outcome: str | None
    round: int | None
    combatants: list[CombatantRecord]


class CombatStateRecord(TypedDict, total=False):
    """The persisted encounter (`persist_combat` / `combat_state_public` input)."""

    id: int
    round: int
    ended: bool
    outcome: str | None
    combatants: list[CombatantRecord]
    initiative: list[dict[str, Any]]
    initiative_order: list[str]
    current_turn_index: int | None
    current_turn: str | None
    current_turn_character_id: int | None  # EDG-049: whose turn it is in a party fight
    player_ac: int | None
    second_wind_used: bool
    second_wind_uses_remaining: int | None
    solo_budget: dict[str, Any] | None
    economy: dict[str, Any] | None
    raging: bool


class TelemetryEvent(TypedDict):
    """A row the API writes in the turn's transaction (`event_log`: type and payload)."""

    event_type: str
    payload: dict[str, Any]


class AgentState(TypedDict, total=False):
    parser_scene_context: dict  # Per-turn verified scene selection reused by retrieval; private
    # Inputs from the API
    story_reveal_context: list  # Exact reviewed self-gated player spans; validator-only
    story_arrival_context: list  # Exact reviewed player arrival spans of a move's destination; validator-only
    story_state: dict  # Serialized edgar_core.story.StoryState; committed session facts only
    earned_observations: list[dict]  # Committed accepted-answer receipts, separate from narration
    prepared_campaign_overlays: dict[
        str, dict
    ]  # Retained historical additions; live play does not author prepared amendments
    prepared_amendment_events: list[dict]  # Retained event snapshot used for stale-write checks
    exact_recall: dict  # Exact text/source from one committed public clue, narrator-only
    policy_rejection: bool  # Safe generic rejection for unavailable reviewed actions
    policy_narration: str  # Deterministic narration for an engine-resolved reviewed policy
    authorized_policy_effect_keys: list[str]  # Validator-only policy consequence receipts
    engine_roll_mode: str  # Advantage/disadvantage the engine applied this turn ("normal" if none)
    policy_reveal_authorizations: list[dict]  # Exact policy-authorized player reveal passages
    turn_id: str  # Server-generated per-turn provenance identity
    turn_snapshot: dict  # Initial position/active character used for atomic stale-state checks
    player_input: str
    session_id: int
    campaign_id: int
    messages: list  # Uncovered narration events after memory_summary coverage, oldest first
    character: (
        CharacterState | None
    )  # {id, name, class, level, hp_current, hp_max, stats, inventory}
    party: list[CharacterState]  # Every seated PC, acting character first (EDG-046); roster only
    adventure_collections: list[str]  # campaigns.adventure_collections
    world_flags: dict[str, str]
    current_scene_id: str | None
    rules_edition: str
    progression_mode: str  # EDG-045: 'fixed' or 'milestone' (agent.progression)
    death_mode: str  # EDG-045: 'subdued' or 'death_saves' (agent.progression)
    combat_always_confirm: (
        bool  # EDG-182: the campaign confirms every combat turn that declares an action
    )
    adventure_manifests: dict[str, dict]  # validated manifests from existing adventure metadata
    npcs: list[NpcRecord]  # loaded from DB; used to keep allies out of enemy rosters
    resolved_combat_outcomes: list[CombatOutcomeRecord]  # Session-owned authoritative ended rows
    combat_state: CombatStateRecord  # loaded from DB if an encounter is in progress
    combat_captives: list[dict]  # Combat-owned captive/released/dead NPC rows (EDG-146)
    party_updates: list[dict]  # EDG-049: sheet changes of the other heroes a party fight round made
    combat_skip: bool  # EDG-049: the turn is the default Dodge of an idle hero, not a typed action
    captive_outcome: dict  # Engine-decided release/kill of a captive, persisted with the turn
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
    # WorldStateUpdater: event_log rows the API writes in the turn's single transaction (nodes
    # never commit on their own).
    telemetry_events: list[TelemetryEvent]

    # EDG-079: set by the SSE runner. A combat round finalises its mechanics and returns
    # `pending_round_narration` instead of calling the narrator; the runner streams that call.
    defer_narration: bool
    # graph_combat.PendingRoundNarration. Kept as Any: the class lives in graph_combat, which
    # imports this module, and LangGraph resolves these annotations at runtime.
    pending_round_narration: Any  # never persisted

    # Test injection
    qdrant_client: Any  # tests inject a fake; production uses get_qdrant_client()

    # Set by any node on failure; the API turns this into a 503.
    error: str
