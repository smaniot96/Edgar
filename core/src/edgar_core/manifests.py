"""Reviewed adventure structure; source text stays in its extraction bundle and index.

This is a review receipt, not authentication. Reviewers explicitly classify exact spans;
neither heading detection nor a successful vector search certifies player-safe material.
Campaign progress remains in Session.current_scene_id and WorldFlag rows.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.artifacts import text_digest
from edgar_core.sources import SourceEdition

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ManifestError(ValueError):
    """The source needs explicit manifest review before module play can proceed."""


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Link(Record):
    scene_id: Identifier
    requires: dict[Identifier, str] = Field(default_factory=dict)


class Scene(Record):
    scene_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    links: list[Link] = Field(default_factory=list)
    references: list[Identifier] = Field(default_factory=list)


class StatReference(Record):
    """An explicitly reviewed actor identity, never inferred from its name or prose."""

    kind: Literal["template", "module"]
    creature_name: str = Field(min_length=1, max_length=200)
    collection: str | None = Field(default=None, min_length=1)
    source_sha256: Digest | None = None
    rules_edition: SourceEdition | None = None
    passage_id: Identifier | None = None

    @model_validator(mode="after")
    def explicit_binding(self):
        if self.kind == "template":
            if (
                not self.collection
                or not self.source_sha256
                or self.rules_edition not in ("2014", "2024")
                or self.passage_id
            ):
                raise ValueError(
                    "Templates require a collection, source checksum and explicit edition."
                )
        elif not self.passage_id or any((self.collection, self.source_sha256, self.rules_edition)):
            raise ValueError("Module statistics require a passage in this reviewed manifest.")
        return self


class Entity(Record):
    entity_id: Identifier
    kind: Literal["actor", "clue", "hazard", "ending"]
    title: str = Field(min_length=1, max_length=200)
    discovery_flag: Identifier | None = None
    stat_reference: StatReference | None = None


class Passage(Record):
    passage_id: Identifier
    page: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text_sha256: Digest | None = None
    scene_ids: list[Identifier] = Field(default_factory=list)
    entity_ids: list[Identifier] = Field(default_factory=list)
    audience: Literal["unreviewed", "player", "planner", "excluded"] = "unreviewed"
    requires: dict[Identifier, str] = Field(default_factory=dict)


class StoryDefinition(Record):
    """Optional explicitly reviewed story identity; never automatically player knowledge."""

    canonical_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    scene_ids: list[Identifier] = Field(min_length=1)
    passage_ids: list[Identifier] = Field(min_length=1)
    requires: dict[Identifier, str] = Field(default_factory=dict)
    statuses: list[str] = Field(default_factory=lambda: ["active", "completed"])
    reveal_on: Literal["success", "always"] = "success"
    clock_max: int | None = Field(default=None, ge=1, le=1000)
    consumable: bool = False
    portable: bool = True
    initial_owner: str | None = Field(default=None, max_length=200)


class AdventureManifest(Record):
    manifest_version: Literal[1] = 1
    source: str = Field(min_length=1)
    source_sha256: Digest
    extraction_sha256: Digest
    rules_edition: SourceEdition = "unspecified"
    opening_scene_id: Identifier | None = None
    scenes: list[Scene] = Field(min_length=1)
    entities: list[Entity] = Field(default_factory=list)
    passages: list[Passage] = Field(min_length=1)
    objects: list[StoryDefinition] = Field(default_factory=list)
    objectives: list[StoryDefinition] = Field(default_factory=list)
    milestones: list[StoryDefinition] = Field(default_factory=list)
    promises: list[StoryDefinition] = Field(default_factory=list)
    clocks: list[StoryDefinition] = Field(default_factory=list)
    reviewed_sha256: Digest | None = None
    reviewed_by: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def references_exist(self):
        scenes = {s.scene_id for s in self.scenes}
        entities = {e.entity_id for e in self.entities}
        passages = {p.passage_id for p in self.passages}
        if (
            len(scenes) != len(self.scenes)
            or len(entities) != len(self.entities)
            or len(passages) != len(self.passages)
        ):
            raise ValueError("Canonical scene, entity and passage IDs must be unique.")
        if self.opening_scene_id is not None and self.opening_scene_id not in scenes:
            raise ValueError("Opening scene is not a canonical scene.")
        flags = [e.discovery_flag for e in self.entities if e.discovery_flag]
        if len(flags) != len(set(flags)):
            raise ValueError("Each discovery flag must identify one canonical entity.")
        for scene in self.scenes:
            if any(link.scene_id not in scenes for link in scene.links):
                raise ValueError("Branch link names an unknown scene.")
            if not set(scene.references) <= passages:
                raise ValueError("Cross reference names an unknown passage.")
        for passage in self.passages:
            if passage.end <= passage.start:
                raise ValueError("Passage span is empty or reversed.")
            if not set(passage.scene_ids) <= scenes or not set(passage.entity_ids) <= entities:
                raise ValueError("Passage names an unknown scene or entity.")
            if passage.audience not in ("excluded", "unreviewed") and not passage.scene_ids:
                raise ValueError("Playable passages need an explicit scene scope.")
        for category in (self.objects, self.objectives, self.milestones, self.promises, self.clocks):
            if len({d.canonical_id for d in category}) != len(category):
                raise ValueError("Story definition IDs must be unique within their kind.")
            for definition in category:
                if not set(definition.scene_ids) <= scenes or not set(definition.passage_ids) <= passages:
                    raise ValueError("Story definition names an unknown scene or passage.")
                if not definition.statuses or any(status not in {
                    "discovered", "encountered", "visited", "active", "completed", "failed",
                    "fulfilled", "broken", "advanced", "friendly", "neutral", "hostile",
                    "acquired", "consumed", "moved", "used", "changed",
                } for status in definition.statuses):
                    raise ValueError("Story definition has unsupported statuses.")
        for clock in self.clocks:
            if clock.clock_max is None:
                raise ValueError("Reviewed clocks need an explicit maximum.")
        for entity in self.entities:
            reference = entity.stat_reference
            if reference is None:
                continue
            if entity.kind != "actor":
                raise ValueError("Only actors can reference creature statistics.")
            if reference.kind == "template" and reference.rules_edition != self.rules_edition:
                raise ValueError("Actor template edition must match its adventure manifest.")
            if reference.kind == "module":
                passage = next(
                    (p for p in self.passages if p.passage_id == reference.passage_id), None
                )
                if (
                    passage is None
                    or entity.entity_id not in passage.entity_ids
                    or passage.audience not in ("player", "planner")
                ):
                    raise ValueError(
                        "Module statistics must be a playable reviewed passage for this actor."
                    )
        return self


def manifest_digest(manifest: AdventureManifest) -> str:
    """Receipt covers all structure/classification; editing a published draft revokes it."""
    body = manifest.model_dump(exclude={"reviewed_sha256", "reviewed_by"})
    # Absent optional actor bindings must not revoke receipts issued before this extension.
    # Adding or changing an explicit binding still changes the complete review digest.
    for entity in body["entities"]:
        if entity["stat_reference"] is None:
            entity.pop("stat_reference")
    for key in ("objects", "objectives", "milestones", "promises", "clocks"):
        if not body[key]:
            body.pop(key)
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text_digest(encoded)


def require_published(
    value: dict | AdventureManifest, expected: dict | None = None
) -> AdventureManifest:
    try:
        manifest = AdventureManifest.model_validate(
            value.model_dump() if isinstance(value, AdventureManifest) else value
        )
    except (ValueError, TypeError) as exc:
        raise ManifestError("Adventure manifest is invalid. Compile and review it again.") from exc
    if (
        not manifest.reviewed_by
        or manifest.reviewed_sha256 != manifest_digest(manifest)
        or any(p.audience == "unreviewed" or not p.text_sha256 for p in manifest.passages)
    ):
        raise ManifestError("Adventure manifest is unreviewed or changed after review.")
    if not manifest.opening_scene_id or not any(
        p.audience == "player" and manifest.opening_scene_id in p.scene_ids and not p.requires
        for p in manifest.passages
    ):
        raise ManifestError("Choose an opening scene with unconditional reviewed player text.")
    for field in ("source_sha256", "extraction_sha256", "rules_edition"):
        if (
            expected
            and expected.get(field) is not None
            and getattr(manifest, field) != expected[field]
        ):
            raise ManifestError(
                f"Adventure manifest has a stale {field}; review the current source."
            )
    return manifest


def load_published_manifest(metadata: dict) -> AdventureManifest:
    """Existing adventure JSON is the authority; legacy sources remain explicitly unreviewed."""
    if not metadata.get("manifest"):
        raise ManifestError("This adventure needs a reviewed scene manifest before module play.")
    expected = dict(metadata.get("ingestion") or {})
    if metadata.get("rules_edition") is not None:
        expected["rules_edition"] = metadata["rules_edition"]
    if not expected.get("source_sha256") or not expected.get("extraction_sha256"):
        raise ManifestError("This adventure has no validated extraction identity for its manifest.")
    return require_published(metadata["manifest"], expected)


def select_scene_context(
    value: dict | AdventureManifest,
    current_scene_id: str | None,
    world_flags: dict[str, str] | None = None,
) -> dict:
    """Current scene + explicit cross references, gated by already persisted prerequisites.

    Links expose eligible canonical destinations to the planner without loading their text.
    No transitive traversal or semantic search can pull in an unrelated future scene.
    """
    manifest = require_published(value)
    scene = next((s for s in manifest.scenes if s.scene_id == current_scene_id), None)
    if scene is None:
        raise ManifestError("Current scene is absent or unknown; select a reviewed opening scene.")
    flags = world_flags or {}

    def unlocked(requirements):
        return all(flags.get(key) == value for key, value in requirements.items())

    selected = [
        p
        for p in manifest.passages
        if (scene.scene_id in p.scene_ids or p.passage_id in scene.references)
        and unlocked(p.requires)
        and p.audience in ("player", "planner")
    ]
    entity_ids = {eid for p in selected for eid in p.entity_ids}
    return {
        "scene_id": scene.scene_id,
        "player_passage_ids": [p.passage_id for p in selected if p.audience == "player"],
        "planner_passage_ids": [p.passage_id for p in selected if p.audience == "planner"],
        "allowed_scene_ids": [scene.scene_id]
        + [link.scene_id for link in scene.links if unlocked(link.requires)],
        "entities": [e.model_dump() for e in manifest.entities if e.entity_id in entity_ids],
    }
