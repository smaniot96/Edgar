"""Structured outcome the RulesAdjudicator returns and the API persists.

Field semantics:
  - `success`, `damage`, `conditions`, `mechanical_summary`, `dice_result`: human-facing
    summary fed into the narrator prompt.
  - `character_update`, `flags_set`, `flags_cleared`, `scene_id`: machine-readable side
    effects applied by `apps.api.services.world_writes.apply_adjudication`. These are the
    "writes" the LLM proposes and Postgres commits.

Keep field names stable: changing them silently breaks the LLM's structured output schema.
"""

from pydantic import BaseModel, Field, field_validator


class CharacterUpdate(BaseModel):
    hp_delta: int | None = None  # negative for damage, positive for healing; clamped to [0, hp_max]
    add_conditions: list[str] = Field(default_factory=list)
    remove_conditions: list[str] = Field(default_factory=list)
    inventory_add: list[str] = Field(default_factory=list)
    inventory_remove: list[str] = Field(default_factory=list)


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

    @field_validator("flags_set", "flags_cleared", mode="before")
    @classmethod
    def _null_lists_to_empty(cls, value: object) -> list:
        # Structured LLM output often sends explicit null instead of omitting keys.
        return value if value is not None else []
