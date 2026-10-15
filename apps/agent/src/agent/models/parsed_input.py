"""InputParser output. The intent value drives the conditional edge after input_parser.

The parser LLM only *proposes* what kind of check the action needs (`check` + `difficulty`);
the engine (`agent.resolution`) turns that into a modifier from the character sheet, a DC, and
a deterministic success/failure from the dice tool. The LLM never picks numbers.

`dice_expression` is legacy: older prompts returned e.g. "1d20+3". It is still accepted (and
tolerated when invalid) but the engine ignores its modifier when `check` is set.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Intent = Literal["combat", "rp", "exploration"]
Difficulty = Literal["easy", "medium", "hard", "very_hard"]

_INTENT_ALIASES = {
    "combat": "combat",
    "attack": "combat",
    "fight": "combat",
    "rp": "rp",
    "roleplay": "rp",
    "role-play": "rp",
    "social": "rp",
    "dialogue": "rp",
    "exploration": "exploration",
    "explore": "exploration",
}

_DIFFICULTY_ALIASES = {
    "easy": "easy",
    "medium": "medium",
    "moderate": "medium",
    "normal": "medium",
    "hard": "hard",
    "very_hard": "very_hard",
    "very hard": "very_hard",
    "very-hard": "very_hard",
    "veryhard": "very_hard",
}


class ParsedInput(BaseModel):
    intent: Intent = "exploration"
    entities: dict = {}  # free-form, e.g. {"target": "goblin", "skill": "perception"}
    check: str | None = Field(
        default=None,
        description=(
            "Ability/skill the action tests, or null when no roll is needed. One of: an ability "
            "(str, dex, con, int, wis, cha), a skill (athletics, acrobatics, stealth, "
            "sleight_of_hand, perception, investigation, insight, survival, medicine, arcana, "
            "history, nature, religion, animal_handling, persuasion, deception, intimidation, "
            "performance), a saving throw (e.g. dex_save), 'attack' (weapon attack) or "
            "'spell_attack'."
        ),
    )
    difficulty: Difficulty | None = Field(
        default=None,
        description="How hard the task is: easy | medium | hard | very_hard. Ignored for attacks.",
    )
    target: str | None = Field(
        default=None, description="Creature/object the action targets, if any (e.g. 'goblin')."
    )
    retrieval_query: str | None = Field(
        default=None,
        description=(
            "Short mechanics query (<= 15 words) for rules lookup, naming the "
            "skill, spell or feature without story/scene names (e.g. 'Grappling Athletics')."
        ),
    )
    uses_item: str | None = Field(
        default=None,
        description="Inventory item the player consumes/uses this turn (e.g. 'potion of healing').",
    )
    confirms_ending: bool = Field(
        default=False,
        description=(
            "True only if the player explicitly confirms they want the adventure/campaign to end "
            "now (e.g. after being asked whether to conclude the story)."
        ),
    )
    resolution_policy_id: str | None = Field(
        default=None,
        max_length=265,
        description=(
            "A collection-qualified reviewed policy key from the current-scene choices, or null. "
            "Never invent a key."
        ),
    )
    recall_fact_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The exact committed earned-clue fact ID the player explicitly asks to reread, or null. "
            "Choose only from the supplied session-earned clue choices."
        ),
    )
    dice_expression: str | None = None  # legacy; e.g. "1d20+3". Engine prefers `check`.

    @field_validator("intent", mode="before")
    @classmethod
    def _normalise_intent(cls, value: object) -> str:
        key = str(value or "").strip().lower()
        return _INTENT_ALIASES.get(key, "exploration")

    @field_validator("difficulty", mode="before")
    @classmethod
    def _normalise_difficulty(cls, value: object) -> str | None:
        if value is None or value == "":
            return None
        key = str(value).strip().lower()
        return _DIFFICULTY_ALIASES.get(key, "medium")

    @field_validator("entities", mode="before")
    @classmethod
    def _null_entities(cls, value: object) -> dict:
        return value if isinstance(value, dict) else {}

    @field_validator(
        "check",
        "target",
        "retrieval_query",
        "uses_item",
        "dice_expression",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "n/a"}:
            return None
        return text[:200]

    @field_validator("resolution_policy_id", "recall_fact_id", mode="before")
    @classmethod
    def _normalise_selector(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "n/a"}:
            return None
        return text

    @field_validator("confirms_ending", mode="before")
    @classmethod
    def _null_bool(cls, value: object) -> bool:
        return bool(value) if value is not None else False
