"""Per-turn action economy for the player (EDG-044, first slice).

On its turn a creature has one action and one bonus action (2014 PHB, Chapter 9). The engine
classifies each declared action into a slot from the code-owned table here; the model never
chooses a slot. A turn is one player input, so several declared actions are allowed only when
they use different slots (an attack and Second Wind, say). A second action, or a second bonus
action, is refused before any dice are rolled, and the round does not advance.

What the turn spent is recorded on the player combatant as `action_economy` and mirrored on the
combat state for the public payload. A Dodge is the `dodging` typed effect (agent.combatants).

Not modelled here: Dash, Disengage, Help, reactions, opportunity attacks, movement and
positioning, and any bonus action other than Second Wind, Healing Word, Rage and the off-hand
attack (see the contract in `agent.supported_rules`).
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
    kind: str  # "attack" | "maneuver" | "dodge" | "dash" | "disengage" | "item" | "spell" | ...
    slot: str  # ACTION or BONUS_ACTION
    label: str  # how the turn record and refusals name it


DODGE = Declared("dodge", ACTION, "Dodge")
DASH = Declared("dash", ACTION, "Dash")
DISENGAGE = Declared("disengage", ACTION, "Disengage")
SECOND_WIND = Declared("second_wind", BONUS_ACTION, "Second Wind")
OFF_HAND_ATTACK = Declared("off_hand_attack", BONUS_ACTION, "off-hand attack")

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
# The same clause shape for Dash and Disengage. "Dash of salt" and "I dash a glance" are idioms, and
# "dash my hopes" is not an action, so those objects do not declare the action.
_ACTION_DECLARED = (
    r"(?:^|[.;,!?]\s*|\b(?:i|we|and|then|also|now)\s+)"
    r"(?:(?:will|'ll|want\s+to|choose\s+to|decide\s+to|try\s+to|take|use|do)\s+)*"
    r"(?:the\s+)?{verb}(?:\s+action)?\b"
)
_DASH_DECLARED = re.compile(
    _ACTION_DECLARED.format(verb=r"dash(?:es|ing)?")
    + r"(?!\s+(?:of|a|an|glance|glances|look|looks|off|hopes|expectations|my|your|his|her|its|"
    r"their|our|it\s+all)\b)",
    re.IGNORECASE,
)
_DISENGAGE_DECLARED = re.compile(_ACTION_DECLARED.format(verb=r"disengag(?:e|es|ing)"), re.I)
# Two-weapon fighting (PHB, PDF p.196) in the player's own words. "On the other hand" is an idiom.
_OFF_HAND_DECLARED = re.compile(
    r"\b(?:off[- ]?hand|two[- ]weapon\s+fighting|dual[- ]wield\w*|both\s+(?:weapons|blades)"
    r"|(?:with|using)\s+(?:my|the)\s+other\s+(?:hand|weapon|blade))\b",
    re.IGNORECASE,
)
_ATTACK_WORDS = re.compile(
    r"\b(?:attack\w*|strik\w*|stab\w*|swing\w*|slash\w*|shoot\w*|lunge\w*|smash\w*|thrust\w*"
    r"|hit\s+(?:the|a|an|him|her|it|them)|cut\s+(?:the|a|an|him|her|it|them))\b",
    re.IGNORECASE,
)


def dodge_declared(text: str | None) -> bool:
    return bool(_DODGE_DECLARED.search(str(text or "")))


def dash_declared(text: str | None) -> bool:
    return bool(_DASH_DECLARED.search(str(text or "")))


def disengage_declared(text: str | None) -> bool:
    return bool(_DISENGAGE_DECLARED.search(str(text or "")))


def off_hand_declared(text: str | None) -> bool:
    return bool(_OFF_HAND_DECLARED.search(str(text or "")))


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


def off_hand_refusal(hero: str, *, attacked: bool, pair: bool) -> str | None:
    """The engine's refusal of an off-hand attack that lacks its Attack action or its weapon pair."""
    if not attacked:
        return (
            f"{hero} can attack with the off hand only after taking the Attack action with a "
            "light melee weapon this turn. No roll is made and no turn is spent."
        )
    if not pair:
        return (
            f"{hero} needs a different light melee weapon in the other hand, with no shield, to "
            "attack with the off hand. No roll is made and no turn is spent."
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
