"""Structured outcome the RulesAdjudicator returns and the API persists.

Field semantics:
  - `success`, `damage`, `conditions`, `mechanical_summary`, `dice_result`: human-facing
    summary fed into the narrator prompt.
  - `character_update`, `flags_set`, `flags_cleared`, `scene_id`: machine-readable side
    effects applied by `apps.api.services.world_writes.apply_adjudication`. These are the
    "writes" the LLM proposes and Postgres commits.

  - `check`, `dc`, `against`, `modifier`, `roll_expression`, `natural_roll`, `dice_log`,
    `completion_status`: ENGINE-owned. They are hidden from the LLM's structured-output schema
    (`SkipJsonSchema`) and always overwritten by `agent.resolution`, so the UI can render
    "d20+3 = 17 vs DC 15 -> success" from numbers the LLM never touched.

The LLM only *proposes* side effects; `agent.resolution.enforce_side_effects` decides which
survive (e.g. damage only after a failed check, rolled by the engine; healing only from a
consumable actually in inventory).

Keep field names stable: changing them silently breaks the LLM's structured output schema.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from edgar_core.story import AcceptedStoryEffect, StoryEffect


class CharacterUpdate(BaseModel):
    hp_delta: int | None = None  # negative for damage, positive for healing; clamped to [0, hp_max]
    # LLM proposal for damage taken (e.g. "1d6" for a trap). The engine rolls it; only applied
    # when this turn's check failed.
    damage_dice: str | None = None
    add_conditions: list[str] = Field(default_factory=list)
    remove_conditions: list[str] = Field(default_factory=list)
    inventory_add: list[str] = Field(default_factory=list)
    inventory_remove: list[str] = Field(default_factory=list)
    # Engine-owned Second Wind uses left on the sheet (EDG-025). Set only by the engine after a
    # use or a rest; any model-proposed value is cleared in enforce_side_effects.
    second_wind_uses_remaining: SkipJsonSchema[int | None] = None
    # Engine-owned class feature uses left, per feature (EDG-145): only the features written are
    # changed. Cleared for model proposals in enforce_side_effects.
    class_feature_uses: SkipJsonSchema[dict[str, int] | None] = None
    # Engine-owned spell slot ledger (EDG-027): the expended count per spell level, written when
    # set (an empty map clears it, e.g. after a long rest). Hidden from the model's schema.
    spell_slots_expended: SkipJsonSchema[dict[str, int] | None] = None
    # Engine-owned Hit Dice spent (EDG-026): written after a short rest spends a die or a long
    # rest recovers some. A model-proposed value is cleared in enforce_side_effects.
    hit_dice_spent: SkipJsonSchema[int | None] = None
    # Engine-owned death record (EDG-045, death_mode 'death_saves'): written as a dict, or {} to
    # clear it. A model proposal is cleared in enforce_side_effects; the model never sees the field.
    death_saves: SkipJsonSchema[dict | None] = None

    @field_validator(
        "add_conditions", "remove_conditions", "inventory_add", "inventory_remove", mode="before"
    )
    @classmethod
    def _null_lists_to_empty(cls, value: object) -> list:
        return value if value is not None else []


class RestRecord(BaseModel):
    """Engine evidence for a rest turn (EDG-026). Every number is engine-rolled or engine-derived."""

    kind: str  # "short" | "long"
    refused: bool = False  # a short rest with no Hit Dice left: nothing rolled, nothing healed
    expression: str | None = None  # the short-rest roll, e.g. "1d10+2"
    rolled: int | None = None  # total of that roll, before the cap at maximum HP
    healed: int = 0  # HP actually regained, capped at the maximum
    die: str  # the class Hit Die, e.g. "d10", shown even when refused
    hit_dice_remaining: int
    hit_dice_total: int
    hit_dice_recovered: int | None = None  # long rest only: Hit Dice regained


class HomebrewProposal(BaseModel):
    """EDG-169: a homebrew-only improvised fact. The engine validates it before any commit."""

    kind: Literal["place", "person", "clue", "threat"]
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=600)


class FlagUpdate(BaseModel):
    key: str  # snake_case, stable across turns (e.g. "door_unlocked_atrium")
    value: str  # stringified; world_flags.value is varchar


class AdjudicationResult(BaseModel):
    success: bool
    damage: int | None = None
    conditions: list[str] = []
    mechanical_summary: str = ""
    dice_result: int | None = None  # overwritten by the adjudicator with the actual roll

    citation_ids: list[str] = Field(
        default_factory=list,
        description="Exact reference IDs supporting this ruling; never invent IDs.",
    )
    citations: SkipJsonSchema[list[dict]] = Field(default_factory=list)

    story_effects: list[StoryEffect] = Field(default_factory=list)
    accepted_story_effects: SkipJsonSchema[list[AcceptedStoryEffect]] = Field(default_factory=list)
    # Homebrew only (EDG-169): model-proposed improvised places, people, clues and threats. The
    # validator turns accepted ones into homebrew story effects and clears this list.
    # exclude=True keeps it out of every serialized ruling, frame and golden snapshot.
    homebrew_proposals: list[HomebrewProposal] = Field(
        default_factory=list,
        exclude=True,
        description=(
            "Homebrew play only, otherwise empty: new places, named people, clues or threats "
            "consistent with the committed session facts. Never numbers, dice or secrets."
        ),
    )

    # Compatibility for retained saved rulings and their API receipt checks. Live play
    # clears this field and has no amendment proposal in its structured-output schema.
    accepted_prepared_amendment: SkipJsonSchema[dict | None] = None

    # Side effects (see module docstring).
    character_update: CharacterUpdate | None = None
    flags_set: list[FlagUpdate] = Field(default_factory=list)
    flags_cleared: list[str] = Field(default_factory=list)
    scene_id: str | None = None  # becomes sessions.current_scene_id when set

    # Engine-owned resolution details (never produced by the LLM; see module docstring).
    check: SkipJsonSchema[str | None] = None  # e.g. "athletics", "attack", "dex_save"
    dc: SkipJsonSchema[int | None] = None  # DC for checks, target AC for attacks
    against: SkipJsonSchema[str | None] = None  # "DC" | "AC"
    modifier: SkipJsonSchema[int | None] = None  # ability mod (+ proficiency) from the sheet
    roll_expression: SkipJsonSchema[str | None] = None  # e.g. "1d20+3"
    natural_roll: SkipJsonSchema[int | None] = None  # the raw d20 face
    dice_log: SkipJsonSchema[list[dict]] = Field(default_factory=list)  # every engine roll
    # None (not evaluated) | "none" | "pending" | "accepted" | "rejected"
    completion_status: SkipJsonSchema[str | None] = None
    # Engine evidence of a rest turn (EDG-026); never a model proposal.
    rest_record: SkipJsonSchema[RestRecord | None] = None

    @field_validator(
        "story_effects",
        "accepted_story_effects",
        "homebrew_proposals",
        "flags_set",
        "flags_cleared",
        "conditions",
        "dice_log",
        "citation_ids",
        "citations",
        mode="before",
    )
    @classmethod
    def _null_lists_to_empty(cls, value: object) -> list:
        # Structured LLM output often sends explicit null instead of omitting keys.
        return value if value is not None else []
