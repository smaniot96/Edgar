"""The Ready action for a melee attack (EDG-044c; 2014 PDF p.194).

Ready takes the hero's action. The hero names a perceivable trigger and the attack it holds. The
engine supports one trigger: a named foe closes into the hero's melee band on its own turn. When it
happens, the readied attack is made with the hero's reaction, right after the foe's move and before
the foe's own attacks (PDF p.191: a reaction that interrupts a turn lets that creature continue). The
reaction is spent until the hero's next turn starts (PDF p.191). The readied action lapses at that
point if the trigger did not occur. The PHB gives no duration for it; the lapse is the engine's
reading of "act later in the round".

Other triggers ("when the cultist steps on the trapdoor"), a ranged readied attack, a readied spell
or a readied move are not executed. The engine refuses them with one question that names the nearest
supported option, before any roll and without spending the turn (see `agent.combat_declaration`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.combatants import can_act, stable_id
from agent.monster_stats import stats_of
from agent.positioning import RANGED, band_of

# The player's words that ready an attack or an action ("I ready an attack", "ready my action"). "I'm
# ready to attack" is a state, not the Ready action, so no article-less "to" is read here.
READY_PHRASE = re.compile(
    r"\bready(?:ing)?\s+(?:an?\s+|my\s+|the\s+)?(?:attack|strike|swing|action)\b",
    re.IGNORECASE,
)
# The clause that gives the trigger, after the words "when", "if" or "as soon as".
_TRIGGER_OPENER = re.compile(r"\b(?:when|whenever|if|once|as\s+soon\s+as)\b", re.IGNORECASE)
# A foe closing on the hero: a movement verb followed, within a few words, by a word for the hero's
# reach. The trigger is the foe's own move into melee, which is the only one the engine observes.
_CLOSE_VERB = (
    r"(?:close[sd]?|closing|comes?|coming|steps?|stepping|moves?|moving|advances?|advancing|"
    r"charges?|charging|rush(?:es)?|runs?|walks?)"
)
_CLOSE_IN = re.compile(
    # "closes in", "steps into melee", "moves up": the verb and its direction.
    r"\b" + _CLOSE_VERB + r"\s+(?:in|into|up|back\s+in)\b"
    # "closes next to you", "comes within reach": the verb, then a word for the hero's reach.
    r"|\b" + _CLOSE_VERB + r"\b(?=[^.;!?]{0,40}?\b(?:me|us|you|reach|melee|next\s+to|adjacent|"
    r"close(?:r)?|within|in\s+reach|near)\b)",
    re.IGNORECASE,
)
_RANGED_WORDS = re.compile(
    r"\b(?:shoot\w*|fire\w*|loose\w*|arrow|bow|crossbow|sling|dart|javelin|throw\w*|spell|cast\w*)\b",
    re.IGNORECASE,
)


# A standing order (EDG-182): the same Ready, kept after each trigger until the player cancels it. The
# words are the engine's ruling for "every time", which PHB Ready (p.194) does not give.
STANDING_PHRASE = re.compile(
    r"\b(?:whenever|every\s+time|each\s+time|from\s+now\s+on|standing)\b", re.IGNORECASE
)
# A reply that cancels a standing order: "cancel the standing order against the goblin", "stop ready".
CANCEL_ORDER = re.compile(
    r"^\W*(?:cancel|stop|end|drop|clear)\b[^.;!?]*\b(?:order|orders|standing|readied|ready)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReadyOrder:
    """A validated readied attack: the foe it is held against and the trigger the words gave."""

    foe_name: str
    foe_id: str
    trigger: str
    weapon_hint: str
    foe: Mapping[str, Any] = field(compare=False, repr=False)
    standing: bool = False  # kept after it fires, until cancelled (EDG-182)


def _named_foes(clause: str, foes: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The living foes whose full name the clause contains ("goblin" is not "goblin 2")."""
    lower = clause.lower()
    found = []
    for foe in foes:
        name = str(foe.get("name") or "").lower()
        if not name:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)(?!\s+\d)", lower):
            found.append(foe)
    return found


def movement_text(text: str | None) -> str | None:
    """The declared text the hero's own moves are read from. A readied attack's trigger ("when the goblin
    closes in") describes the foe's move, not the hero's, so the trigger clause is left out."""
    body = str(text or "")
    phrase = READY_PHRASE.search(body)
    if phrase is None:
        return text
    opener = _TRIGGER_OPENER.search(body, phrase.end())
    return body[: opener.start()] if opener else body


def closes_in(foe: Mapping[str, Any]) -> bool:
    """True when the foe can close into melee: a legacy row, or a record with a melee attack or a save
    action. A foe with only ranged attacks keeps its distance and never triggers a readied attack."""
    stats = stats_of(foe)
    if stats is None:
        return True
    return any(not attack.ranged or attack.save for attack in stats.attacks)


def ready_order(
    text: str | None, foes: Sequence[Mapping[str, Any]], *, hero: str
) -> ReadyOrder | str:
    """The validated readied attack, or the question that names what is missing or unsupported.

    `foes` are the living foes the hero can name. The question is a string, asked with no roll and no
    turn spent. A valid order has exactly one foe, a closing trigger and a melee attack.
    """
    body = str(text or "")
    phrase = READY_PHRASE.search(body)
    if phrase is None:
        raise ValueError("ready_order needs a readying phrase")
    example = "for example 'ready an attack when the goblin closes in'"
    opener = _TRIGGER_OPENER.search(body, phrase.end())
    if opener is None:
        return (
            f"{hero} must name a trigger to ready an attack, {example}. No roll is made and no turn "
            "is spent."
        )
    trigger = body[opener.start() :].strip().rstrip(".")
    named = _named_foes(trigger, foes)
    if not named:
        return (
            f"{hero} readies an attack against which foe? Name it in the trigger, {example}. No roll "
            "is made and no turn is spent."
        )
    if len(named) > 1:
        names = ", ".join(str(f.get("name")) for f in named)
        return (
            f"{hero} readies an attack against which of these foes? ({names}). No roll is made and no "
            "turn is spent."
        )
    foe = named[0]
    name = str(foe.get("name"))
    if _RANGED_WORDS.search(body[phrase.start() : opener.start()]):
        return (
            f"Readied attacks are melee here: {hero} can ready a melee attack against {name} that "
            "triggers when it closes in. No roll is made and no turn is spent."
        )
    if not _CLOSE_IN.search(trigger):
        return (
            f"The engine can ready an attack only on a foe that closes into melee, and it cannot read "
            f"that trigger. Choose: ready an attack when {name} closes in, or attack {name} now. No "
            "roll is made and no turn is spent."
        )
    if not closes_in(foe):
        return (
            f"{name} has no melee attack and keeps its distance, so a readied attack against it would "
            f"never trigger. Attack it now, or back away so it closes in. No roll is made and no turn "
            "is spent."
        )
    return ReadyOrder(
        foe_name=name,
        foe_id=stable_id(foe),
        trigger=f"{name} closes in",
        weapon_hint=body,
        foe=foe,
    )


def foe_is_beyond_reach(foe: Mapping[str, Any], bands: Mapping[int, str]) -> bool:
    """True when the foe stands at range after the hero's declared moves (`bands` from MovePlan)."""
    return bands.get(id(foe), band_of(foe)) == RANGED and can_act(foe)
