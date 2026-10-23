"""The combat grid inside a round: the hero's route, foes' movement and attack ranges (combat grid).

`agent.battlefield` is the geometry; this module applies it to a round. The engine owns every square:
the player's words (with "move to D4") are planned here into a route
before any die is rolled, a route over the hero's speed is refused, and foes move by the rules below.
The band projection (`battlefield.sync_bands`) is refreshed after every move, so the rules written
against bands (reach, the within-5-feet penalty, readied attacks) read the grid.

Foe movement, on its own turn, before its attacks:
  * A foe that can already hit the hero from where it stands does not move.
  * A foe with a ranged attack moves toward the hero only as far as its normal range needs; a range
    its record does not state is not limited.
  * A melee foe moves up to its speed toward a square within its reach of the hero. When it cannot get
    there, it takes the Dash action (PHB p.192) and keeps coming, and makes no attack this turn.
Foes never provoke the hero: the engine runs no opportunity attack for the hero (see supported_rules).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from agent.battlefield import (
    Route,
    battlefield_of,
    distance_ft,
    in_bounds,
    label,
    melee_reach_ft,
    move_along,
    occupies,
    pos_of,
    provocations,
    route_away,
    route_to,
    route_toward,
    set_pos,
    speed_of,
    square_moves,
    sync_bands,
    weapon_range_ft,
    weapon_reach_ft,
)
from agent.combatants import can_act, is_standing, stable_id
from agent.monster_stats import MonsterAttack, attack_plan, stats_of
from agent.positioning import MELEE, MELEE_FEET, RANGED, MovePlan


def grid_active(combat_state: Mapping[str, Any] | None) -> bool:
    """True when the encounter has a battlefield (encounters stored before the grid keep bands)."""
    return battlefield_of(combat_state) is not None


def ensure_positions(combat_state: dict) -> None:
    """Give a square to any combatant without one (a hero who joined after the fight opened)."""
    battlefield = battlefield_of(combat_state)
    if battlefield is None:
        return
    combatants = combat_state.get("combatants") or []
    taken = {pos_of(c) for c in combatants if occupies(c)}
    walls = {(t["x"], t["y"]) for t in battlefield.get("terrain") or () if t.get("kind") == "wall"}
    for c in combatants:
        if pos_of(c) is not None:
            continue
        column = 0 if c.get("is_player") else int(battlefield["width"]) - 1
        for y in sorted(
            range(int(battlefield["height"])),
            key=lambda v: abs(v - int(battlefield["height"]) // 2),
        ):
            for x in (column, column + (1 if column == 0 else -1)):
                if (x, y) not in taken and (x, y) not in walls and in_bounds(battlefield, (x, y)):
                    set_pos(c, (x, y))
                    taken.add((x, y))
                    break
            if pos_of(c) is not None:
                break


def hero_budget(hero: Mapping[str, Any], *, dash: bool) -> int:
    """The hero's movement this turn: its speed, doubled by Dash (PHB p.192)."""
    return speed_of(hero) * (2 if dash else 1)


def plan_route(
    combat_state: Mapping[str, Any],
    hero: Mapping[str, Any],
    text: str | None,
    plan: MovePlan,
    foes: Sequence[Mapping[str, Any]],
    *,
    budget_ft: int,
    hero_name: str,
    dash: bool,
    start: tuple[int, int] | None = None,
) -> Route | str:
    """The hero's route for the moves the text declares, in the order stated, or the refusal.

    A close-in ends on the cheapest square touching its foe (or as near as the budget allows); a retreat
    ends on the square farthest from the foes it leaves; "move to D4" goes to that square, refused when
    it is blocked, taken or beyond the movement left. Pure: no row is changed.
    """
    battlefield = battlefield_of(combat_state)
    origin = start or pos_of(hero)
    if battlefield is None or origin is None:
        return Route(origin or (0, 0))
    combatants = list(combat_state.get("combatants") or [])
    legs: list[tuple[int, tuple[Any, ...]]] = [
        (pos, ("leg", band, target, feet)) for band, target, feet, pos in plan.legs
    ]
    for square, begin, end in square_moves(text):
        # "dash to H5" or "step to H5" also reads as a close-in: the named square is the move meant.
        legs = [leg for leg in legs if not begin <= leg[0] < end]
        legs.append((begin, ("square", square)))
    legs.sort(key=lambda leg: leg[0])
    squares: list[tuple[int, int]] = []
    spent = 0
    for _pos, leg in legs:
        at = squares[-1] if squares else origin
        left = budget_ft - spent
        if leg[0] == "square":
            dest = leg[1]
            if not in_bounds(battlefield, dest):
                return (
                    f"{label(dest)} is not on the battlefield. Choose a square on the grid: no roll is made "
                    "and no turn is spent."
                )
            step = route_to(battlefield, combatants, hero, dest, left, start=at)
            if step is None:
                full = route_to(battlefield, combatants, hero, dest, 1 << 20, start=at)
                if full is None:
                    return (
                        f"{hero_name} cannot end a move on {label(dest)}: the square is blocked or taken. "
                        "Choose another square: no roll is made and no turn is spent."
                    )
                hint = "" if dash else " The Dash action doubles it."
                return (
                    f"{label(dest)} is {full.cost_ft} feet of movement away and {hero_name} has {left} feet "
                    f"left this turn.{hint} Choose a nearer square: no roll is made and no turn is spent."
                )
        else:
            _kind, band, target, feet = leg
            budget = left if feet is None else min(int(feet), left)
            if band == MELEE:
                target_at = pos_of(target) if target is not None else None
                if target_at is None:
                    continue
                step = route_toward(battlefield, combatants, hero, target_at, budget, start=at)
            else:
                threats = (
                    [pos_of(target)]
                    if target is not None and pos_of(target) is not None
                    else [pos_of(f) for f in foes if occupies(f) and is_standing(f)]
                )
                step = route_away(
                    battlefield,
                    combatants,
                    hero,
                    [t for t in threats if t is not None],
                    budget,
                    start=at,
                )
        squares += step.squares
        spent += step.cost_ft
    return Route(origin, squares, spent)


def project(plan: MovePlan, route: Route, foes: Iterable[Mapping[str, Any]]) -> None:
    """Record the route on the plan, and the bands the foes will be in once the hero has moved.

    `steps` lists the foes the route brings closer (as melee steps, so the fear rule reads them as a
    move toward its source) and the foes it leaves (as ranged steps).
    """
    plan.route = route
    plan.distances = []
    closer: list[dict] = []
    left: list[dict] = []
    for foe in foes:
        at = pos_of(foe)
        if at is None:
            continue
        before = distance_ft(route.start, at)
        after = distance_ft(route.destination, at)
        plan.bands[id(foe)] = MELEE if after <= MELEE_FEET else RANGED
        gaps = [before, *[distance_ft(square, at) for square in route.squares]]
        if any(b < a for a, b in zip(gaps, gaps[1:], strict=False)):
            closer.append(foe)  # type: ignore[arg-type]
        elif before <= MELEE_FEET < after:
            left.append(foe)  # type: ignore[arg-type]
    plan.steps = ([(MELEE, closer)] if closer else []) + ([(RANGED, left)] if left else [])


def attack_reach_check(
    hero_name: str,
    target: Mapping[str, Any],
    *,
    from_square: tuple[int, int],
    weapon_name: str | None,
    ranged: bool,
) -> tuple[str | None, bool]:
    """(refusal or None, long range) for the hero's weapon attack from `from_square`.

    A melee attack reaches 5 feet (10 with a reach weapon); a ranged weapon reaches its long range, with
    disadvantage beyond its normal range (PHB p.195).
    """
    at = pos_of(target)
    if at is None:
        return None, False
    gap = distance_ft(from_square, at)
    name = str(target.get("name") or "the foe")
    if not ranged:
        reach = weapon_reach_ft(weapon_name)
        if gap > reach:
            return (
                f"{name} is {gap} feet away and {hero_name}'s melee reach is {reach} feet. Move first: "
                f"declare 'I move next to {name}' (or pick a square on the grid) and then the attack. No "
                "roll is made and no turn is spent."
            ), False
        return None, False
    span = weapon_range_ft(weapon_name)
    if span is None:
        return None, False
    if gap > span[1]:
        return (
            f"{name} is {gap} feet away, beyond the {span[1]}-foot long range of {hero_name}'s "
            f"{weapon_name}. Move closer first: no roll is made and no turn is spent."
        ), False
    return None, gap > span[0]


def walk_hero(turns: Any, route: Route, *, disengage: bool) -> Route:
    """Move the hero along its route. Each foe whose reach it leaves may make its opportunity attack
    (PHB p.195), unless the hero took Disengage. A hero dropped by one stops where it was hit.

    Returns the route actually walked.
    """
    rec = turns.rec
    hero = rec.player
    combatants = rec.combatants
    if not route.moved:
        return route
    stop: int | None = None
    if not disengage:
        for index, foe in provocations(
            combatants,
            hero,
            route,
            can_react=lambda f: (
                can_act(f) and not f.get("surprised") and not f.get("reaction_used")
            ),
        ):
            if rec.hp <= 0:
                break
            move_along(hero, route, stop_at=index)
            turns.opportunity_attack(foe)
            if rec.hp <= 0:
                stop = index
                break
    walked = route if stop is None else Route(route.start, route.squares[:stop], 0)
    if stop is not None:
        walked = _costed(rec.combat_state, hero, walked)
    move_along(hero, walked)
    sync_bands(rec.combat_state, hero)
    line = _move_line(rec.hero, walked, stopped=stop is not None)
    rec.log_lines.append(line)
    rec.events.append({"kind": "move", "text": line, "actor_id": stable_id(hero)})
    return walked


def _costed(combat_state: Mapping[str, Any], mover: Mapping[str, Any], route: Route) -> Route:
    if not route.squares:
        return route
    battlefield = battlefield_of(combat_state)
    full = route_to(
        battlefield,
        list(combat_state.get("combatants") or []),
        mover,
        route.destination,
        1 << 20,
        start=route.start,
    )  # type: ignore[arg-type]
    return Route(route.start, route.squares, full.cost_ft if full else route.cost_ft)


def _move_line(name: str, route: Route, *, stopped: bool = False, dash: bool = False) -> str:
    if not route.moved:
        return f"{name} holds position at {label(route.start)}."
    verb = "dashes" if dash else "moves"
    text = f"{name} {verb} {route.cost_ft} feet from {label(route.start)} to {label(route.destination)}"
    return text + (", and is stopped there." if stopped else ".")


def _ranged_attacks(plan: Sequence[MonsterAttack]) -> list[MonsterAttack]:
    return [a for a in plan if a.ranged and a.save is None]


def position_foe(turns: Any, e: dict) -> bool:
    """Move a foe on its own turn (see the module doc). False when it Dashed and makes no attack."""
    rec = turns.rec
    combat_state = rec.combat_state
    battlefield = battlefield_of(combat_state)
    hero = rec.player
    if battlefield is None or pos_of(e) is None or pos_of(hero) is None:
        return True
    sync_bands(combat_state, hero)
    target = pos_of(hero)
    assert target is not None
    stats = stats_of(e)
    plan = attack_plan(stats, spent=e.get("recharge_spent") or ()) if stats is not None else ()
    reach = melee_reach_ft(e, stats)
    has_melee = stats is None or any(not a.ranged for a in plan)
    ranged = _ranged_attacks(plan)
    gap = distance_ft(pos_of(e), target)  # type: ignore[arg-type]
    if has_melee and gap <= reach:
        return True
    speed = speed_of(e)
    combatants = rec.combatants
    was_ranged = gap > MELEE_FEET
    if ranged:
        spans = [a.range_ft for a in ranged]
        if any(span is None for span in spans):
            return True  # a range the record does not state is not limited
        normal = max(span[0] for span in spans if span is not None)
        if gap <= normal:
            return True
        route = route_toward(battlefield, combatants, e, target, speed, within_ft=normal)
        _walk_foe(rec, e, route, was_ranged=was_ranged, turns=turns)
        return True
    route = route_toward(battlefield, combatants, e, target, speed, within_ft=reach)
    if distance_ft(route.destination, target) <= reach:
        _walk_foe(rec, e, route, was_ranged=was_ranged, turns=turns)
        return True
    dash = route_toward(battlefield, combatants, e, target, speed * 2, within_ft=reach)
    _walk_foe(rec, e, dash, was_ranged=was_ranged, turns=turns, dash=True)
    # A Dash that still reaches is the same as walking: it spends the action, so no attack follows.
    rec.log_lines.append(f"{e['name']} takes the Dash action and makes no attack this turn.")
    return False


def _walk_foe(
    rec: Any, e: dict, route: Route, *, was_ranged: bool, turns: Any, dash: bool = False
) -> None:
    move_along(e, route)
    sync_bands(rec.combat_state, rec.player)
    line = _move_line(str(e["name"]), route, dash=dash)
    rec.log_lines.append(line)
    if route.moved:
        rec.events.append({"kind": "enemy_move", "text": line, "actor_id": stable_id(e)})
    if route.moved and "foe_moved" not in rec.round_effects:
        rec.round_effects.append("foe_moved")
    if was_ranged and e.get("band") == MELEE:
        turns._foe_closed(e)


def foe_attack_range(
    e: Mapping[str, Any], hero: Mapping[str, Any], attack: MonsterAttack
) -> tuple[bool, bool]:
    """(in range, long range) for one of a foe's attacks against the hero on the grid.

    A save action is not range-limited here; a melee attack needs its reach; a ranged attack its long
    range, at disadvantage beyond its normal range. Off the grid every attack is in range.
    """
    here, there = pos_of(e), pos_of(hero)
    if here is None or there is None or attack.save is not None:
        return True, False
    gap = distance_ft(here, there)
    if not attack.ranged:
        return gap <= int(attack.reach_ft or MELEE_FEET), False
    if attack.range_ft is None:
        return True, False
    return gap <= attack.range_ft[1], gap > attack.range_ft[0]


def fly_away(rec: Any, e: dict) -> None:
    """A legendary Wing Attack's flight: the foe moves away from the hero, up to its speed."""
    battlefield = battlefield_of(rec.combat_state)
    if battlefield is None or pos_of(e) is None or pos_of(rec.player) is None:
        return
    route = route_away(battlefield, rec.combatants, e, [pos_of(rec.player)], speed_of(e))  # type: ignore[list-item]
    move_along(e, route)
    sync_bands(rec.combat_state, rec.player)


def bands_for(combat_state: Mapping[str, Any], hero: Mapping[str, Any] | None) -> None:
    """Refresh the band projection for `hero` (a no-op off the grid)."""
    if grid_active(combat_state):
        sync_bands(combat_state, hero)
