"""What the player's own text declares: leaving the fight, the named foes, and attack wording.

Pure text reading (EDG-057 split from graph_combat). It rules nothing: a declared flight or target
still goes through the round's checks in `agent.graph_combat`.
"""

from __future__ import annotations

import re
from typing import Any

from agent.resolution import normalise_check
from agent.state import AgentState
from db.vector.references import reference_matches

# First-person intent to leave the fight. Requires a flee verb with nothing aimed *at* someone,
# so "I run at the goblin" / "I run toward the door to block it" are attacks/moves, not flight.
_FLEE_NEGATION = re.compile(
    r"\b(?:i|we)\s+(?:won't|will not|don't|do not|never|refuse to|can't|cannot)\s+"
    r"(?:\w+\s+)?(?:flee|run|retreat|escape|surrender|give up)",
    re.IGNORECASE,
)
_FLEE_PATTERNS = (
    re.compile(
        # Disengage (EDG-044) and a retreat to range or away are moves, not leaving the fight.
        r"\b(?:i|we)\s+(?:try\s+to\s+|attempt\s+to\s+|decide\s+to\s+)?"
        r"(?:flee|surrender|give\s+up|yield|retreat(?!\s+(?:to\s+range|from|away|out\s+of\s+reach))"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:try\s+to\s+|attempt\s+to\s+)?escape\b(?!\s+(?:from\s+)?"
        r"(?:the\s+|his\s+|her\s+|its\s+|their\s+)?(?:grapple|grip|hold|net|web|bonds|ropes|chains|shackles))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:turn\s+and\s+)?run\s+(?:away|off|for\s+(?:it|my\s+life|our\s+lives|the\s+exit))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:i|we)\s+(?:turn\s+and\s+)?run\s*[.!]*\s*$", re.IGNORECASE),
    re.compile(r"\brun\s+away\b|\bback\s+away\s+and\s+run\b|\bmake\s+a\s+run\s+for\s+it\b", re.IGNORECASE),
)


_GENERIC_TARGETS = {
    "", "enemy", "enemies", "foe", "foes", "them", "it", "him", "her", "monster", "creature",
    "target", "opponent", "the enemy", "nearest enemy", "closest enemy", "someone",
}  # fmt: skip


def is_flee(text: str | None) -> bool:
    """True when the player declares leaving the fight (not 'I run at the goblin')."""
    if not text:
        return False
    if _FLEE_NEGATION.search(text):
        return False
    return any(p.search(text) for p in _FLEE_PATTERNS)


def _named_targets(state: AgentState) -> list[str]:
    parsed = state.get("parsed_input")
    names: list[str] = []
    if parsed is not None and getattr(parsed, "target", None):
        names.append(str(parsed.target))
    entities = getattr(parsed, "entities", None) or {}
    raw = entities.get("targets", entities.get("target")) if isinstance(entities, dict) else None
    if isinstance(raw, str):
        names.append(raw)
    elif isinstance(raw, list):
        names += [str(t) for t in raw if t]
    out: list[str] = []
    for n in names:
        n = n.strip()
        if n.lower() not in _GENERIC_TARGETS and n.lower() not in (o.lower() for o in out):
            out.append(n[:60])
    return out


def _name_matches(a: str, b: str) -> bool:
    return reference_matches(a, b)


_RANGED_HINT = re.compile(r"\b(?:shoot|shoots|fire|fires|loose|looses)\b", re.IGNORECASE)
_STAND_UP = re.compile(
    r"\b(?:stand(?:s|ing)?\s+up|get(?:s|ting)?\s+up|rise(?:s|n)?\s+to\s+(?:my|their|his|her|your)\s+feet)\b",
    re.IGNORECASE,
)


def _is_attack_action(parsed: Any, named: list[str]) -> tuple[bool, bool]:
    """(is_attack, is_spell)."""
    norm = normalise_check(getattr(parsed, "check", None)) if parsed is not None else None
    if norm is not None:
        return norm[1] in ("attack", "spell_attack"), norm[1] == "spell_attack"
    if parsed is None:
        return True, False
    return (getattr(parsed, "intent", "combat") == "combat" or bool(named)), False
