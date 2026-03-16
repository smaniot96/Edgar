"""AdjudicationResult: output of RulesAdjudicator node."""

from pydantic import BaseModel


class AdjudicationResult(BaseModel):
    """Structured outcome from RulesAdjudicator."""

    success: bool
    damage: int | None = None
    conditions: list[str] = []
    mechanical_summary: str = ""
    dice_result: int | None = None
