"""Non-spoiler commissioning inputs. Hero statistics are always loaded by the server."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class PreparationSetup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    character_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=6)
    target_level: int | None = Field(default=None, ge=1, le=20)
    max_cost_microusd: int = Field(ge=1, le=1_000_000_000)
    max_provider_calls: int = Field(default=200, ge=1, le=1000)
    max_total_tokens: int = Field(default=4_000_000, ge=1, le=20_000_000)
    required_maps: bool = True
    required_handouts: bool = True
    preferences: str = Field(default="", max_length=2000)

