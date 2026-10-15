"""Per-turn action economy for the player (EDG-044, first slice).

On its turn a creature has one action and one bonus action (2014 PHB, Chapter 9). The engine
classifies each declared action into a slot from the code-owned table here; the model never
chooses a slot. A turn is one player input, so several declared actions are allowed only when
they use different slots (an attack and Second Wind, say). A second action, or a second bonus
action, is refused before any dice are rolled, and the round does not advance.

What the turn spent is recorded on the player combatant as `action_economy` and mirrored on the
combat state for the public payload. A Dodge is the `dodging` typed effect (agent.combatants).

Not modelled here: Dash, Disengage, Help, reactions, opportunity attacks, movement and
positioning, and any bonus action other than Second Wind and Healing Word (see the contract in
`agent.supported_rules`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ACTION = "action"
BONUS_ACTION = "bonus_action"
_SLOT_NAMES = {ACTION: "action", BONUS_ACTION: "bonus action"}


@dataclass(frozen=True)
class Declared:
    kind: str  # "attack" | "maneuver" | "dodge" | "item" | "spell" | "second_wind"
    slot: str  # ACTION or BONUS_ACTION
    label: str  # how the turn record and refusals name it


DODGE = Declared("dodge", ACTION, "Dodge")
SECOND_WIND = Declared("second_wind", BONUS_ACTION, "Second Wind")

# A declared Dodge: the player's own clause, "I dodge", "I take the Dodge action", "and dodge".
# A third-person or narrative use ("the goblin tries to dodge", "I dodge the arrow") is not one.
_DODGE_DECLARED = re.compile(
    r"(?:^|[.;,!?]\s*|\b(?:i|we|and|then|also|now)\s+)"
    r"(?:(?:will|'ll|want\s+to|choose\s+to|decide\s+to|try\s+to|take|use|do)\s+)*"
    r"(?:the\s+)?dodg(?:e|ing)(?:\s+action)?\b"
    r"(?!\s+(?:the|a|an|his|her|its|their|my|your|that|this|it|out|past|under|behind|away"
    r"|around|from|of|to|at|off|incoming)\b)",
    re.IGNORECASE,
)
# The object of a dodge ("dodge the attack") is not an attack declared alongside it.
_DODGE_OBJECT = re.compile(
    r"\bdodg\w*\s+(?:the|an?|its|their|his|her|my|that|this|incoming)\s+\w+", re.I
)
_ATTACK_WORDS = re.compile(
    r"\b(?:attack\w*|strik\w*|stab\w*|swing\w*|slash\w*|shoot\w*|lunge\w*|smash\w*|thrust\w*"
    r"|hit\s+(?:the|a|an|him|her|it|them)|cut\s+(?:the|a|an|him|her|it|them))\b",
    re.IGNORECASE,
)


def dodge_declared(text: str | None) -> bool:
    return bool(_DODGE_DECLARED.search(str(text or "")))


def has_attack_words(text: str | None) -> bool:
    """True when the player's own words name an attack (a dodge's object does not count)."""
    return bool(_ATTACK_WORDS.search(_DODGE_OBJECT.sub(" ", str(text or ""))))


def refusal_for(declared: Sequence[Declared], hero: str) -> str | None:
    """The engine's refusal when one turn declares two actions of the same slot. None otherwise."""
    for slot in (ACTION, BONUS_ACTION):
        labels = [d.label for d in declared if d.slot == slot]
        if len(labels) > 1:
            return (
                f"{hero} can take only one {_SLOT_NAMES[slot]} per turn, and this turn declares "
                f"{' and '.join(labels)}. Choose one: no roll is made and no turn is spent."
            )
    return None


def economy_record(round_num: int, spent: Iterable[Declared], *, dodging: bool) -> dict[str, Any]:
    """What the turn spent, as stored on the player combatant and mirrored for the client."""
    spent = list(spent)
    return {
        "round": int(round_num),
        "action": next((d.label for d in spent if d.slot == ACTION), None),
        "bonus_action": next((d.label for d in spent if d.slot == BONUS_ACTION), None),
        "dodging": bool(dodging),
    }


def economy_text(combatants: Iterable[Mapping[str, Any]]) -> str | None:
    """Narrator context for the player's turn: what the engine recorded, and nothing more."""
    player = next((c for c in combatants if c.get("is_player")), None)
    record = (player or {}).get("action_economy")
    if not isinstance(record, Mapping):
        return None
    action = record.get("action") or "none"
    bonus = record.get("bonus_action") or "none"
    text = f"Hero's turn (engine-recorded): action used: {action}; bonus action used: {bonus}."
    if record.get("dodging"):
        text += (
            " The hero is dodging: attacks against the hero have disadvantage until the hero's "
            "next turn."
        )
    return text
