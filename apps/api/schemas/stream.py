"""The turn SSE protocol as one typed contract (EDG-058).

Each frame the turn runner emits has a model here, and the public combatant, combat-state and
outcome projections are typed alongside them. The models VALIDATE; they never serialise. The
runner still writes the same dict it always has, so the bytes on the wire are unchanged and
the golden tests in `tests/api/test_stream_frames.py` keep that promise.

The committed JSON Schema (`frontend/src/api/stream.schema.json`) is rendered from these models
by `render_stream_schema`. `tests/api/test_stream_schema.py` fails when the file is stale, and the
frontend contract test fails when `turnStream.ts` or `api/types.ts` disagrees with it.

Models forbid undeclared keys on purpose: an added field on the wire is a protocol change that
must be declared here, in the schema and in the frontend, not silently tolerated. Frontends may
still ignore unknown fields (the TypeScript parser does).
"""

from __future__ import annotations

import json
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Pipeline stages the runner announces, in the order it announces them.
StreamStage = Literal["parsing", "retrieving", "adjudicating", "narrating", "saving"]
STREAM_STAGES: tuple[str, ...] = get_args(StreamStage)

OutcomeLabel = Literal["not_committed", "committed", "unknown"]
CombatOutcomeLabel = Literal["victory", "defeat", "fled", "surrendered"]

_STRICT = ConfigDict(extra="forbid")


class _Frame(BaseModel):
    model_config = _STRICT


# --- Public projections -------------------------------------------------------------------------


class GridPositionPublic(_Frame):
    """A combatant's square on the combat grid: 0-based x (east) and y (south), and its name ("D4")."""

    x: int
    y: int
    label: str


class TerrainPublic(_Frame):
    """One square of scenery: `wall` blocks movement, `difficult` costs double (agent.battlefield)."""

    x: int
    y: int
    kind: Literal["wall", "difficult"]
    feature: str


class BattlefieldPublic(_Frame):
    """The combat grid the squares are on (agent.battlefield.public_battlefield)."""

    width: int
    height: int
    square_ft: int
    theme: str
    terrain: list[TerrainPublic]


class CombatantPublic(_Frame):
    """One roster entry as the HUD and narrator may see it (`combat_state_public`)."""

    name: str | None
    is_player: bool
    hp_current: int | None
    hp_max: int | None
    ac: int | None
    id: str
    alive: bool
    disposition: str
    can_act: bool
    band: str | None
    reaction_used: bool
    # Additive (EDG-049), present on a hero of a party fight only.
    character_id: int | None = None
    fled: bool | None = None
    death_saves: dict[str, Any] | None = None
    # Additive (combat grid), present when the encounter has a battlefield.
    position: GridPositionPublic | None = None
    speed_ft: int | None = None


class EncounterParticipantPublic(_Frame):
    id: str
    name: str
    participation: Literal["active", "visible_nonparticipant"]
    reason: str


class CombatRoundEventPublic(_Frame):
    """An ordered, engine-written public event from the last resolved round."""

    kind: str
    text: str
    actor_id: str | None = None
    target_id: str | None = None
    spell: str | None = None
    damage: int | None = Field(default=None, ge=0)
    healing: int | None = Field(default=None, ge=0)
    hit: bool | None = None


class CombatStatePublic(_Frame):
    """The encounter view attached to `done` (`combat_state_public`)."""

    participants: list[EncounterParticipantPublic] | None = None
    round: int | None
    ended: bool
    outcome: CombatOutcomeLabel | None
    combatants: list[CombatantPublic]
    initiative: list[dict[str, Any]]
    current_turn_id: str | None
    current_turn: str | None
    current_turn_index: int | None
    player_ac: int | None
    second_wind_used: bool
    solo_budget: dict[str, Any] | None
    second_wind_uses_remaining: int | None
    economy: dict[str, Any] | None
    raging: bool
    concentration: str | None
    # Additive (EDG-182): a multi-part declaration waiting for the player's yes or no, if any.
    pending_declaration: dict[str, str] | None = None
    # Additive (EDG-182): the hero's standing reaction orders (foe and summary), if any.
    standing_orders: list[dict[str, str]] | None = None
    # Additive (EDG-049): a fight with two or more heroes, and whose turn it is (a character id).
    party: bool | None = None
    current_turn_character_id: int | None = None
    round_events: list[CombatRoundEventPublic] | None = None
    # Additive (combat grid): the battlefield, absent for an encounter opened before the grid.
    battlefield: BattlefieldPublic | None = None


class CombatOutcomeEntryPublic(_Frame):
    """A finished encounter as the player may see it (`resolved_combat_outcomes_public`)."""

    id: int
    outcome: CombatOutcomeLabel | None
    round: int | None
    combatants: list[dict[str, Any]]


class UsesPublic(_Frame):
    """A resource counted in uses: Second Wind, or a class feature such as Rage."""

    uses_remaining: int
    uses_max: int


class HitDicePublic(_Frame):
    """The Hit Dice ledger view every class has (`hit_dice_status`)."""

    remaining: int
    total: int
    die: str


class SpellSlotPublic(_Frame):
    """Spell slots at one level (`spell_slots_status`); remaining never exceeds total."""

    total: int
    remaining: int


class CharacterPublic(_Frame):
    """The hero on a committed turn (`character_play_state`) with its read-only resource views.

    `class` is a Python keyword, so the field is named `character_class` and validated by alias.
    The optional resources are present only for classes that hold them.
    """

    id: int
    name: str
    character_class: str = Field(alias="class")
    level: int
    hp_current: int
    hp_max: int
    milestones_pending: int = Field(default=0, ge=0)
    stats: dict[str, Any] | None
    inventory: list[Any] | dict[str, Any] | None
    second_wind: UsesPublic | None = None
    hit_dice: HitDicePublic | None = None
    spell_slots: dict[str, SpellSlotPublic] | None = None
    class_features: dict[str, UsesPublic] | None = None


# --- Frames -------------------------------------------------------------------------------------


class StatusFrame(_Frame):
    """`event: status`: which pipeline stage is running now."""

    stage: StreamStage


class TokenFrame(_Frame):
    """`event: token`: a provisional narration chunk. `done.narration` is authoritative."""

    text: str


class AdjudicationFrame(_Frame):
    """`event: adjudication`: the public ruling (`public_adjudication`).

    Mirrors `AdjudicationResult` minus its engine-only fields (story effects, prepared-amendment
    proposals). `tests/api/test_stream_schema.py` checks this key set against the model.
    """

    success: bool
    damage: int | None
    conditions: list[str]
    mechanical_summary: str
    dice_result: int | None
    citation_ids: list[str]
    citations: list[dict[str, Any]]
    character_update: dict[str, Any] | None
    flags_set: list[dict[str, Any]]
    flags_cleared: list[str]
    scene_id: str | None
    check: str | None
    dc: int | None
    against: str | None
    modifier: int | None
    roll_expression: str | None
    natural_roll: int | None
    dice_log: list[dict[str, Any]]
    completion_status: str | None
    rest_record: dict[str, Any] | None


class ErrorFrame(_Frame):
    """`event: error`: a stable code and fixed copy. Never raw exception or provider text."""

    code: str
    category: str
    message: str
    detail: str
    recovery_action: str
    retryable: bool
    support_ref: str | None
    outcome: OutcomeLabel
    # Only set when a source bucket failed and the turn was answered without it.
    retrieval_degraded: bool | None = None


class DoneFrame(_Frame):
    """`event: done`: the committed turn. Authoritative for narration and state."""

    event_id: int | None
    narration: str
    citations: list[dict[str, Any]]
    ruling_citation_ids: list[str]
    rules_edition: str | None
    character: CharacterPublic | None
    current_scene_id: str | None
    combat_state: CombatStatePublic | None
    campaign_complete: bool
    retrieval_degraded: bool
    source_status: list[dict[str, Any]]
    source_mode: str
    roll_status: str
    roll: dict[str, Any] | None


class DebugFrame(_Frame):
    """`event: debug`: developer diagnostics. Only sent when requested and enabled server-side.

    `combat_state` is the developer view (a superset of `CombatStatePublic`), so it stays opaque
    here rather than duplicating every diagnostic key.
    """

    parsed_input: dict[str, Any] | None
    rules_context: list[dict[str, Any]]
    adventure_context: list[dict[str, Any]]
    rules_context_count: int
    adventure_context_count: int
    world_flags_in: dict[str, Any]
    combat_state: dict[str, Any] | None
    timings_ms: dict[str, int | None]
    combat_diagnostics: dict[str, Any] | None
    turn_diagnostics: dict[str, Any] | None
    model: str
    model_profiles: dict[str, Any]


FRAME_MODELS: dict[str, type[_Frame]] = {
    "status": StatusFrame,
    "token": TokenFrame,
    "adjudication": AdjudicationFrame,
    "debug": DebugFrame,
    "done": DoneFrame,
    "error": ErrorFrame,
}
STREAM_EVENTS: tuple[str, ...] = tuple(FRAME_MODELS)

# Public projections published in the schema alongside the frames.
PUBLIC_MODELS: dict[str, type[_Frame]] = {
    "CombatantPublic": CombatantPublic,
    "CombatStatePublic": CombatStatePublic,
    "CombatOutcomeEntryPublic": CombatOutcomeEntryPublic,
    "CharacterPublic": CharacterPublic,
}


def frame_violations(event: str, data: Any) -> list[str]:
    """Return where `data` breaks the contract for `event`; empty when it conforms.

    Each entry names the field path and the error type only. Values are never returned, so a
    violation can be logged without leaking player or provider text.
    """
    model = FRAME_MODELS.get(event)
    if model is None:
        return [f"unknown event: {event}"]
    try:
        model.model_validate(data)
    except ValidationError as exc:
        return [
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in exc.errors()
        ]
    return []


def render_stream_schema() -> str:
    """The committed contract as deterministic JSON text (trailing newline included)."""
    document: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Edgar turn stream contract",
        "description": (
            "Generated from apps/api/schemas/stream.py by tools/generate_stream_schema.py. "
            "Do not edit by hand."
        ),
        "events": list(STREAM_EVENTS),
        "statusStages": list(STREAM_STAGES),
        "frames": {event: model.model_json_schema() for event, model in FRAME_MODELS.items()},
        "publicModels": {
            name: model.model_json_schema() for name, model in PUBLIC_MODELS.items()
        },
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
