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
    r"|bash\w*|punch\w*|kick\w*|cleav\w*|hurl\w*|throw\w*|kill\w*|(?:keep|continue)\s+fighting|knock\s+(?:(?:the|a|an)\s+)?\w+\s+out"
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
    return bool(affirmative_matches(_ATTACK_WORDS, _DODGE_OBJECT.sub(" ", str(text or ""))))


def affirmative_matches(pattern: re.Pattern, text: str | None) -> list[re.Match]:
    """Player actions only: quoted speech, hypothetical and negated verbs authorize no mechanics.

    This is a conservative guard, not a semantic parser. Uncertain wording is clarified before dice.
    A new explicit first-person clause resets the subject/negation of an earlier clause.
    """
    body = re.sub(r'"[^"\n]*"|“[^”\n]*”', lambda m: " " * len(m[0]), str(text or ""))
    result = []
    for match in pattern.finditer(body):
        prefix = re.split(r"[.;!?\n]", body[:match.start()])[-1]
        # An explicit new player subject can follow an enemy's description or a refusal.
        if re.match(r"\s*(?:if|unless|whether)\b", prefix, re.I):
            continue
        subjects = list(re.finditer(r"\b(?:i|we)\b", prefix, re.I))
        if subjects:
            prefix = prefix[subjects[-1].start():]
        elif re.match(r"\s*(?:the\s+)?(?:he|she|they|it|enemy|foe|goblin|wolf|guard)\b", prefix, re.I):
            continue
        # A verb mentioned as a topic, memory, question or discarded plan is not a
        # present action. Keep direct clauses (including a later new "I attack")
        # separate from subordinate mentions such as "changed my mind about attacking".
        if re.search(
            r"\b(?:about|against|discuss(?:ing)?|mention(?:ing)?|remember(?:ing)?|"
            r"consider(?:ing)?|reconsider(?:ing)?|regret(?:ting)?|avoid(?:ing)?|"
            r"cancel(?:ling)?|abandon(?:ing)?|gave\s+up|give\s+up)\b[^,;]*$",
            prefix, re.I,
        ):
            continue
        if re.search(r"\b(?:if|unless|would|might|could|whether|instead\s+of)\b", prefix, re.I):
            continue
        if re.search(r"\b(?:not|never|no\s+longer|don't|won't|cannot|can't|refuse\s+to|stop(?:ped|ping)?|ceas(?:e|ed|ing)|without)\b[^,;]*$", prefix, re.I):
            continue
        result.append(match)
    return result


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


def economy_record(
    round_num: int, spent: Iterable[Declared], *, dodging: bool, reaction: str | None = None
) -> dict[str, Any]:
    """What the turn spent, as stored on the player combatant and mirrored for the client.

    `reaction` (EDG-044c) names the reaction the hero spent in this round's foe phase, when it was a
    readied attack; it is additive, and None when the reaction was not used.
    """
    spent = list(spent)
    record: dict[str, Any] = {
        "round": int(round_num),
        "action": next((d.label for d in spent if d.slot == ACTION), None),
        "bonus_action": next((d.label for d in spent if d.slot == BONUS_ACTION), None),
        "dodging": bool(dodging),
    }
    if reaction is not None:  # additive: a record with no reaction keeps its earlier shape
        record["reaction"] = reaction
    return record


def economy_text(combatants: Iterable[Mapping[str, Any]]) -> str | None:
    """Narrator context for the player's turn: what the engine recorded, and nothing more."""
    from agent.combatants import hero_of

    player = hero_of(list(combatants))
    record = (player or {}).get("action_economy")
    if not isinstance(record, Mapping):
        return None
    action = record.get("action") or "none"
    bonus = record.get("bonus_action") or "none"
    text = f"Hero's turn (engine-recorded): action used: {action}; bonus action used: {bonus}."
    if record.get("reaction"):
        text += f" Reaction used this round: {record['reaction']}."
    if record.get("dodging"):
        text += (
            " The hero is dodging: attacks against the hero have disadvantage until the hero's "
            "next turn."
        )
    return text
