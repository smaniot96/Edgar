"""InputParser output. The intent value drives the conditional edge after input_parser.

The parser LLM only *proposes* what kind of check the action needs (`check` + `difficulty`);
the engine (`agent.resolution`) turns that into a modifier from the character sheet, a DC, and
a deterministic success/failure from the dice tool. The LLM never picks numbers.

`dice_expression` is legacy: older prompts returned e.g. "1d20+3". It is still accepted (and
tolerated when invalid) but the engine ignores its modifier when `check` is set.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from agent.declarations import normalise_kind

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


class ProposedAction(BaseModel):
    """One declared action the parser proposes for a combat turn (EDG-182). A proposal, not a fact:
    the engine adopts it only when its own detection of the player's words agrees."""

    kind: str = Field(
        default="unknown",
        description=(
            "One of: attack, off_hand_attack, maneuver, item, spell, dodge, dash, disengage, ready, "
            "second_wind, rage, move, contingency."
        ),
    )
    text: str | None = Field(
        default=None, description="The player's own words for this action, quoted or closely kept."
    )
    target: str | None = Field(default=None, description="Creature the action targets, if any.")
    weapon: str | None = Field(default=None, description="Weapon named for an attack, if any.")
    spell: str | None = Field(default=None, description="Spell named for a spell, if any.")
    destination: str | None = Field(default=None, description="Where a move goes, e.g. '30 feet north'.")

    @field_validator("kind", mode="before")
    @classmethod
    def _registered_kind(cls, value: object) -> str:
        return normalise_kind(value)

    @field_validator("text", "target", "weapon", "spell", "destination", mode="before")
    @classmethod
    def _short_text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:200] or None


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
    lethality: Literal["lethal", "nonlethal"] | None = Field(
        default=None,
        description=(
            "Only for an attack in combat: 'nonlethal' when the player declares they want to knock "
            "out, subdue, stun or capture the target; 'lethal' when they declare they want to kill "
            "it; null when they state neither. Never guess."
        ),
    )
    actions: list[ProposedAction] = Field(
        default_factory=list,
        description=(
            "Combat only: the ordered declared actions this turn, one entry per action the player's "
            "words state (movement, attacks, off-hand attack, item, spell, Dodge, Dash, Disengage, "
            "Ready, Second Wind, Rage, and any if-then condition). Empty when the turn declares none."
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

    @field_validator("lethality", mode="before")
    @classmethod
    def _normalise_lethality(cls, value: object) -> str | None:
        key = str(value or "").strip().lower()
        if key in {"lethal", "kill", "lethal_attack"}:
            return "lethal"
        if key in {"nonlethal", "non-lethal", "non_lethal"}:
            return "nonlethal"
        return None

    @field_validator("actions", mode="before")
    @classmethod
    def _null_actions(cls, value: object) -> list:
        return value if isinstance(value, list) else []

    @field_validator("confirms_ending", mode="before")
    @classmethod
    def _null_bool(cls, value: object) -> bool:
        return bool(value) if value is not None else False
