"""The tactical battlefield: a square grid the engine owns (combat grid).

A fight opened with a battlefield places every combatant on a grid of 5-foot squares (PHB p.192,
"Playing on a Grid"). The grid is engine state, not narration: the model never chooses a square, a
distance or a path. Everything here is pure; callers persist the result on `combat_state`.

Stored state (all additive):
  * `combat_state["battlefield"]`: `{"version", "width", "height", "theme", "terrain": [{x, y, kind,
    feature}]}`. `kind` is `wall` (blocks movement and line of travel) or `difficult` (each square
    costs 10 feet, PHB p.190). `feature` is the scenery word shown to the player ("rubble").
  * `combatant["pos"]`: `[x, y]`, 0-based, x to the east and y to the south. Squares are labelled
    like a chess board: column letter then row number, so `[0, 0]` is A1 and `[3, 4]` is D5.
  * `combatant["speed_ft"]`: the walking speed the engine moves the creature with.

Rules (2014 PHB):
  * Distance is counted in squares, diagonals included, 5 feet each (the PHB grid rule; the DMG's
    optional 5-10-5 diagonal is not used). A creature is "within 5 feet" of another when their
    squares touch.
  * Moving into a difficult square, or through an ally's square, costs 10 feet (PHB p.190-191). A
    hostile creature's square cannot be entered, and no move may end in another creature's square.
    A diagonal step may not squeeze between two walls.
  * Speed: heroes 30 feet (`positioning.HERO_SPEED_FEET`; the sheet records no speed). Foes use the
    first walking speed of their record ("30 ft., fly 60 ft." walks 30; a creature that only flies
    moves at its flying speed), else 30. Dash doubles the budget (PHB p.192).
  * Leaving a hostile creature's reach provokes its opportunity attack (PHB p.195); see
    `provocations`. Reach is 5 feet, or the attack's recorded reach (a glaive or a "reach 10 ft."
    stat line).
  * Ranged weapons have a normal and a long range (PHB p.146-147). Beyond long range an attack is
    impossible; beyond normal range it has disadvantage (PHB p.195). A range the record does not state
    is not invented: such an attack is not range-limited.

Range bands (EDG-044b) are kept as a projection of the grid, so every rule written against them keeps
its meaning: a foe is in the `melee` band when its square touches the hero's, else `ranged`, and its
`distance_ft` is the grid distance. `sync_bands` writes that projection relative to the hero the
engine is acting for. Encounters stored before the grid (no battlefield) keep the abstract bands.

Not modelled: creature size (every creature fills one square), cover and line of sight, flying,
climbing and swimming movement, forced movement, and area shapes.
"""

from __future__ import annotations

import hashlib
import heapq
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent.action_economy import affirmative_matches
from agent.combatants import disposition_of, has_effect, is_standing
from agent.monster_stats import MonsterAttack, MonsterStats, stats_of
from agent.positioning import HERO_SPEED_FEET, MELEE, MELEE_FEET, RANGED, RANGED_START_FEET

BATTLEFIELD_KEY = "battlefield"
POS_KEY = "pos"
SPEED_KEY = "speed_ft"
SQUARE_FT = 5
VERSION = 1
WALL = "wall"
DIFFICULT = "difficult"

Square = tuple[int, int]

_NEIGHBOURS: tuple[Square, ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)

# PHB weapon table ranges (normal, long) in feet, for ranged and thrown weapons (PHB p.149).
WEAPON_RANGES: dict[str, tuple[int, int]] = {
    "shortbow": (80, 320),
    "longbow": (150, 600),
    "hand crossbow": (30, 120),
    "heavy crossbow": (100, 400),
    "light crossbow": (80, 320),
    "crossbow": (80, 320),
    "sling": (30, 120),
    "dart": (20, 60),
    "blowgun": (25, 100),
    "dagger": (20, 60),
    "handaxe": (20, 60),
    "javelin": (30, 120),
    "spear": (20, 60),
    "light hammer": (20, 60),
    "trident": (20, 60),
}
# Weapons with the reach property add 5 feet to the wielder's reach (PHB p.147).
REACH_WEAPONS = frozenset({"glaive", "halberd", "pike", "whip", "lance"})

# Scenery by scene words: (blocking feature, difficult feature). The first theme a word matches wins.
THEMES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "forest",
        ("forest", "wood", "woods", "grove", "thicket", "glade", "trees"),
        "tree",
        "undergrowth",
    ),
    ("swamp", ("swamp", "marsh", "bog", "fen", "mire"), "dead tree", "bog"),
    (
        "cave",
        (
            "cave",
            "cavern",
            "tunnel",
            "mine",
            "grotto",
            "underground",
            "dungeon",
            "crypt",
            "tomb",
            "lair",
            "sewer",
        ),
        "rock pillar",
        "rubble",
    ),
    (
        "ruins",
        (
            "ruin",
            "ruins",
            "temple",
            "shrine",
            "tower",
            "castle",
            "keep",
            "fort",
            "hall",
            "chapel",
            "manor",
        ),
        "broken pillar",
        "rubble",
    ),
    (
        "town",
        (
            "town",
            "village",
            "street",
            "alley",
            "market",
            "tavern",
            "inn",
            "dock",
            "docks",
            "harbor",
            "warehouse",
            "city",
        ),
        "crates",
        "debris",
    ),
)
DEFAULT_THEME = ("open", "boulder", "brush")

_SPEED_PART = re.compile(r"(?:\b(fly|swim|climb|burrow)\s+)?(\d+)\s*(?:ft|feet)", re.IGNORECASE)
# "move to D4", "step into c7", "go to square B2": a destination square the player names.
_SQUARE_MOVE = re.compile(
    r"\b(?:move|moves|moving|step|steps|stepping|go|goes|going|walk|walks|run|runs|dash|dashes|"
    r"advance|advances|retreat|retreats|head|heads|reposition|repositions)\b[^.;!?]{0,24}?"
    r"\b(?:to|into|onto|toward|towards)\s+(?:square\s+)?([A-Za-z])\s?(\d{1,2})\b",
    re.IGNORECASE,
)


# --- squares -------------------------------------------------------------------------------------


def label(square: Sequence[int]) -> str:
    """The square's name: column letter then row number (A1 is the north-west corner)."""
    x, y = int(square[0]), int(square[1])
    return f"{chr(ord('A') + x)}{y + 1}"


def parse_label(text: str) -> Square | None:
    match = re.fullmatch(r"\s*([A-Za-z])\s?(\d{1,2})\s*", str(text or ""))
    if not match:
        return None
    return ord(match.group(1).upper()) - ord("A"), int(match.group(2)) - 1


def distance_squares(a: Sequence[int], b: Sequence[int]) -> int:
    """Squares between two squares, a diagonal counting as one (PHB grid rule)."""
    return max(abs(int(a[0]) - int(b[0])), abs(int(a[1]) - int(b[1])))


def distance_ft(a: Sequence[int], b: Sequence[int]) -> int:
    return distance_squares(a, b) * SQUARE_FT


def battlefield_of(combat_state: Mapping[str, Any] | None) -> dict | None:
    field_ = (combat_state or {}).get(BATTLEFIELD_KEY)
    return field_ if isinstance(field_, dict) and field_.get("width") else None


def pos_of(c: Mapping[str, Any]) -> Square | None:
    raw = c.get(POS_KEY)
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(v, int) for v in raw):
        return int(raw[0]), int(raw[1])
    return None


def set_pos(c: dict, square: Square) -> None:
    c[POS_KEY] = [int(square[0]), int(square[1])]


def in_bounds(battlefield: Mapping[str, Any], square: Square) -> bool:
    return 0 <= square[0] < int(battlefield["width"]) and 0 <= square[1] < int(
        battlefield["height"]
    )


def terrain_at(battlefield: Mapping[str, Any], square: Square) -> dict | None:
    return next(
        (t for t in battlefield.get("terrain") or () if (t.get("x"), t.get("y")) == tuple(square)),
        None,
    )


def _walls(battlefield: Mapping[str, Any]) -> set[Square]:
    return {
        (int(t["x"]), int(t["y"]))
        for t in battlefield.get("terrain") or ()
        if t.get("kind") == WALL
    }


def _difficult(battlefield: Mapping[str, Any]) -> set[Square]:
    return {
        (int(t["x"]), int(t["y"]))
        for t in battlefield.get("terrain") or ()
        if t.get("kind") == DIFFICULT
    }


# --- creatures -----------------------------------------------------------------------------------


def occupies(c: Mapping[str, Any]) -> bool:
    """A creature fills its square while it is alive and still in the fight (a body does not)."""
    return (
        pos_of(c) is not None
        and disposition_of(c) not in ("dead", "defeated")
        and not c.get("fled")
    )


def hostile(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return bool(a.get("is_player")) != bool(b.get("is_player"))


def walking_speed(speed: str | None) -> int | None:
    """The record's walking speed in feet; a creature with no walking speed uses its flying speed."""
    walk: int | None = None
    fly: int | None = None
    for mode, feet in _SPEED_PART.findall(str(speed or "")):
        if not mode and walk is None:
            walk = int(feet)
        elif mode.lower() == "fly" and fly is None:
            fly = int(feet)
    if walk:
        return walk
    return fly or walk


def speed_of(c: Mapping[str, Any]) -> int:
    if any(has_effect(c, kind) for kind in ("grappled", "restrained", "paralyzed", "stunned")):
        return 0
    stored = c.get(SPEED_KEY)
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    if c.get("is_player"):
        return HERO_SPEED_FEET
    stats = stats_of(c)
    parsed = walking_speed(stats.speed) if stats is not None else None
    return parsed if parsed is not None else 30


def attack_reach_ft(attack: MonsterAttack) -> int:
    return int(attack.reach_ft or MELEE_FEET)


def melee_reach_ft(c: Mapping[str, Any], stats: MonsterStats | None = None) -> int:
    """How far a foe's melee attacks reach: its longest recorded reach, else 5 feet."""
    stats = stats if stats is not None else stats_of(c)
    if stats is None:
        return MELEE_FEET
    melee = [a for a in stats.attacks if not a.ranged and a.save is None]
    return max((attack_reach_ft(a) for a in melee), default=MELEE_FEET)


def weapon_reach_ft(weapon_name: str | None) -> int:
    return MELEE_FEET * 2 if str(weapon_name or "").lower() in REACH_WEAPONS else MELEE_FEET


def weapon_range_ft(weapon_name: str | None) -> tuple[int, int] | None:
    return WEAPON_RANGES.get(str(weapon_name or "").lower())


def range_refusal(attacker: str, target: str, distance: int, long_range: int, what: str) -> str:
    return (
        f"{target} is {distance} feet away, beyond the {long_range}-foot long range of {attacker}'s "
        f"{what}. Move closer first (for example 'I move toward {target}'). No roll is made and no turn "
        "is spent."
    )


# --- movement ------------------------------------------------------------------------------------


@dataclass
class Route:
    """A move on the grid: the squares entered in order (the start is not listed) and their cost."""

    start: Square
    squares: list[Square] = field(default_factory=list)
    cost_ft: int = 0

    @property
    def destination(self) -> Square:
        return self.squares[-1] if self.squares else self.start

    @property
    def moved(self) -> bool:
        return bool(self.squares)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": label(self.start),
            "to": label(self.destination),
            "path": [label(s) for s in self.squares],
            "cost_ft": self.cost_ft,
        }


@dataclass
class _Reach:
    costs: dict[Square, int]
    prev: dict[Square, Square | None]
    stoppable: set[Square]

    def route(self, start: Square, dest: Square) -> Route:
        squares: list[Square] = []
        at: Square | None = dest
        while at is not None and at != start:
            squares.append(at)
            at = self.prev.get(at)
        squares.reverse()
        return Route(start, squares, self.costs.get(dest, 0))


def reachable(
    battlefield: Mapping[str, Any],
    combatants: Iterable[Mapping[str, Any]],
    mover: Mapping[str, Any],
    budget_ft: int,
    *,
    start: Square | None = None,
) -> _Reach:
    """Every square the mover can enter within `budget_ft`, with the cheapest path to it.

    Dijkstra over the 8 neighbours; ties go to the northern, then western square, so the same state
    always gives the same path. `stoppable` holds the squares a move may end in.
    """
    origin = start or pos_of(mover)
    if origin is None:
        return _Reach({}, {}, set())
    walls = _walls(battlefield)
    difficult = _difficult(battlefield)
    others = [c for c in combatants if c is not mover and occupies(c)]
    hostile_squares = {pos_of(c) for c in others if hostile(mover, c)}
    ally_squares = {pos_of(c) for c in others if not hostile(mover, c)}
    costs: dict[Square, int] = {origin: 0}
    prev: dict[Square, Square | None] = {origin: None}
    queue: list[tuple[int, int, int]] = [(0, origin[1], origin[0])]
    while queue:
        cost, y, x = heapq.heappop(queue)
        here = (x, y)
        if cost > costs.get(here, 1 << 30):
            continue
        for dx, dy in _NEIGHBOURS:
            nxt = (x + dx, y + dy)
            if not in_bounds(battlefield, nxt) or nxt in walls or nxt in hostile_squares:
                continue
            if dx and dy and (x + dx, y) in walls and (x, y + dy) in walls:
                continue  # no squeezing diagonally between two walls
            step = SQUARE_FT * 2 if (nxt in difficult or nxt in ally_squares) else SQUARE_FT
            total = cost + step
            if total > budget_ft or total >= costs.get(nxt, 1 << 30):
                continue
            costs[nxt] = total
            prev[nxt] = here
            heapq.heappush(queue, (total, nxt[1], nxt[0]))
    stoppable = {sq for sq in costs if sq == origin or sq not in ally_squares}
    return _Reach(costs, prev, stoppable)


def route_to(
    battlefield: Mapping[str, Any],
    combatants: Iterable[Mapping[str, Any]],
    mover: Mapping[str, Any],
    dest: Square,
    budget_ft: int,
    *,
    start: Square | None = None,
) -> Route | None:
    """The cheapest route to `dest` within the budget, or None when the square cannot be reached."""
    origin = start or pos_of(mover)
    if origin is None:
        return None
    reach = reachable(battlefield, combatants, mover, budget_ft, start=origin)
    if dest not in reach.costs or dest not in reach.stoppable:
        return None
    return reach.route(origin, dest)


def route_toward(
    battlefield: Mapping[str, Any],
    combatants: Iterable[Mapping[str, Any]],
    mover: Mapping[str, Any],
    target: Square,
    budget_ft: int,
    *,
    within_ft: int = MELEE_FEET,
    start: Square | None = None,
) -> Route:
    """Move toward `target`: the cheapest square within `within_ft` of it, else the nearest one.

    A mover already within `within_ft` stays put.
    """
    origin = start or pos_of(mover)
    assert origin is not None
    if distance_ft(origin, target) <= within_ft:
        return Route(origin)
    reach = reachable(battlefield, combatants, mover, budget_ft, start=origin)
    options = [sq for sq in reach.stoppable if sq != target]
    close = [sq for sq in options if distance_ft(sq, target) <= within_ft]
    if close:
        best = min(
            close, key=lambda sq: (reach.costs[sq], distance_squares(sq, target), sq[1], sq[0])
        )
    else:
        best = min(
            options, key=lambda sq: (distance_squares(sq, target), reach.costs[sq], sq[1], sq[0])
        )
    return reach.route(origin, best)


def route_away(
    battlefield: Mapping[str, Any],
    combatants: Iterable[Mapping[str, Any]],
    mover: Mapping[str, Any],
    threats: Sequence[Square],
    budget_ft: int,
    *,
    start: Square | None = None,
) -> Route:
    """Fall back: the square, within the budget, farthest from the nearest threat (cheapest on ties)."""
    origin = start or pos_of(mover)
    assert origin is not None
    if not threats:
        return Route(origin)
    reach = reachable(battlefield, combatants, mover, budget_ft, start=origin)

    def gap(sq: Square) -> int:
        return min(distance_squares(sq, t) for t in threats)

    # Farthest from the nearest threat; then the cheapest; then the straightest line from the start.
    best = max(
        reach.stoppable,
        key=lambda sq: (
            gap(sq),
            -reach.costs[sq],
            -(abs(sq[0] - origin[0]) + abs(sq[1] - origin[1])),
            -sq[1],
            -sq[0],
        ),
    )
    if gap(best) <= gap(origin):
        return Route(origin)
    return reach.route(origin, best)


def provocations(
    combatants: Iterable[Mapping[str, Any]],
    mover: Mapping[str, Any],
    route: Route,
    *,
    can_react: Any,
) -> list[tuple[int, dict]]:
    """Who gets an opportunity attack along a route: (index of the square the mover is leaving, foe).

    A hostile creature that can react provokes once, when the mover leaves its reach (PHB p.195).
    `can_react(foe)` says whether that foe may take a reaction now.
    """
    path = [route.start, *route.squares]
    out: list[tuple[int, dict]] = []
    seen: set[int] = set()
    for c in combatants:
        if c is mover or not hostile(mover, c) or not occupies(c) or not can_react(c):
            continue
        at = pos_of(c)
        reach = melee_reach_ft(c)
        for index in range(len(path) - 1):
            if distance_ft(path[index], at) <= reach < distance_ft(path[index + 1], at):
                if id(c) not in seen:
                    seen.add(id(c))
                    out.append((index, c))  # type: ignore[arg-type]
                break
    return sorted(out, key=lambda item: item[0])


def move_along(mover: dict, route: Route, *, stop_at: int | None = None) -> Square:
    """Put the mover on its route's destination (or on the square at index `stop_at` of the path)."""
    path = [route.start, *route.squares]
    square = path[stop_at] if stop_at is not None else route.destination
    set_pos(mover, square)
    return square


# --- the band projection ------------------------------------------------------------------------


def sync_bands(combat_state: Mapping[str, Any], hero: Mapping[str, Any] | None) -> None:
    """Write each foe's band and distance relative to `hero` from the grid (see the module doc).

    A no-op for an encounter without a battlefield, or a hero without a square.
    """
    if battlefield_of(combat_state) is None or hero is None:
        return
    origin = pos_of(hero)
    if origin is None:
        return
    for c in combat_state.get("combatants") or ():
        if c.get("is_player"):
            continue
        at = pos_of(c)
        if at is None:
            continue
        gap = distance_ft(origin, at)
        c["distance_ft"] = gap
        c["band"] = MELEE if gap <= MELEE_FEET else RANGED


def nearest_hero(
    combatants: Iterable[Mapping[str, Any]], foe: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    at = pos_of(foe)
    heroes = [c for c in combatants if c.get("is_player") and occupies(c) and is_standing(c)]
    if at is None or not heroes:
        return None
    return min(heroes, key=lambda h: distance_squares(at, pos_of(h)))  # type: ignore[arg-type]


# --- opening the battlefield --------------------------------------------------------------------


def theme_for(scene_text: str) -> tuple[str, str, str]:
    words = set(re.findall(r"[a-z]+", str(scene_text or "").lower()))
    for name, keys, wall, difficult in THEMES:
        if words & set(keys):
            return name, wall, difficult
    return DEFAULT_THEME


def _seed_int(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)


def _free(square: Square, taken: set[Square], width: int, height: int) -> bool:
    return 0 <= square[0] < width and 0 <= square[1] < height and square not in taken


def _place_near(
    anchor: Square, taken: set[Square], width: int, height: int, order: Sequence[Square]
) -> Square:
    for dx, dy in order:
        sq = (anchor[0] + dx, anchor[1] + dy)
        if _free(sq, taken, width, height):
            return sq
    # Spiral outwards when every preferred square is taken.
    for radius in range(2, max(width, height)):
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                sq = (anchor[0] + dx, anchor[1] + dy)
                if max(abs(dx), abs(dy)) == radius and _free(sq, taken, width, height):
                    return sq
    raise ValueError("no free square on the battlefield")


# Squares beside a hero a melee foe starts in, front first (the foes come from the east).
_MELEE_RING: tuple[Square, ...] = (
    (1, 0),
    (1, -1),
    (1, 1),
    (0, -1),
    (0, 1),
    (-1, -1),
    (-1, 1),
    (-1, 0),
)


def open_battlefield(combat_state: dict, *, scene_text: str = "", seed: str = "") -> dict:
    """Generate the battlefield and place every combatant. Deterministic for a given seed.

    Placement honours each foe's opening band (`positioning.initial_band`): a melee foe starts in a
    square touching a hero, a ranged foe 30 feet away (`RANGED_START_FEET`). Scenery comes from the
    scene's words (`theme_for`); it never blocks the way between any two combatants.
    """
    combatants = combat_state.get("combatants") or []
    heroes = [c for c in combatants if c.get("is_player")]
    foes = [c for c in combatants if not c.get("is_player")]
    ranged_squares = RANGED_START_FEET // SQUARE_FT
    width = min(20, max(14, 4 + ranged_squares + 2 + len(foes)))
    height = min(16, max(10, 2 * len(heroes) + len(foes) + 4))
    rng = random.Random(_seed_int(seed or "battlefield"))
    centre = height // 2
    hero_x = 2
    taken: set[Square] = set()
    hero_squares: list[Square] = []
    # Heroes stand shoulder to shoulder, so a melee foe can touch several of them (the shared melee
    # band of a party fight before the grid).
    offsets = [0, 1, -1, 2, -2, 3, -3]
    for index, hero in enumerate(heroes):
        square = (hero_x, max(0, min(height - 1, centre + offsets[index % len(offsets)])))
        square = _place_near(square, taken, width, height, ((0, 0), (0, 1), (0, -1)))
        set_pos(hero, square)
        hero[SPEED_KEY] = speed_of(hero)
        taken.add(square)
        hero_squares.append(square)
    ranged_index = 0
    for index, foe in enumerate(foes):
        foe[SPEED_KEY] = speed_of(foe)
        anchor = hero_squares[index % len(hero_squares)] if hero_squares else (hero_x, centre)
        if foe.get("band") == RANGED:
            spread = offsets[ranged_index % len(offsets)]
            ranged_index += 1
            square = (
                min(width - 1, anchor[0] + ranged_squares),
                max(0, min(height - 1, centre + spread)),
            )
            square = _place_near(
                square, taken, width, height, ((0, 0), (0, 1), (0, -1), (1, 0), (1, 1), (1, -1))
            )
        else:
            square = _melee_square(anchor, hero_squares, taken, width, height)
        set_pos(foe, square)
        taken.add(square)
    theme, wall_word, difficult_word = theme_for(scene_text)
    terrain = _scatter_terrain(rng, width, height, taken, wall_word, difficult_word)
    battlefield = {
        "version": VERSION,
        "width": width,
        "height": height,
        "theme": theme,
        "terrain": terrain,
    }
    combat_state[BATTLEFIELD_KEY] = battlefield
    return battlefield


def _melee_square(
    anchor: Square, heroes: Sequence[Square], taken: set[Square], width: int, height: int
) -> Square:
    """A free square touching as many heroes as possible, nearest the front of `anchor` on ties."""
    options = [
        (hero[0] + dx, hero[1] + dy)
        for hero in heroes
        for dx, dy in _MELEE_RING
        if _free((hero[0] + dx, hero[1] + dy), taken, width, height)
    ]
    if not options:
        return _place_near(anchor, taken, width, height, _MELEE_RING)
    ring = {(anchor[0] + dx, anchor[1] + dy): rank for rank, (dx, dy) in enumerate(_MELEE_RING)}

    def touching(sq: Square) -> int:
        return sum(1 for hero in heroes if distance_squares(sq, hero) == 1)

    return min(options, key=lambda sq: (-touching(sq), ring.get(sq, 99), sq[1], sq[0]))


def _scatter_terrain(
    rng: random.Random,
    width: int,
    height: int,
    occupied: set[Square],
    wall_word: str,
    difficult_word: str,
) -> list[dict]:
    """A few clusters of scenery off the combatants' squares. A wall that would cut any combatant off
    from another is not placed."""
    keep_clear = {(x + dx, y + dy) for x, y in occupied for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    walls: set[Square] = set()
    difficult: set[Square] = set()
    target = max(4, (width * height) // 14)
    attempts = 0
    while len(walls) + len(difficult) < target and attempts < 200:
        attempts += 1
        kind = WALL if rng.random() < 0.45 else DIFFICULT
        seed = (rng.randrange(width), rng.randrange(height))
        cluster = [seed]
        for _ in range(rng.randint(0, 3 if kind == DIFFICULT else 2)):
            dx, dy = _NEIGHBOURS[rng.randrange(4)]
            last = cluster[-1]
            cluster.append((last[0] + dx, last[1] + dy))
        for sq in cluster:
            if not _free(sq, keep_clear | walls | difficult, width, height):
                continue
            if kind == WALL:
                if not _connected(occupied, walls | {sq}, width, height):
                    continue
                walls.add(sq)
            else:
                difficult.add(sq)
    terrain = [{"x": x, "y": y, "kind": WALL, "feature": wall_word} for x, y in walls]
    terrain += [
        {"x": x, "y": y, "kind": DIFFICULT, "feature": difficult_word} for x, y in difficult
    ]
    return sorted(terrain, key=lambda t: (t["y"], t["x"]))


def _connected(points: set[Square], walls: set[Square], width: int, height: int) -> bool:
    if not points:
        return True
    start = next(iter(points))
    seen = {start}
    stack = [start]
    while stack:
        x, y = stack.pop()
        for dx, dy in _NEIGHBOURS:
            nxt = (x + dx, y + dy)
            if nxt in seen or nxt in walls or not (0 <= nxt[0] < width and 0 <= nxt[1] < height):
                continue
            if dx and dy and (x + dx, y) in walls and (x, y + dy) in walls:
                continue
            seen.add(nxt)
            stack.append(nxt)
    return points <= seen


# --- text ----------------------------------------------------------------------------------------


def square_move(text: str | None) -> tuple[Square, int, int] | None:
    """The square a player's words move to ("I move to D4"), with the span of those words."""
    return next(iter(square_moves(text)), None)


def square_moves(text: str | None) -> list[tuple[Square, int, int]]:
    """All affirmative square moves, in text order; negation and quoted speech authorize none."""
    moves = []
    for match in affirmative_matches(_SQUARE_MOVE, text):
        square = parse_label(match.group(1) + match.group(2))
        if square is not None:
            moves.append((square, match.start(), match.end()))
    return moves


def describe_square(battlefield: Mapping[str, Any], square: Square) -> str:
    feature = terrain_at(battlefield, square)
    return f"{label(square)} ({feature['feature']})" if feature else label(square)


def position_line(c: Mapping[str, Any], hero: Mapping[str, Any] | None) -> str | None:
    """The narrator's statement of where a combatant stands, as the engine recorded it."""
    at = pos_of(c)
    if at is None:
        return None
    if hero is None or c is hero or pos_of(hero) is None:
        return f"at {label(at)}"
    return f"at {label(at)}, {distance_ft(at, pos_of(hero))} feet from the hero"  # type: ignore[arg-type]


def public_battlefield(combat_state: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the client draws: the grid size, square size and scenery. Positions travel on each row."""
    battlefield = battlefield_of(combat_state)
    if battlefield is None:
        return None
    return {
        "width": int(battlefield["width"]),
        "height": int(battlefield["height"]),
        "square_ft": SQUARE_FT,
        "theme": str(battlefield.get("theme") or DEFAULT_THEME[0]),
        "terrain": [
            {
                "x": int(t["x"]),
                "y": int(t["y"]),
                "kind": str(t["kind"]),
                "feature": str(t.get("feature") or t["kind"]),
            }
            for t in battlefield.get("terrain") or ()
        ],
    }


def public_position(c: Mapping[str, Any]) -> dict[str, Any] | None:
    at = pos_of(c)
    if at is None:
        return None
    return {"x": at[0], "y": at[1], "label": label(at)}
