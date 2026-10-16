"""Canonical adventure asset plans and player-safe map projections.

Assets are version-bound decorations around the reviewed manifest/prepared campaign. They
never own routes, player knowledge, or movement. The player projection is built from the
selected session's committed facts before the renderer sees any topology.
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
from edgar_core.prepared import prepared_digest, validate_prepared_campaign
from edgar_core.sources import SourceEdition
from edgar_core.story import StoryFact


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
        if len(set(self.scene_ids)) != len(self.scene_ids) or len(set(self.passage_ids)) != len(self.passage_ids):
            raise ValueError("Visual release review scope IDs must be unique.")
        return self


class IndependentVisualReviewDecision(AssetRecord):
    """Exact bounded critic output. Scope and reviewer identity are server-derived."""

    candidate_id: Identifier
    artifact_sha256: Digest
    decision: Literal["approved", "rejected"]
    reasons: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def bounded_reasons(self):
        if any(not reason.strip() or len(reason) > 500 for reason in self.reasons):
            raise ValueError("Visual review reasons must be non-empty and at most 500 characters.")
        return self


class PrivateVisualCandidate(AssetRecord):
    """Private retained bytes and opaque identity supplied to the independent critic."""

    candidate_id: Identifier
    artifact_sha256: Digest
    manifest_sha256: Digest
    prepared_sha256: Digest
    kind: Literal["source_map", "generated_illustration", "original_creature_art"]
    media_type: str
    scene_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    passage_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    entity_id: Identifier | None = None
    data: bytes = Field(repr=False)


class CropRect(AssetRecord):
    """Normalized crop inside one source PDF page, recorded without guessing geometry."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_page(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Source crop must remain within its page.")
        return self


class SourceCropBinding(AssetRecord):
    collection: str = Field(min_length=1, max_length=200)
    source_sha256: Digest
    extraction_sha256: Digest
    book_title: str = Field(min_length=1, max_length=300)
    edition: SourceEdition
    page: int = Field(ge=1)
    crop_id: Identifier
    crop: CropRect


class CreatureArtworkBinding(AssetRecord):
    creature_name: str = Field(min_length=1, max_length=200)
    variant: str = Field(min_length=1, max_length=200)
    source: SourceCropBinding
    attribution: str = Field(min_length=1, max_length=1000)
    usage_terms: str = Field(min_length=1, max_length=1000)


class AssetRequirement(AssetRecord):
    """Private stage input describing a desired visual or printable artifact."""

    asset_id: Identifier
    kind: Literal["source_map", "generated_illustration", "handout", "original_creature_art"]
    required: bool
    act_id: Identifier | None = None
    scene_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    passage_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    entity_id: Identifier | None = None
    prompt: str | None = Field(default=None, max_length=4000)
    rationale: str = Field(default="", max_length=1000)
    source_crop: SourceCropBinding | None = None
    artwork_status: Literal["available", "unavailable", "ambiguous"] = "unavailable"
    creature_artwork: CreatureArtworkBinding | None = None

    @model_validator(mode="after")
    def matching_target(self):
        if self.kind == "handout" and not self.passage_ids:
            raise ValueError("Handouts must name exact reviewed passage IDs.")
        if self.kind == "original_creature_art" and not self.entity_id:
            raise ValueError("Creature artwork must name one canonical encounter entity.")
        if self.kind == "source_map" and self.source_crop is None:
            raise ValueError("Retained source maps need exact source/page/crop provenance.")
        if self.kind == "original_creature_art":
            if self.artwork_status == "available" and self.creature_artwork is None:
                raise ValueError("Available original artwork needs an exact source crop binding.")
            if self.artwork_status != "available" and self.creature_artwork is not None:
                raise ValueError("Unavailable or ambiguous original artwork cannot carry an image binding.")
        if self.kind in {"source_map", "generated_illustration"} and not (
            self.act_id or self.scene_ids
        ):
            raise ValueError("Visual assets must be scoped to canonical acts or scenes.")
        if self.kind == "generated_illustration" and not self.prompt:
            raise ValueError("Generated illustrations need a private descriptive prompt.")
        return self


class AssetPlanItem(AssetRecord):
    asset_id: Identifier
    kind: Literal[
        "schematic_map", "source_map", "generated_illustration", "handout", "original_creature_art"
    ]
    required: bool
    act_id: Identifier | None = None
    scene_ids: list[Identifier] = Field(default_factory=list)
    passage_ids: list[Identifier] = Field(default_factory=list)
    entity_id: Identifier | None = None
    prompt: str | None = None
    rationale: str = ""
    source_crop: SourceCropBinding | None = None
    artwork_status: Literal["available", "unavailable", "ambiguous"] | None = None
    creature_artwork: CreatureArtworkBinding | None = None


class AssetPlan(AssetRecord):
    """Deterministic P07 result. Readiness is derived from complete coverage, never supplied."""

    version: Literal[1] = 1
    source_mode: Literal["pdf", "ai"]
    manifest_sha256: Digest
    prepared_sha256: Digest
    items: list[AssetPlanItem]
    required_act_ids: list[Identifier]
    required_scene_ids: list[Identifier]
    findings: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.findings


class AssetCoverage(AssetRecord):
    asset_id: Identifier
    kind: Literal[
        "schematic_map", "source_map", "generated_illustration", "handout", "original_creature_art"
    ]
    required: bool
    status: Literal["ready", "unavailable", "failed", "skipped"]
    player_review_status: Literal["not_required", "pending", "approved", "rejected"] = "not_required"
    artifact_sha256: Digest | None = None
    explanation: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def review_status_matches_asset(self):
        reviewable = self.kind in {
            "source_map", "generated_illustration", "original_creature_art"
        }
        if self.status == "ready" and reviewable and self.player_review_status == "not_required":
            raise ValueError("Prepared player visuals must remain pending until independently reviewed.")
        if not reviewable and self.player_review_status != "not_required":
            raise ValueError("Only player visual assets can require independent visual review.")
        return self


class AssetScopeCoverage(AssetRecord):
    scope: Literal["act", "scene"]
    scope_id: Identifier
    required: bool
    status: Literal["ready", "unavailable", "failed", "skipped"]
    asset_ids: list[Identifier] = Field(default_factory=list)


class AssetCoverageReceipt(AssetRecord):
    manifest_sha256: Digest
    prepared_sha256: Digest
    profile_digest: Digest | None = None
    coverage: list[AssetCoverage]
    findings: list[str] = Field(default_factory=list)
    image_price_revision: str | None = None
    image_cost_limit_microusd: int = Field(default=0, ge=0)
    image_call_cost_limit_microusd: int = Field(default=0, ge=0)
    image_calls_reserved: int = Field(default=0, ge=0)
    image_calls_succeeded: int = Field(default=0, ge=0)
    image_calls_failed: int = Field(default=0, ge=0)
    image_cost_reserved_microusd: int = Field(default=0, ge=0)
    image_cost_succeeded_microusd: int = Field(default=0, ge=0)
    image_cost_failed_microusd: int = Field(default=0, ge=0)
    image_cost_refunded_microusd: int = Field(default=0, ge=0)
    image_job_keys: list[Digest] = Field(default_factory=list, max_length=500)
    act_coverage: list[AssetScopeCoverage] = Field(default_factory=list)
    scene_coverage: list[AssetScopeCoverage] = Field(default_factory=list)

    @property
    def required_ready(self) -> bool:
        required_items = [item for item in self.coverage if item.required]
        required_acts = [item for item in self.act_coverage if item.required]
        required_scenes = [item for item in self.scene_coverage if item.required]
        return (
            not self.findings
            and bool(required_items)
            and bool(required_acts)
            and bool(required_scenes)
            and all(item.status == "ready" for item in required_items)
            and all(
                item.kind not in {"source_map", "generated_illustration", "original_creature_art"}
                or item.player_review_status == "approved"
                for item in required_items
            )
            and all(item.status == "ready" for item in required_acts)
            and all(item.status == "ready" for item in required_scenes)
        )


class PlayerMapPlace(AssetRecord):
    scene_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    status: Literal["current", "visited", "known"]
    detail: str | None = Field(default=None, max_length=12000)


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
        "routes": sorted(
            (route.from_scene_id, route.to_scene_id) for route in routes
        ),
    }
    return hashlib.sha256(json.dumps(visible, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _safe_fact_scene_ids(
    facts: list[StoryFact], *, collection: str, source_sha256: str, manifest_sha256: str
) -> tuple[set[str], set[str], dict[str, str]]:
    known: set[str] = set()
    visited: set[str] = set()
    details: dict[str, str] = {}
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
            if fact.display_name:
                details[target.canonical_id] = fact.display_name
    return known, visited, details


def project_player_map(
    *,
    session_id: int,
    collection: str,
    manifest: AdventureManifest | dict,
    facts: list[StoryFact | dict],
    world_flags: dict[str, str],
    current_scene_id: str | None,
) -> PlayerMapProjection:
    """Create a known-only map projection before rendering any image bytes.

    Scene IDs, titles, routes, source/version identity and scale are omitted when no
    canonical manifest is available. Private prepared topology is evaluated here and never
    placed in the browser response for client-side fog.
    """
    canonical = manifest if isinstance(manifest, AdventureManifest) else AdventureManifest.model_validate(manifest)
    bound_facts = [item if isinstance(item, StoryFact) else StoryFact.model_validate(item) for item in facts]
    manifest_hash = canonical.reviewed_sha256 or "0" * 64
    source_hash = canonical.source_sha256
    source_version = hashlib.sha256(
        f"{collection}\0{source_hash}\0{manifest_hash}".encode()
    ).hexdigest()
    known, visited, details = _safe_fact_scene_ids(
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

    places = [
        PlayerMapPlace(
            scene_id=scene_id,
            title=details.get(scene_id, scene_by_id[scene_id].title),
            status=(
                "current"
                if scene_id == current_scene_id
                else "visited"
                if scene_id in visited
                else "known"
            ),
        )
        for scene_id in sorted(known & scene_by_id.keys())
    ]
    # Reviewed links may describe topology for unprepared manifests. Once a prepared
    # campaign exists, its route inventory is authoritative; missing or invalid prepared
    # topology must never fall back to every canonical link.
    prepared = canonical.prepared
    prepared_routes: dict[tuple[str, str], object] | None = None
    topology: Literal["prepared", "reviewed_manifest", "unavailable"] = "reviewed_manifest"
    prepared_valid = prepared is None
    if prepared is not None:
        report = validate_prepared_campaign(canonical, prepared)
        if report.valid:
            prepared_routes = {
                (route.from_scene_id, route.to_scene_id): route for route in prepared.routes
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
            route = prepared_routes.get((scene.scene_id, link.scene_id)) if prepared_routes else None
            if prepared is not None:
                if not prepared_valid or route is None:
                    continue
                if route.reveal_requires and not _matches(route.reveal_requires, world_flags):
                    continue
                if route.visibility == "private" and not route.reveal_requires:
                    continue
            routes.append(PlayerMapRoute(from_scene_id=scene.scene_id, to_scene_id=link.scene_id))
    current = current_scene_id if current_scene_id in known else None
    return PlayerMapProjection(
        session_id=session_id,
        source_version=_public_map_version(source_version, topology, places, routes, current),
        topology=topology,
        current_scene_id=current,
        places=places,
        routes=routes,
        note="Schematic map; it shows learned connections and does not establish distance or scale.",
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
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#7084a8" stroke-width="3"/>')
    for place in places:
        x, y = positions[place.scene_id]
        fill = "#f7ad55" if place.status == "current" else "#8fb5d8" if place.status == "visited" else "#566883"
        title = html.escape(place.title, quote=True)
        out.append(
            f'<g><circle cx="{x}" cy="{y}" r="32" fill="{fill}" stroke="#d8e3f0" stroke-width="2"/>'
            f'<text x="{x}" y="{y + 58}" text-anchor="middle" fill="#f1f4f8" font-size="16">{title}</text>'
            f'<text x="{x}" y="{y + 5}" text-anchor="middle" fill="#0d1725" font-size="12">{place.status.title()}</text></g>'
        )
    out.append("</svg>")
    return "".join(out).encode("utf-8")


def plan_assets(
    manifest: AdventureManifest | dict,
    required_maps: list[AssetRequirement | dict],
    required_handouts: list[AssetRequirement | dict],
) -> AssetPlan:
    """Build P07 coverage for every prepared act/scene and explicitly requested assets."""
    canonical = manifest if isinstance(manifest, AdventureManifest) else AdventureManifest.model_validate(manifest)
    prepared = canonical.prepared
    findings: list[str] = []
    items: list[AssetPlanItem] = []
    acts: list[str] = []
    scenes: list[str] = []
    if prepared is None:
        findings.append("A reviewed prepared campaign is unavailable.")
        mode: Literal["pdf", "ai"] = "pdf" if canonical.source.lower().endswith(".pdf") else "ai"
        prepared_hash = hashlib.sha256(b"missing-prepared-campaign").hexdigest()
    else:
        mode = prepared.source_mode
        report = validate_prepared_campaign(canonical, prepared)
        if not report.valid:
            findings.append("The canonical prepared topology failed deterministic validation.")
        prepared_hash = prepared_digest(prepared)
        acts = [act.act_id for act in prepared.acts]
        scenes = [scene.scene_id for scene in canonical.scenes]
        for act in prepared.acts:
            map_asset_id = (
                f"schematic_{act.act_id[:40]}_"
                f"{hashlib.sha256(act.act_id.encode()).hexdigest()[:12]}"
            )
            items.append(
                AssetPlanItem(
                    asset_id=map_asset_id,
                    kind="schematic_map",
                    required=True,
                    act_id=act.act_id,
                    scene_ids=act.scene_ids,
                    rationale="Deterministic map generated from the reviewed canonical topology.",
                )
            )

    requested_maps = [AssetRequirement.model_validate(item) for item in required_maps]
    requested_handouts = [AssetRequirement.model_validate(item) for item in required_handouts]
    if any(item.kind == "handout" for item in requested_maps):
        findings.append("Handout requirements must use the handout collection.")
    if any(item.kind != "handout" for item in requested_handouts):
        findings.append("Map and artwork requirements must use the map collection.")
    for requirement in requested_maps + requested_handouts:
        if requirement.act_id and requirement.act_id not in acts:
            findings.append(f"Asset {requirement.asset_id} names an unknown canonical act.")
        if set(requirement.scene_ids) - set(scenes):
            findings.append(f"Asset {requirement.asset_id} names an unknown canonical scene.")
        known_passages = {passage.passage_id for passage in canonical.passages}
        if set(requirement.passage_ids) - known_passages:
            findings.append(f"Asset {requirement.asset_id} names an unknown reviewed source passage.")
        if requirement.kind == "source_map":
            crop = requirement.source_crop
            if crop is None or crop.source_sha256 != canonical.source_sha256:
                findings.append(f"Source map {requirement.asset_id} is not bound to this exact source.")
            if crop is not None and crop.extraction_sha256 != canonical.extraction_sha256:
                findings.append(f"Source map {requirement.asset_id} is stale for this extraction.")
            items.append(
                AssetPlanItem(
                    asset_id=requirement.asset_id,
                    kind="source_map",
                    required=requirement.required,
                    act_id=requirement.act_id,
                    scene_ids=requirement.scene_ids,
                    passage_ids=requirement.passage_ids,
                    rationale=requirement.rationale or "Source map must be retained with exact page and crop provenance.",
                    source_crop=requirement.source_crop,
                )
            )
        elif requirement.kind == "generated_illustration":
            items.append(
                AssetPlanItem(
                    asset_id=requirement.asset_id,
                    kind="generated_illustration",
                    required=requirement.required,
                    act_id=requirement.act_id,
                    scene_ids=requirement.scene_ids,
                    prompt=requirement.prompt,
                    rationale=requirement.rationale,
                )
            )
        elif requirement.kind == "handout":
            items.append(
                AssetPlanItem(
                    asset_id=requirement.asset_id,
                    kind="handout",
                    required=requirement.required,
                    scene_ids=requirement.scene_ids,
                    passage_ids=requirement.passage_ids,
                    rationale=requirement.rationale,
                )
            )
        elif requirement.kind == "original_creature_art":
            entity = next(
                (item for item in canonical.entities if item.entity_id == requirement.entity_id), None
            )
            if entity is None or entity.kind != "actor" or entity.stat_reference is None:
                findings.append(f"Original artwork {requirement.asset_id} does not match a verified encounter entity.")
            binding = requirement.creature_artwork
            if binding is not None and entity is not None and entity.stat_reference is not None:
                stat = entity.stat_reference
                if binding.creature_name != stat.creature_name:
                    findings.append(f"Original artwork {requirement.asset_id} has a similar or mismatched creature name.")
                if stat.kind == "template" and (
                    binding.source.collection != stat.collection
                    or binding.source.source_sha256 != stat.source_sha256
                    or binding.source.edition != stat.rules_edition
                ):
                    findings.append(f"Original artwork {requirement.asset_id} does not match the verified book edition.")
                if stat.kind == "module" and (
                    binding.source.source_sha256 != canonical.source_sha256
                    or binding.source.edition != canonical.rules_edition
                    or binding.source.extraction_sha256 != canonical.extraction_sha256
                ):
                    findings.append(f"Original artwork {requirement.asset_id} does not match the reviewed module edition.")
            if requirement.required and requirement.artwork_status != "available":
                findings.append(f"Required original artwork {requirement.asset_id} is {requirement.artwork_status}.")
            items.append(
                AssetPlanItem(
                    asset_id=requirement.asset_id,
                    kind="original_creature_art",
                    required=requirement.required,
                    scene_ids=requirement.scene_ids,
                    entity_id=requirement.entity_id,
                    rationale=requirement.rationale,
                    artwork_status=requirement.artwork_status,
                    creature_artwork=requirement.creature_artwork,
                )
            )
    if len({item.asset_id for item in items}) != len(items):
        findings.append("Asset plan contains duplicate stable asset IDs.")
    return AssetPlan(
        source_mode=mode,
        manifest_sha256=canonical.reviewed_sha256 or hashlib.sha256(b"unreviewed-manifest").hexdigest(),
        prepared_sha256=prepared_hash,
        items=items,
        required_act_ids=acts,
        required_scene_ids=scenes,
        findings=findings,
    )


__all__ = [
    "AssetCoverage", "AssetCoverageReceipt", "AssetPlan", "AssetPlanItem", "AssetRequirement",
    "CreatureArtworkBinding", "CropRect", "IndependentVisualReviewArtifact",
    "IndependentVisualReviewDecision", "PrivateVisualCandidate", "SourceCropBinding",
    "PlayerMapPlace", "PlayerMapProjection", "PlayerMapRoute", "plan_assets",
    "project_player_map", "render_player_map_svg",
]
