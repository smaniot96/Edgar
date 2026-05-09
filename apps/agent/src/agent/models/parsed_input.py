"""ParsedInput: output of InputParser node."""

from pydantic import BaseModel


class ParsedInput(BaseModel):
    """Structured output from InputParser."""

    intent: str  # combat | rp | exploration
    entities: dict = {}
    dice_expression: str | None = None
