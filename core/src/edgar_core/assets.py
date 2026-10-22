"""Session-safe map projections and an inert archived visual-review schema.

Known places come from committed session facts. Archived prepared topology remains readable;
this module has no campaign preparation, asset planning or provider dispatch.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.identifiers import Digest, Identifier
from edgar_core.manifests import AdventureManifest
from edgar_core.prepared import route_identity, validate_prepared_campaign
from edgar_core.public_places import public_place_label
from edgar_core.story import StoryFact, player_story_effect


class AssetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class IndependentVisualReviewArtifact(AssetRecord):
    """Signed operator review tied to one exact retained visual and its full scope."""

    version: Literal[1] = 1
    review_id: Identifier
    reviewer_id: Identifier
    candidate_id: Identifier
    decision: Literal["approved", "rejected"]
    reasons: list[str] = Field(min_length=1, max_length=20)
    artifact_sha256: Digest
    manifest_sha256: Digest
    prepared_sha256: Digest
    scene_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    passage_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    entity_id: Identifier | None = None
    signature: str = Field(min_length=40, max_length=256)

    @model_validator(mode="after")
    def has_review_scope(self):
        if not (self.scene_ids or self.passage_ids or self.entity_id):
            raise ValueError("Visual release review must name at least one exact scope.")
        if len(set(self.scene_ids)) != len(self.scene_ids) or len(set(self.passage_ids)) != len(
            self.passage_ids
        ):
            raise ValueError("Visual release review scope IDs must be unique.")
        return self


class PlayerMapPlace(AssetRecord):
    scene_id: Identifier
    # Several story events can happen at one physical place. Only earned scenes occur here.
    scene_ids: list[Identifier] = Field(default_factory=list)
    visited_scene_ids: list[Identifier] = Field(default_factory=list)
    title: str = Field(min_length=1, max_length=200)
    status: Literal["current", "visited", "known"]
    detail: str | None = Field(default=None, max_length=12000)
    current_area: str | None = None


class PlayerMapRoute(AssetRecord):
    from_scene_id: Identifier
    to_scene_id: Identifier


class PlayerMapProjection(AssetRecord):
    """Only the earned topology and text necessary to render one session's map."""

    session_id: int = Field(ge=1)
    source_version: Digest
    topology: Literal["prepared", "reviewed_manifest", "unavailable"]
    schematic: bool = True
    scale: None = None
    current_scene_id: Identifier | None = None
    places: list[PlayerMapPlace]
    routes: list[PlayerMapRoute]
    note: str | None = Field(default=None, max_length=300)


def _matches(predicates: dict[str, str], flags: dict[str, str]) -> bool:
    return all(flags.get(key) == value for key, value in predicates.items())


def _public_map_version(
    source_version: str,
    topology: str,
    places: list[PlayerMapPlace],
    routes: list[PlayerMapRoute],
    current_scene_id: str | None,
) -> str:
    """Version only the already-filtered projection so saved views follow map updates."""
    visible = {
        "source_version": source_version,
        "topology": topology,
        "current_scene_id": current_scene_id,
        "places": [place.model_dump(mode="json") for place in places],
        "routes": sorted((route.from_scene_id, route.to_scene_id) for route in routes),
    }
    return hashlib.sha256(
        json.dumps(visible, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _safe_fact_scene_ids(
    facts: list[StoryFact], *, collection: str, source_sha256: str, manifest_sha256: str
) -> tuple[set[str], set[str], dict[str, str], dict[str, str]]:
    known: set[str] = set()
    visited: set[str] = set()
    details: dict[str, str] = {}
    public_text: dict[str, str] = {}
    for fact in facts:
        target = fact.target
        if (
            target.kind != "scene"
            or target.collection != collection
            or target.source_sha256 != source_sha256
            or target.manifest_sha256 != manifest_sha256
        ):
            continue
        if fact.kind in {"learn_place", "visit", "move"}:
            known.add(target.canonical_id)
            if fact.kind in {"visit", "move"}:
                visited.add(target.canonical_id)
            public = player_story_effect(fact)
            if public is None:
                continue
            label = public_place_label(public.get("text"), public.get("name", ""))
            if label:
                details[target.canonical_id] = label
            if public.get("text"):
                public_text[target.canonical_id] = public["text"][:12000]
    return known, visited, details, public_text


def project_player_map(
    *,
    session_id: int,
    collection: str,
    manifest: AdventureManifest | dict,
    facts: list[StoryFact | dict],
    world_flags: dict[str, str],
    current_scene_id: str | None,
    scene_places: dict[str, dict] | None = None,
    earned_place_names: dict[str, str] | None = None,
) -> PlayerMapProjection:
    """Create a known-only map projection before rendering any image bytes.

    Scene IDs, titles, routes, source/version identity and scale are omitted when no
    canonical manifest is available. Private prepared topology is evaluated here and never
    placed in the browser response for client-side fog.
    """
    canonical = (
        manifest
        if isinstance(manifest, AdventureManifest)
        else AdventureManifest.model_validate(manifest)
    )
    bound_facts = [
        item if isinstance(item, StoryFact) else StoryFact.model_validate(item) for item in facts
    ]
    manifest_hash = canonical.reviewed_sha256 or "0" * 64
    source_hash = canonical.source_sha256
    source_version = hashlib.sha256(
        f"{collection}\0{source_hash}\0{manifest_hash}".encode()
    ).hexdigest()
    known, visited, details, public_text = _safe_fact_scene_ids(
        bound_facts,
        collection=collection,
        source_sha256=source_hash,
        manifest_sha256=manifest_hash,
    )
    scene_by_id = {scene.scene_id: scene for scene in canonical.scenes}
    if current_scene_id in scene_by_id:
        known.add(current_scene_id)
        visited.add(current_scene_id)
    if not known:
        topology = "prepared" if canonical.prepared else "unavailable"
        return PlayerMapProjection(
            session_id=session_id,
            source_version=_public_map_version(source_version, topology, [], [], None),
            topology=topology,
            places=[],
            routes=[],
            note="No places have been committed to this session yet.",
        )

    scene_places = scene_places or {}
    earned_place_names = earned_place_names or {}
    groups: dict[str, list[str]] = {}
    for scene_id in sorted(known & scene_by_id.keys()):
        identity = scene_places.get(scene_id, {}).get("place_id", scene_id)
        groups.setdefault(identity, []).append(scene_id)
    places = []
    projected_scene: dict[str, str] = {}
    for scenes in groups.values():
        representative = current_scene_id if current_scene_id in scenes else scenes[0]
        for scene_id in scenes:
            projected_scene[scene_id] = representative
        text = "\n".join(public_text.get(scene_id, "") for scene_id in scenes)
        names = [name for scene_id in scenes for name in scene_places.get(scene_id, {}).get("names", [])]
        label = next((earned_place_names[s] for s in scenes if earned_place_names.get(s) not in {None, "Unidentified place"}), None) or next(
            (label for name in names if (label := public_place_label(text, name)) == name),
            None,
        ) or next((details[s] for s in [representative, *scenes] if details.get(s) != "Unidentified place" and details.get(s)), None)
        places.append(PlayerMapPlace(
            scene_id=representative,
            scene_ids=scenes,
            visited_scene_ids=[scene for scene in scenes if scene in visited],
            title=label or ("Starting location" if canonical.opening_scene_id in scenes else "Known location"),
            # Arrival staging is immutable evidence, not a claim about actors still present.
            detail=public_text.get(representative),
            status="current" if current_scene_id in scenes else "visited" if set(scenes) & visited else "known",
            current_area=details.get(current_scene_id) if current_scene_id in scenes and details.get(current_scene_id) != "Unidentified place" else None,
        ))
    # Reviewed links may describe topology for unprepared manifests. Once a prepared
    # campaign exists, its route inventory is authoritative; missing or invalid prepared
    # topology must never fall back to every canonical link.
    prepared = canonical.prepared
    prepared_routes: dict[tuple, object] | None = None
    topology: Literal["prepared", "reviewed_manifest", "unavailable"] = "reviewed_manifest"
    prepared_valid = prepared is None
    if prepared is not None:
        report = validate_prepared_campaign(canonical, prepared)
        if report.valid:
            prepared_routes = {
                route_identity(
                    route.from_scene_id, route.to_scene_id, route.traversal_requires
                ): route
                for route in prepared.routes
            }
            topology = "prepared"
            prepared_valid = True
        else:
            topology = "unavailable"

    routes: list[PlayerMapRoute] = []
    for scene in canonical.scenes:
        for link in scene.links:
            if not _matches(link.requires, world_flags):
                continue
            if scene.scene_id not in known or link.scene_id not in known:
                continue
            route = (
                prepared_routes.get(route_identity(scene.scene_id, link.scene_id, link.requires))
                if prepared_routes
                else None
            )
            if prepared is not None:
                if not prepared_valid or route is None:
                    continue
                if route.reveal_requires and not _matches(route.reveal_requires, world_flags):
                    continue
                if route.visibility == "private" and not route.reveal_requires:
                    continue
            start, end = projected_scene[scene.scene_id], projected_scene[link.scene_id]
            # Event transitions at the same physical location are not geographic routes.
            if start == end and scene.scene_id != link.scene_id:
                continue
            projected = PlayerMapRoute(from_scene_id=start, to_scene_id=end)
            if projected not in routes:
                routes.append(projected)
    current = current_scene_id if current_scene_id in known else None
    return PlayerMapProjection(
        session_id=session_id,
        source_version=_public_map_version(source_version, topology, places, routes, current),
        topology=topology,
        current_scene_id=current,
        places=places,
        routes=routes,
        note="Schematic map; a location may include several nearby areas. Current area shows your position within it. Routes do not establish distance or scale.",
    )


def render_player_map_svg(projection: PlayerMapProjection) -> bytes:
    """Render a deterministic SVG using only the already-filtered player projection."""
    places = projection.places
    if not places:
        return (
            b'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
            b'aria-label="No known places to map" viewBox="0 0 640 220">'
            b'<rect width="640" height="220" fill="#101827"/><text x="32" y="112" '
            b'fill="#d5dbe7" font-size="18">No places are known yet</text></svg>'
        )
    # Build deterministic positions from the safe graph. Hidden graph nodes never affect
    # spacing, node count, dimensions, IDs or SVG metadata.
    ids = [place.scene_id for place in places]
    adjacency: dict[str, set[str]] = {scene_id: set() for scene_id in ids}
    for route in projection.routes:
        adjacency[route.from_scene_id].add(route.to_scene_id)
        adjacency[route.to_scene_id].add(route.from_scene_id)
    columns = max(1, math.ceil(math.sqrt(len(ids))))
    positions = {
        scene_id: (120 + (index % columns) * 260, 100 + (index // columns) * 150)
        for index, scene_id in enumerate(ids)
    }
    width = max(520, columns * 260 + 100)
    height = max(220, math.ceil(len(ids) / columns) * 150 + 100)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Known places map" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#101827"/>',
    ]
    rendered_edges: set[tuple[str, str]] = set()
    for route in projection.routes:
        edge = tuple(sorted((route.from_scene_id, route.to_scene_id)))
        if edge in rendered_edges:
            continue
        rendered_edges.add(edge)
        x1, y1 = positions[route.from_scene_id]
        x2, y2 = positions[route.to_scene_id]
        out.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#7084a8" stroke-width="3"/>'
        )
    for place in places:
        x, y = positions[place.scene_id]
        fill = (
            "#f7ad55"
            if place.status == "current"
            else "#8fb5d8"
            if place.status == "visited"
            else "#566883"
        )
        title = html.escape(place.title, quote=True)
        out.append(
            f'<g><circle cx="{x}" cy="{y}" r="32" fill="{fill}" stroke="#d8e3f0" stroke-width="2"/>'
            f'<text x="{x}" y="{y + 58}" text-anchor="middle" fill="#f1f4f8" font-size="16">{title}</text>'
            f'<text x="{x}" y="{y + 5}" text-anchor="middle" fill="#0d1725" font-size="12">{place.status.title()}</text></g>'
        )
    out.append("</svg>")
    return "".join(out).encode("utf-8")
