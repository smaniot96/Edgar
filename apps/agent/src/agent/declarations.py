"""Registered declared-action types for a combat turn (EDG-182, increment 1).

A combat turn is a list of declared actions. The parser proposes that list as typed entries; the
engine never adopts a proposal on the model's word. `agreement_question` checks every proposal
against what the engine itself detected from the player's words and state, and asks one question
when they disagree. Nothing here rolls a die or spends a turn.

Each registered type names its action-economy slot and the fields its declaration needs. The
kinds are the same strings the engine already records on `action_economy.Declared`, so the registry
extends the existing mechanism rather than replacing it.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass

from agent.action_economy import ACTION, BONUS_ACTION

FREE = "free"  # movement and conditions: no action-economy slot is spent


@dataclass(frozen=True)
class ActionType:
    kind: str  # the engine's kind string (action_economy.Declared.kind)
    slot: str  # ACTION, BONUS_ACTION or FREE
    label: str  # how refusals and the turn record name it
    required: tuple[str, ...] = ()  # fields a declaration of this type needs


ATTACK = ActionType("attack", ACTION, "attack", ("target",))
OFF_HAND_ATTACK = ActionType("off_hand_attack", BONUS_ACTION, "off-hand attack", ("target",))
MANEUVER = ActionType("maneuver", ACTION, "maneuver", ("target",))
ITEM = ActionType("item", ACTION, "item use", ("item",))
SPELL = ActionType("spell", ACTION, "spell", ("spell",))
DODGE = ActionType("dodge", ACTION, "Dodge")
DASH = ActionType("dash", ACTION, "Dash")
DISENGAGE = ActionType("disengage", ACTION, "Disengage")
READY = ActionType("ready", ACTION, "Ready", ("trigger", "target"))
SECOND_WIND = ActionType("second_wind", BONUS_ACTION, "Second Wind")
RAGE = ActionType("rage", BONUS_ACTION, "Rage")
MOVE = ActionType("move", FREE, "move", ("destination",))
# A conditional clause ("if it drops, hit the other"). Not executed yet: see the question it asks.
CONTINGENCY = ActionType("contingency", FREE, "contingency", ("condition",))

REGISTRY: dict[str, ActionType] = {
    t.kind: t
    for t in (
        ATTACK,
        OFF_HAND_ATTACK,
        MANEUVER,
        ITEM,
        SPELL,
        DODGE,
        DASH,
        DISENGAGE,
        READY,
        SECOND_WIND,
        RAGE,
        MOVE,
        CONTINGENCY,
    )
}

# Spellings the parser may use for a registered kind. Anything else is the "unknown" kind.
_ALIASES = {
    "melee_attack": "attack",
    "ranged_attack": "attack",
    "weapon_attack": "attack",
    "offhand_attack": "off_hand_attack",
    "off-hand_attack": "off_hand_attack",
    "off_hand": "off_hand_attack",
    "grapple": "maneuver",
    "shove": "maneuver",
    "release": "maneuver",
    "use_item": "item",
    "item_use": "item",
    "cast": "spell",
    "cast_spell": "spell",
    "dodge_action": "dodge",
    "dash_action": "dash",
    "disengage_action": "disengage",
    "ready_action": "ready",
    "movement": "move",
    "if": "contingency",
    "conditional": "contingency",
}

UNKNOWN = "unknown"


def normalise_kind(raw: object) -> str:
    """The registered kind for a parser's spelling of it, or UNKNOWN (never a guess)."""
    key = str(raw or "").strip().lower().replace(" ", "_")
    key = _ALIASES.get(key, key)
    return key if key in REGISTRY else UNKNOWN


def label_of(kind: str) -> str:
    return REGISTRY[kind].label if kind in REGISTRY else "that action"


def agreement_question(
    proposed: Iterable[tuple[str, str | None]], detected: Collection[str], hero: str
) -> str | None:
    """One question when the parser proposed an action the engine did not detect, or an unknown one.

    `proposed` holds (kind, the player's words for it) pairs as the parser wrote them; `detected`
    holds the registered kinds the engine found in the player's words and state this turn. A kind
    that both sides agree on needs no question. Disagreement is never adopted silently.
    """
    unknown = [text for kind, text in proposed if kind == UNKNOWN]
    if unknown:
        return (
            f"I could not tell which action {hero} means by \"{unknown[0] or 'that'}\". Say it as one "
            "action this turn: attack, off-hand attack, a maneuver, an item, a spell, Dodge, Dash, "
            "Disengage, Ready, Second Wind, Rage, a move, or an if-then condition. No roll is made "
            "and no turn is spent."
        )
    missing = []
    for kind, _text in proposed:
        if kind not in detected and kind not in missing:
            missing.append(kind)
    if not missing:
        return None
    names = " and ".join(f"\"{label_of(k)}\"" for k in missing)
    return (
        f"I did not find {names} in what {hero} said, so I have not declared it. Say it in plain "
        "words if you mean it, or leave it out. No roll is made and no turn is spent."
    )


def completeness_question(
    actions: Iterable[object], foes: Collection[str], player_input: str, hero: str
) -> str | None:
    """One question when a proposed attack has no target among several standing foes.

    Only a typed proposal is checked here: the single-action path keeps its existing choice of the
    first foe, so its behaviour is unchanged. A foe the player's words name (any word of its name)
    completes the declaration, and so does the proposal's own target.
    """
    names = list(foes)
    if len(names) < 2:
        return None
    text = str(player_input or "").lower()
    if any(word in text for name in names for word in name.lower().split()):
        return None
    for action in actions:
        if getattr(action, "kind", None) in ("attack", "off_hand_attack", "maneuver") and not getattr(
            action, "target", None
        ):
            return (
                f"Which foe does {hero} mean: {' or '.join(names)}? No roll is made and no turn is "
                "spent."
            )
    return None
