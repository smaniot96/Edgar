"""Coarse positioning and movement (EDG-044, second slice).

Each foe stands in one of two distance bands from the hero: `melee` (within 5 feet) or `ranged`
(beyond 5 feet). The hero is the reference point and carries no band. Exact distances, cover,
flanking, high ground and difficult terrain are not modelled (see the contract in
`agent.supported_rules`).

Stored on each foe as `band` (a missing or legacy value reads as melee) and `reaction_used` (a
missing value reads as unused). Both are additive JSONB keys on the combatant row.

Movement is not an action. The hero may change band once per turn at normal speed; the Dash
action allows a second change in the same turn (2014 PHB, p.192). A creature that leaves a hostile
creature's melee reach provokes one opportunity attack from that creature, made with its reaction
(PHB p.195). A reaction is taken once and comes back at the start of the creature's own next turn
(PHB p.190). The Disengage action stops the provocation for the rest of the turn (PHB p.192).

Ranged attacks made within 5 feet of a hostile creature that can see you have disadvantage (PHB
p.195), so a ranged attack made from the melee band uses the `within_five_feet` source of
`agent.combatants.attack_roll_mode`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.monster_stats import MonsterAttack, MonsterStats

MELEE = "melee"
RANGED = "ranged"

# Movement the hero declares. A close-in needs a foe; a retreat may name none (every foe in melee).
_CLOSE_IN = re.compile(
    r"\b(?:close\s+in|closes?\s+in|"
    r"charg(?:e|es|ed|ing)\b(?!\s+(?:my|your|his|her|its|their|the\s+(?:spell|sword|blade|weapon|"
    r"staff|bow|axe|mace|shield|rope)s?)\b)|"
    r"advance\s+(?:on|toward|towards)|step\s+(?:in\s+)?(?:up\s+)?to|"
    r"get\s+(?:in\s+)?(?:close|next)\s+to|move\s+(?:in\s+)?(?:next\s+to|into\s+(?:melee|reach)))\b"
    r"|\bdash\s+(?:in\s+)?(?:toward|towards|to|at|into)\b",
    re.IGNORECASE,
)
_RETREAT = re.compile(
    r"\b(?:backs?\s+(?:away|off)|retreats?|retreating|fall(?:s|ing)?\s+back|withdraw(?:s|ing)?|"
    r"keep(?:s|ing)?\s+(?:my|our)\s+distance|step(?:s|ping)?\s+back|move\s+away|pull(?:s|ing)?\s+back)\b"
    r"|\bdash\s+(?:away|back|out)\b",
    re.IGNORECASE,
)


def band_of(c: Mapping[str, Any]) -> str:
    """A foe's band. Rows without one (legacy encounters) read as melee."""
    return RANGED if c.get("band") == RANGED else MELEE


def initial_band(stats: MonsterStats | None) -> str:
    """Where a foe starts: at range only when every attack its record lists is a ranged attack."""
    if stats is not None and stats.attacks and all(a.ranged for a in stats.attacks):
        return RANGED
    return MELEE


def primary_melee_attack(stats: MonsterStats) -> MonsterAttack | None:
    """The first melee attack listed: the attack an opportunity attack makes (PHB p.195)."""
    return next((a for a in stats.attacks if not a.ranged), None)


def attacks_from_band(plan: Sequence[MonsterAttack], band: str) -> tuple[MonsterAttack, ...]:
    """The attacks a foe makes from its band: at melee its melee attacks and save actions (ranged
    attacks only when it has no melee one); at range only its ranged attacks. A melee-only foe at
    range has none here."""
    if band == RANGED:
        return tuple(a for a in plan if a.ranged)
    melee = tuple(a for a in plan if not a.ranged or a.save)
    return melee or tuple(plan)


def movement_steps(text: str | None) -> tuple[tuple[str, int], ...]:
    """The hero's declared moves as (destination band, position in the text), in the order stated."""
    body = str(text or "")
    steps = [(MELEE, m.start()) for m in _CLOSE_IN.finditer(body)]
    steps += [(RANGED, m.start()) for m in _RETREAT.finditer(body)]
    return tuple(sorted(steps, key=lambda step: step[1]))


def move_refusal(hero: str, changes: int, *, dash: bool) -> str | None:
    """The refusal when the declared moves change band more often than the turn allows. None if fine."""
    allowed = 2 if dash else 1
    if changes <= allowed:
        return None
    if dash:
        return (
            f"{hero} can change position at most twice in one turn, even with Dash, and this turn "
            f"declares {changes} moves. Choose fewer moves: no roll is made and no turn is spent."
        )
    return (
        f"{hero} can change position once per turn at normal speed, and this turn declares "
        f"{changes} moves. The Dash action allows a second move: no roll is made and no turn is spent."
    )


def reach_refusal(hero: str, foe: str) -> str:
    """The refusal when a melee attack or grapple is aimed at a foe beyond 5 feet."""
    return (
        f"{hero}'s melee attack cannot reach {foe}, which stands beyond 5 feet. Move first: declare "
        f"'I close in on {foe}' and then the attack. No roll is made and no turn is spent."
    )


def start_turn(c: dict) -> None:
    """A creature's reaction comes back at the start of its own turn (PHB p.190)."""
    c["reaction_used"] = False


def band_text(c: Mapping[str, Any]) -> str:
    """The narrator's description of a foe's band; it states what the engine recorded."""
    if band_of(c) == RANGED:
        return "beyond 5 feet of the hero"
    return "within 5 feet of the hero"


@dataclass
class MovePlan:
    """The hero's declared moves, planned on a copy of the bands. No foe row is changed by planning.

    `steps` are (destination band, foes that change band in that step), in the order stated.
    `bands` maps id(foe) to the band after every step; a refusal reads the changes from it.
    `question` asks the player for the foe a close-in means when several are at range.
    """

    steps: list[tuple[str, list[dict]]] = field(default_factory=list)
    bands: dict[int, str] = field(default_factory=dict)
    question: str | None = None


def plan_moves(
    text: str | None,
    named: Sequence[str],
    foes: Sequence[dict],
    *,
    pick: Callable[[str], dict | None],
) -> MovePlan:
    """Plan the moves the text declares against the living foes. `pick` resolves a named foe.

    A move aimed at a named foe is the foe nearest after the move's words (else before them). A
    retreat with no foe named moves every foe in melee to range. A close-in with no foe named is
    for the one foe at range; with several it is a question, not a guess.
    """
    lower = str(text or "").lower()
    bands = {id(f): band_of(f) for f in foes}
    plan = MovePlan(bands=bands)
    # Every foe the text names, by its own name ("goblin" is not the "goblin 2" in the same text).
    mentions: list[tuple[int, dict]] = []
    for foe in foes:
        name = str(foe.get("name") or "").lower()
        if name:
            pattern = rf"(?<!\w){re.escape(name)}(?!\w)(?!\s+\d)"
            mentions += [(m.start(), foe) for m in re.finditer(pattern, lower)]
    for n in named:
        at = lower.find(str(n).lower()) if n else -1
        named_foe = pick(str(n)) if at >= 0 else None
        if named_foe is not None:
            mentions.append((at, named_foe))
    for band, pos in movement_steps(text):
        after = [m for m in mentions if m[0] > pos]
        before = [m for m in mentions if m[0] <= pos]
        chosen = (
            min(after, key=lambda m: m[0])
            if after
            else (max(before, key=lambda m: m[0]) if before else None)
        )
        target = chosen[1] if chosen else None
        if target is not None:
            changing = [target] if bands[id(target)] != band else []
        elif band == RANGED:
            changing = [f for f in foes if bands[id(f)] == MELEE]
        else:
            at_range = [f for f in foes if bands[id(f)] == RANGED]
            if len(at_range) > 1:
                names = ", ".join(str(f.get("name")) for f in at_range)
                plan.question = f"Which foe do you close in on? ({names})"
                return plan
            changing = at_range
        for foe in changing:
            bands[id(foe)] = band
        if changing:
            plan.steps.append((band, changing))
    return plan
