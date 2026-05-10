from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
