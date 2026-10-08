from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._validators import forbid_null


class CurrentAssignmentSummary(BaseModel):
    """Active assignment context for library views."""

    campaign_id: int
    campaign_title: str


class CharacterCreate(BaseModel):
    """Create a character in the library (no campaign binding)."""

    name: str = Field(..., min_length=1, max_length=255)
    character_class: str = Field(..., min_length=1, max_length=255)
    level: int = Field(..., ge=1, le=20)
    hp_max: int = Field(..., ge=1)
    base_stats: dict = Field(default_factory=dict)
    base_inventory: dict = Field(default_factory=dict)


class CharacterRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    name: str
    character_class: str
    level: int
    hp_max: int
    base_stats: dict
    base_inventory: dict
    created_at: datetime
    current_assignment: CurrentAssignmentSummary | None = None


class CharacterUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    character_class: str | None = Field(None, min_length=1, max_length=255)
    level: int | None = Field(None, ge=1, le=20)
    hp_max: int | None = Field(None, ge=1)
    base_stats: dict | None = None
    base_inventory: dict | None = None

    reject_nulls = forbid_null(
        "name", "character_class", "level", "hp_max", "base_stats", "base_inventory"
    )


class CampaignCharacterAssignBody(BaseModel):
    character_id: int = Field(..., ge=1)


class CampaignCharacterRead(BaseModel):
    """Active assignment row plus identity for campaign roster UI."""

    model_config = ConfigDict(from_attributes=True)

    assignment_id: int
    character_id: int
    name: str
    character_class: str
    level: int
    hp_current: int
    hp_max: int


class CampaignCharacterPatch(BaseModel):
    hp_current: int | None = Field(None, ge=0)
    hp_max: int | None = Field(None, ge=1)
    stats: dict | None = None
    inventory: dict | None = None

    reject_nulls = forbid_null("hp_current", "hp_max", "stats", "inventory")

    @model_validator(mode="after")
    def _hp_within_max(self) -> "CampaignCharacterPatch":
        # When only one side is sent the router checks against the stored value.
        if self.hp_current is not None and self.hp_max is not None and self.hp_current > self.hp_max:
            raise ValueError("hp_current cannot exceed hp_max")
        return self


class CharacterAssignmentHistoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    campaign_title: str
    hp_current: int
    hp_max: int
    stats: dict
    inventory: dict
    assigned_at: datetime
    ended_at: datetime | None
    status: Literal["active", "ended"]
