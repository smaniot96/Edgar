"""A prepared regional sketch map and its player-safe projection.

The prepared map places every adventure place on a 0-100 grid with routes and terrain.
Players see public places, places their session has reached or learned, routes between
places they can see, and the terrain. Hidden places and routes stay server-side until
earned, exactly like the scene map.
"""

from __future__ import annotations

import html
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.identifiers import Identifier

SYMBOLS = (
    "town",
    "village",
    "building",
    "tower",
    "ruin",
    "cave",
    "camp",
    "shrine",
    "dock",
    "bridge",
    "landmark",
    "dungeon",
)
ROUTE_KINDS = ("road", "path", "river", "sea", "tunnel", "bridge")
FEATURE_KINDS = ("water", "coast", "forest", "hills", "mountains", "marsh", "fields", "ruins")


class MapRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MapPoint(MapRecord):
    x: int = Field(ge=0, le=100)
    y: int = Field(ge=0, le=100)


class MapPlace(MapRecord):
    place_id: Identifier
    label: str = Field(min_length=1, max_length=80)
    x: int = Field(ge=0, le=100)
    y: int = Field(ge=0, le=100)
    symbol: Literal[SYMBOLS]
    public: bool


class MapRoute(MapRecord):
    from_place_id: Identifier
    to_place_id: Identifier
    kind: Literal[ROUTE_KINDS]
    hidden: bool
    label: str = Field(default="", max_length=80)


class MapFeature(MapRecord):
    name: str = Field(max_length=80)
    kind: Literal[FEATURE_KINDS]
    shape: Literal["area", "line"]
    points: list[MapPoint] = Field(min_length=2)


class WorldMap(MapRecord):
    """Prepared, private: includes hidden places and routes."""

    title: str = Field(min_length=1, max_length=120)
    places: list[MapPlace] = Field(min_length=1)
    routes: list[MapRoute] = Field(default_factory=list)
    features: list[MapFeature] = Field(default_factory=list)
    scene_places: dict[str, Identifier] = Field(
        default_factory=dict, description="Scene ID -> the place where it happens."
    )


class PlayerWorldPlace(MapRecord):
    place_id: Identifier
    label: str
    x: int
    y: int
    symbol: str
    status: Literal["current", "visited", "known", "rumoured"]


class PlayerWorldMap(MapRecord):
    """Only what one session may see; safe to send to the browser."""

    title: str
    places: list[PlayerWorldPlace]
    routes: list[MapRoute]
    features: list[MapFeature]
    current_place_id: Identifier | None = None


def project_world_map(
    world: WorldMap | dict,
    *,
    known_scene_ids: set[str],
    visited_scene_ids: set[str],
    current_scene_id: str | None,
) -> PlayerWorldMap:
    world = world if isinstance(world, WorldMap) else WorldMap.model_validate(world)
    scene_places = world.scene_places
    known = {scene_places[s] for s in known_scene_ids if s in scene_places}
    visited = {scene_places[s] for s in visited_scene_ids if s in scene_places}
    current = scene_places.get(current_scene_id) if current_scene_id else None
    if current:
        known.add(current)
        visited.add(current)
    places = []
    for place in world.places:
        if not (place.public or place.place_id in known):
            continue
        status = (
            "current"
            if place.place_id == current
            else "visited"
            if place.place_id in visited
            else "known"
            if place.place_id in known
            else "rumoured"
        )
        places.append(
            PlayerWorldPlace(
                place_id=place.place_id,
                label=place.label,
                x=place.x,
                y=place.y,
                symbol=place.symbol,
                status=status,
            )
        )
    shown = {p.place_id for p in places}
    routes = [
        route
        for route in world.routes
        if route.from_place_id in shown
        and route.to_place_id in shown
        # A hidden passage appears once the heroes have been at both of its ends.
        and (not route.hidden or {route.from_place_id, route.to_place_id} <= visited)
    ]
    return PlayerWorldMap(
        title=world.title,
        places=places,
        routes=[r.model_copy(update={"hidden": False}) for r in routes],
        features=world.features,
        current_place_id=current if current in shown else None,
    )


WIDTH, HEIGHT, MARGIN = 1000, 700, 60
INK, PAPER = "#4a3622", "#efe2c4"
FEATURE_STYLE = {
    "water": ("#9cc3d8", "#5d8fae"),
    "coast": ("#c9dfe6", "#5d8fae"),
    "forest": ("#a9c48b", "#5d7a3f"),
    "hills": ("#d8c49a", "#8a6d3f"),
    "mountains": ("#c2b7a6", "#6b5f4f"),
    "marsh": ("#b8c9a0", "#6f8256"),
    "fields": ("#e6d9a2", "#a8935a"),
    "ruins": ("#d2c3a8", "#7d6a4e"),
}
ROUTE_STYLE = {
    "road": ("#7a5a36", "4", ""),
    "path": ("#7a5a36", "2.5", "8 6"),
    "river": ("#4f87ad", "4", ""),
    "sea": ("#4f87ad", "2.5", "2 7"),
    "tunnel": ("#5a4a3a", "2.5", "12 5 2 5"),
    "bridge": ("#7a5a36", "5", ""),
}
STATUS_FILL = {
    "current": "#b7412e",
    "visited": "#4a3622",
    "known": "#8a6d3f",
    "rumoured": PAPER,
}


def _xy(x: float, y: float) -> tuple[float, float]:
    """Grid units (0-100) to SVG coordinates below the title band."""
    return (
        MARGIN + x * (WIDTH - 2 * MARGIN) / 100,
        MARGIN + 30 + y * (HEIGHT - 2 * MARGIN - 30) / 100,
    )


def _symbol(symbol: str, x: float, y: float, fill: str) -> str:
    if symbol in ("town", "village"):
        size = 13 if symbol == "town" else 10
        return (
            f'<rect x="{x - size}" y="{y - size}" width="{2 * size}" height="{2 * size}" '
            f'fill="{fill}" stroke="{INK}" stroke-width="2.5"/>'
        )
    if symbol in ("tower", "dungeon", "landmark"):
        return (
            f'<polygon points="{x},{y - 15} {x + 13},{y + 10} {x - 13},{y + 10}" '
            f'fill="{fill}" stroke="{INK}" stroke-width="2.5"/>'
        )
    if symbol in ("cave", "ruin"):
        return (
            f'<path d="M{x - 13},{y + 9} Q{x},{y - 18} {x + 13},{y + 9} Z" '
            f'fill="{fill}" stroke="{INK}" stroke-width="2.5"/>'
        )
    return f'<circle cx="{x}" cy="{y}" r="11" fill="{fill}" stroke="{INK}" stroke-width="2.5"/>'


def render_world_map_svg(world: PlayerWorldMap) -> bytes:
    """Render a hand-drawn style regional sketch from the filtered projection only."""
    title = html.escape(world.title, quote=True)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Map of {title}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="Georgia, serif">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{PAPER}"/>',
        f'<rect x="14" y="14" width="{WIDTH - 28}" height="{HEIGHT - 28}" fill="none" '
        f'stroke="{INK}" stroke-width="3"/>',
    ]
    for feature in world.features:
        fill, stroke = FEATURE_STYLE[feature.kind]
        points = " ".join("{:.0f},{:.0f}".format(*_xy(p.x, p.y)) for p in feature.points)
        if feature.shape == "area" and len(feature.points) >= 3:
            out.append(
                f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="2" '
                'stroke-linejoin="round" opacity="0.85"/>'
            )
        else:
            out.append(
                f'<polyline points="{points}" fill="none" stroke="{stroke}" stroke-width="6" '
                'stroke-linecap="round" stroke-linejoin="round" opacity="0.8"/>'
            )
        cx = sum(p.x for p in feature.points) / len(feature.points)
        cy = sum(p.y for p in feature.points) / len(feature.points)
        lx, ly = _xy(cx, cy)
        out.append(
            f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle" fill="{stroke}" '
            f'font-size="15" font-style="italic">{html.escape(feature.name, quote=True)}</text>'
        )
    places = {p.place_id: p for p in world.places}
    for route in world.routes:
        color, width, dash = ROUTE_STYLE[route.kind]
        start, end = places[route.from_place_id], places[route.to_place_id]
        (x1, y1), (x2, y2) = _xy(start.x, start.y), _xy(end.x, end.y)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        out.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round"{dash_attr}/>'
        )
    for place in world.places:
        x, y = _xy(place.x, place.y)
        if place.status == "current":
            out.append(
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="24" fill="none" stroke="#b7412e" '
                'stroke-width="3" stroke-dasharray="5 4"/>'
            )
        out.append(_symbol(place.symbol, round(x), round(y), STATUS_FILL[place.status]))
        out.append(
            f'<text x="{x:.0f}" y="{y + 32:.0f}" text-anchor="middle" fill="{INK}" '
            f'font-size="17" font-weight="bold" paint-order="stroke" stroke="{PAPER}" '
            f'stroke-width="4">{html.escape(place.label, quote=True)}</text>'
        )
    out.append(
        f'<text x="{WIDTH / 2:.0f}" y="52" text-anchor="middle" fill="{INK}" font-size="28" '
        f'font-weight="bold">{title}</text>'
    )
    cx, cy = WIDTH - 70, HEIGHT - 80
    out.append(
        f'<g aria-hidden="true"><polygon points="{cx},{cy - 34} {cx + 9},{cy} {cx},{cy - 8} '
        f'{cx - 9},{cy}" fill="{INK}"/><text x="{cx}" y="{cy - 40}" text-anchor="middle" '
        f'fill="{INK}" font-size="16" font-weight="bold">N</text></g>'
    )
    out.append("</svg>")
    return "".join(out).encode("utf-8")
