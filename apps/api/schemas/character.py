from pydantic import BaseModel, ConfigDict, Field


class CharacterCreate(BaseModel):
    """Request body for POST /characters — all required DB columns."""
    campaign_id: int = Field(..., ge=1)
    name: str = Field(..., min_length=1, max_length=255)
    character_class: str = Field(..., min_length=1, max_length=255)  # maps to ORM 'class'
    level: int = Field(..., ge=1, le=20)
    hp_current: int = Field(..., ge=0)
    hp_max: int = Field(..., ge=1)
    stats: dict = Field(default_factory=dict)  # JSONB
    inventory: dict = Field(default_factory=dict)  # JSONB


class CharacterRead(BaseModel):
    """Response body — mirrors Character ORM columns."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    name: str
    character_class: str
    level: int
    hp_current: int
    hp_max: int
    stats: dict
    inventory: dict


class CharacterUpdate(BaseModel):
    """Request body for PATCH /characters/{id} — all optional."""
    name: str | None = Field(None, min_length=1, max_length=255)
    character_class: str | None = Field(None, min_length=1, max_length=255)
    level: int | None = Field(None, ge=1, le=20)
    hp_current: int | None = Field(None, ge=0)
    hp_max: int | None = Field(None, ge=1)
    stats: dict | None = None
    inventory: dict | None = None
