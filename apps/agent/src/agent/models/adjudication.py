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

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema


class CharacterUpdate(BaseModel):
    hp_delta: int | None = None  # negative for damage, positive for healing; clamped to [0, hp_max]
    # LLM proposal for damage taken (e.g. "1d6" for a trap). The engine rolls it; only applied
    # when this turn's check failed.
    damage_dice: str | None = None
    add_conditions: list[str] = Field(default_factory=list)
    remove_conditions: list[str] = Field(default_factory=list)
    inventory_add: list[str] = Field(default_factory=list)
    inventory_remove: list[str] = Field(default_factory=list)

    @field_validator(
        "add_conditions", "remove_conditions", "inventory_add", "inventory_remove", mode="before"
    )
    @classmethod
    def _null_lists_to_empty(cls, value: object) -> list:
        return value if value is not None else []


class FlagUpdate(BaseModel):
    key: str  # snake_case, stable across turns (e.g. "door_unlocked_atrium")
    value: str  # stringified; world_flags.value is varchar


class AdjudicationResult(BaseModel):
    success: bool
    damage: int | None = None
    conditions: list[str] = []
    mechanical_summary: str = ""
    dice_result: int | None = None  # overwritten by the adjudicator with the actual roll

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

    @field_validator("flags_set", "flags_cleared", "conditions", "dice_log", mode="before")
    @classmethod
    def _null_lists_to_empty(cls, value: object) -> list:
        # Structured LLM output often sends explicit null instead of omitting keys.
        return value if value is not None else []
