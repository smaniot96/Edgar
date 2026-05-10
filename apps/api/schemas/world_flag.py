from pydantic import BaseModel, ConfigDict, Field


class WorldFlagCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., max_length=255)


class WorldFlagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    key: str
    value: str


class WorldFlagUpdate(BaseModel):
    value: str = Field(..., max_length=255)
