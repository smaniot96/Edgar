"""Level-up request and response (EDG-045). The engine decides every number; the client only chooses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.progression import ability_name

from .character import CampaignCharacterRead


class LevelUpRequest(BaseModel):
    """Spend one milestone to advance one level.

    `from_level` is the level the client saw: a stale request is refused rather than applied twice.
    `request_id` makes a retry idempotent. `hp_method` chooses the class table's fixed value (the
    default) or an engine roll of the hit die. `ability_increases` is the Ability Score Improvement
    at level 4 (two points: +2 to one score or +1 to two), and is refused at any other level.
    """

    from_level: int = Field(..., ge=1, le=19)
    request_id: str = Field(..., min_length=1, max_length=64)
    hp_method: Literal["average", "roll"] = "average"
    ability_increases: dict[str, int] | None = Field(default=None, max_length=6)

    @field_validator("ability_increases")
    @classmethod
    def _known_abilities(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        for key, points in value.items():
            if ability_name(key) is None:
                raise ValueError(f"{key} is not an ability score.")
            if isinstance(points, bool) or not isinstance(points, int) or not 1 <= points <= 2:
                raise ValueError("Each ability score improvement is 1 or 2 points.")
        return value


class LevelUpHitPoints(BaseModel):
    amount: int
    method: Literal["average", "roll"]
    dice: dict[str, Any] | None = None
    hp_max_before: int
    hp_max_after: int


class LevelUpFeature(BaseModel):
    name: str
    executed: bool


class LevelUpRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applied: bool  # False when this request_id was already applied (an idempotent replay)
    from_level: int
    to_level: int
    hit_points: LevelUpHitPoints
    ability_increases: dict[str, int] = Field(default_factory=dict)
    features: list[LevelUpFeature] = Field(default_factory=list)
    spell_slots: dict[str, int] = Field(default_factory=dict)
    milestones_pending: int
    character: CampaignCharacterRead
