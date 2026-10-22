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
(PDF p.196). A reaction is taken once and comes back at the start of the creature's own next turn
(PDF p.191). The Disengage action stops the provocation for the rest of the turn (PDF p.193).

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
    r"get\s+(?:in\s+)?(?:close|next)\s+to|move\s+(?:back\s+)?(?:in\s+)?(?:next\s+to|into\s+(?:melee|reach)))\b"
    r"|\bdash\s+(?:in\s+)?(?:toward|towards|to|at|into)\b",
    re.IGNORECASE,
)
# "move 10 feet toward the goblin" and "move 10 feet away": the movement word with its direction.
_FEET_NUMBER = r"(?:\d+\s*(?:feet|foot|ft\.?)\s+)?"
_CLOSE_IN_FEET = re.compile(
    r"\bmove\s+" + _FEET_NUMBER + r"(?:toward|towards|closer\s+to)\b", re.IGNORECASE
)
_RETREAT_FEET = re.compile(r"\bmove\s+" + _FEET_NUMBER + r"away\b", re.IGNORECASE)
_FEET = re.compile(r"(\d+)\s*(?:feet|foot|ft\b)", re.IGNORECASE)
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
    """The first melee attack listed: the attack an opportunity attack makes (PDF p.196)."""
    return next((a for a in stats.attacks if not a.ranged), None)


def attacks_from_band(plan: Sequence[MonsterAttack], band: str) -> tuple[MonsterAttack, ...]:
    """The attacks a foe makes from its band: at melee its melee attacks and save actions (ranged
    attacks only when it has no melee one); at range only its ranged attacks. A melee-only foe at
    range has none here."""
    if band == RANGED:
        return tuple(a for a in plan if a.ranged)
    melee = tuple(a for a in plan if not a.ranged or a.save)
    return melee or tuple(plan)


# The hero's speed in feet (PHB p.192: most heroes have 30 feet). The sheet records no speed, so the
# engine uses this value for every hero and says so in the contract; Dash doubles it (PHB p.193).
HERO_SPEED_FEET = 30
# The distance a foe at range is taken to stand, when no move has recorded its distance (a ruling:
# encounters record bands, not distances). A foe at melee stands within 5 feet.
RANGED_START_FEET = 30
MELEE_FEET = 5


def movement_steps(text: str | None) -> tuple[tuple[str, int], ...]:
    """The hero's declared moves as (destination band, position in the text), in the order stated."""
    body = str(text or "")
    steps = [(MELEE, m.start()) for m in _CLOSE_IN.finditer(body)]
    steps += [(MELEE, m.start()) for m in _CLOSE_IN_FEET.finditer(body)]
    steps += [(RANGED, m.start()) for m in _RETREAT.finditer(body)]
    steps += [(RANGED, m.start()) for m in _RETREAT_FEET.finditer(body)]
    return tuple(sorted(_unique_steps(steps), key=lambda step: step[1]))


def _unique_steps(steps: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """One step per position: "move 10 feet toward" matches both the feet and the plain pattern."""
    seen: dict[int, tuple[str, int]] = {}
    for band, pos in steps:
        seen.setdefault(pos, (band, pos))
    return list(seen.values())


def movement_legs(text: str | None) -> tuple[tuple[str, int, int | None], ...]:
    """Each declared move with the feet it names: (destination band, position, feet or None).

    A move's feet are the first "N feet" between its position and the next move's.
    """
    body = str(text or "")
    steps = movement_steps(body)
    legs = []
    for index, (band, pos) in enumerate(steps):
        end = steps[index + 1][1] if index + 1 < len(steps) else len(body)
        feet = _FEET.search(body[pos:end])
        legs.append((band, pos, int(feet.group(1)) if feet else None))
    return tuple(legs)


def feet_refusal(hero: str, feet: int, *, dash: bool) -> str | None:
    """The refusal when the declared feet exceed the speed budget. None when they fit."""
    budget = HERO_SPEED_FEET * (2 if dash else 1)
    if feet <= budget:
        return None
    how = "with Dash" if dash else "at normal speed"
    return (
        f"{hero} can move {budget} feet {how} this turn (speed {HERO_SPEED_FEET} feet"
        f"{', doubled by Dash' if dash else ''}), and this turn declares {feet} feet. Choose a shorter "
        "move: no roll is made and no turn is spent."
    )


def distance_of(foe: Mapping[str, Any], band: str) -> int:
    """A foe's recorded distance in feet, or the distance its band implies when none is recorded."""
    recorded = foe.get("distance_ft")
    if isinstance(recorded, int) and not isinstance(recorded, bool):
        return recorded
    return MELEE_FEET if band == MELEE else RANGED_START_FEET


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
    """A creature's reaction comes back at the start of its own turn (PDF p.191)."""
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
    # Foe rows whose recorded distance a feet move changes, with the distance after the move.
    distances: list[tuple[dict, int]] = field(default_factory=list)


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
    for band, pos, feet in movement_legs(text):
        after = [m for m in mentions if m[0] > pos]
        before = [m for m in mentions if m[0] <= pos]
        chosen = (
            min(after, key=lambda m: m[0])
            if after
            else (max(before, key=lambda m: m[0]) if before else None)
        )
        target = chosen[1] if chosen else None
        if feet is not None:
            # A move in feet is measured from the foe's recorded distance (see `distance_of`).
            if target is None and len(foes) == 1:
                target = foes[0]
            if target is None:
                names = ", ".join(str(f.get("name")) for f in foes)
                plan.question = f"Which foe do you move {'toward' if band == MELEE else 'away from'}? ({names})"
                return plan
            current = next(
                (d for row, d in plan.distances if row is target),
                distance_of(target, bands[id(target)]),
            )
            # Closing stops at the melee boundary: the hero does not move into the foe's space.
            after_move = max(MELEE_FEET, current - feet) if band == MELEE else current + feet
            plan.distances = [(row, d) for row, d in plan.distances if row is not target]
            plan.distances.append((target, after_move))
            new_band = MELEE if after_move <= MELEE_FEET else RANGED
            if bands[id(target)] != new_band:
                bands[id(target)] = new_band
                plan.steps.append((new_band, [target]))
            continue
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
            # A coarse band change invalidates an earlier measured scalar. Persist a consistent
            # band-derived distance rather than retain (for example) 5 feet at ranged distance.
            if "distance_ft" in foe:
                plan.distances = [(row, d) for row, d in plan.distances if row is not foe]
                plan.distances.append((foe, MELEE_FEET if band == MELEE else RANGED_START_FEET))
        if changing:
            plan.steps.append((band, changing))
    return plan
