"""Source-independent preferences and an advisory measurement target. No hero or spend caps."""

from pydantic import BaseModel, ConfigDict, Field


class PreparationSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cost_target_microusd: int = Field(default=5_000_000, ge=0)
    preferences: str = Field(default="", max_length=2000)
