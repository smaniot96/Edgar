from pydantic import BaseModel, ConfigDict, Field

from ._validators import forbid_null


class NPCCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    disposition: str = Field(..., min_length=1, max_length=255)
    stat_block: dict = Field(default_factory=dict)


class NPCRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    name: str
    disposition: str
    stat_block: dict


class NPCUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    disposition: str | None = Field(None, min_length=1, max_length=255)
    stat_block: dict | None = None

    reject_nulls = forbid_null("name", "disposition", "stat_block")
