"""InputParser output. The intent value drives the conditional edge after input_parser."""

from pydantic import BaseModel


class ParsedInput(BaseModel):
    intent: str  # "combat" | "rp" | "exploration"
    entities: dict = {}  # free-form, e.g. {"target": "goblin", "skill": "perception"}
    dice_expression: str | None = None  # e.g. "1d20+3" when a roll is needed
