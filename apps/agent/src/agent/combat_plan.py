"""Order and multiplicity of engine-recognized operations, before any turn is committed."""

from __future__ import annotations

import re
from collections import Counter

from agent.action_economy import BONUS_ACTION, Declared, affirmative_matches
from agent.positioning import movement_steps

_WORDS = {
    "attack": r"\b(?:attack\w*|strik\w*|stab\w*|swing\w*|slash\w*|shoot\w*|lunge\w*|smash\w*|thrust\w*|hit|bash\w*|punch\w*|kick\w*|throw\w*|hurl\w*)\b",
    "second_wind": r"\bsecond\s+wind\b",
    "rage": r"\brage\b",
    "disengage": r"\bdisengag\w*\b",
    "dash": r"\bdash\w*\b",
    "dodge": r"\bdodg\w*\b",
    "ready": r"\bready\b",
    "off_hand_attack": r"\boff[- ]?hand\b|\bother\s+(?:hand|weapon|blade)\b",
    "maneuver": r"\b(?:grappl\w*|pin\w*|shov\w*|knock\w*|releas\w*)\b",
    "item": r"\b(?:drink\w*|use|consume\w*)\b",
}


def operation_order(declared: list[Declared], moving: bool, parsed, text: str) -> tuple[str, ...]:
    """Order comes from the player's text; parser list order alone cannot rewrite it."""
    positioned = []
    for index, action in enumerate(declared):
        pattern = _WORDS.get(action.kind)
        matches = affirmative_matches(re.compile(pattern, re.I), text) if pattern else []
        position = matches[0].start() if matches else len(text) + index
        if not matches:
            for proposal in getattr(parsed, "actions", None) or []:
                snippet = getattr(proposal, "text", None)
                if proposal.kind == action.kind and snippet and snippet.lower() in text.lower():
                    position = text.lower().index(snippet.lower())
                    break
        positioned.append((position, index, action.kind))
    moves = movement_steps(text)
    if moving:
        positioned.append((moves[0][1] if moves else 0, -1, "move"))
    return tuple(kind for _, _, kind in sorted(positioned))


def sequence_question(order: tuple[str, ...], text: str, hero: str) -> str | None:
    """Do not collapse movement around an action into one differently ordered move."""
    moves = movement_steps(text)
    if len(moves) > 1:
        for kind, pattern in _WORDS.items():
            if kind not in order or kind == "off_hand_attack":
                continue
            if any(moves[0][1] < m.start() < moves[-1][1] for m in re.finditer(pattern, text, re.I)):
                return (
                    f"{hero}'s move before and after an action needs separate movement steps. "
                    "Choose one move before or after the action, or a supported if-then move. "
                    "No roll is made and no turn is spent."
                )
    if "off_hand_attack" in order and "attack" in order and order.index("off_hand_attack") < order.index("attack"):
        return "The off-hand attack follows the Attack action. Declare the main attack first, then the off-hand attack. No roll is made and no turn is spent."
    return None


def attack_count_question(text: str, parsed, *, available: int, hero: str) -> str | None:
    """Repeated attacks cannot disappear when engine detection produces a single attack kind."""
    counts = Counter(a.kind for a in getattr(parsed, "actions", None) or [])
    attack_matches = affirmative_matches(re.compile(_WORDS["attack"], re.I), text)
    requested = max(counts["attack"], len(attack_matches))
    targets = {a.target.casefold() for a in getattr(parsed, "actions", None) or [] if a.kind == "attack" and a.target}
    if len(targets) > 1:
        return "Separate attacks against different targets need separate declarations. Choose one target; no roll is made and no turn is spent."
    if len(attack_matches) > 1:
        between = text[attack_matches[0].end():attack_matches[-1].start()]
        if movement_steps(between) or any(re.search(pattern, between, re.I) for kind, pattern in _WORDS.items() if kind not in ("attack", "off_hand_attack")):
            return "Attacks with another operation between them need separate declarations. Group your attacks before or after that operation; no roll is made and no turn is spent."
    if re.search(r"\b(?:twice|two\s+(?:times|attacks)|2\s+(?:times|attacks))\b", text, re.I):
        requested = max(requested, 2)
    if re.search(r"\b(?:three|3)\s+(?:times|attacks)\b", text, re.I):
        requested = max(requested, 3)
    # An off-hand bonus attack is already represented by its own kind, not another Attack action.
    if requested > available:
        return (
            f"{hero} requested {requested} attacks, but this declaration allows {available}. "
            "Choose one attack, or explicitly declare an available Action Surge or a legal off-hand "
            "attack. No roll is made and no turn is spent."
        )
    return None


def phase_for(kind: str, declared: list[Declared]) -> str:
    if kind == "move":
        return "move"
    action = next((d for d in declared if d.kind == kind), None)
    return "bonus" if action is not None and action.slot == BONUS_ACTION else "action"
