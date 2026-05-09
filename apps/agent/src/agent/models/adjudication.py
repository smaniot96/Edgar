"""AdjudicationResult: output of RulesAdjudicator node."""

from pydantic import BaseModel, Field


class CharacterUpdate(BaseModel):
    """Mechanical changes to the active player character."""

    hp_delta: int | None = None
    add_conditions: list[str] = Field(default_factory=list)
    remove_conditions: list[str] = Field(default_factory=list)
    inventory_add: list[str] = Field(default_factory=list)
    inventory_remove: list[str] = Field(default_factory=list)


class FlagUpdate(BaseModel):
    """Single campaign world flag upsert."""

    key: str
    value: str  # stringified; DB column is varchar


class AdjudicationResult(BaseModel):
    """Structured outcome from RulesAdjudicator."""

    success: bool
    damage: int | None = None
    conditions: list[str] = []
    mechanical_summary: str = ""
    dice_result: int | None = None
    character_update: CharacterUpdate | None = None
    flags_set: list[FlagUpdate] = Field(default_factory=list)
    flags_cleared: list[str] = Field(default_factory=list)
    scene_id: str | None = None  # if set, becomes the session's current_scene_id
