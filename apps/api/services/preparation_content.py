"""Bounded authoring schemas and deterministic canonical compilation for the DM workflow."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.artifacts import text_digest
from edgar_core.identifiers import Identifier
from edgar_core.manifests import (
    AdventureManifest,
    Entity,
    OpeningJournalSeed,
    Passage,
    ResolutionPolicy,
    Scene,
    StoryDefinition,
    manifest_digest,
    require_published,
)
from edgar_core.prepared import (
    PreparedAct,
    PreparedBranch,
    PreparedCampaign,
    PreparedExtension,
    PreparedOutcome,
    PreparedQuest,
    PreparedRoute,
    PreparedScenePacket,
    validate_prepared_campaign,
)
from ingestion.extract import ExtractedPage, Extraction
from ingestion.manifest import validate_spans
from ingestion.quality import check_text


class ContentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActDesign(ContentRecord):
    act_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    scene_ids: list[Identifier] = Field(min_length=1, max_length=12)
    summary: str = Field(min_length=50, max_length=4000)
    role: Literal["opening", "development", "climax", "resolution"]
    advance_ids: list[Identifier] = Field(min_length=1, max_length=20)
    climax_ids: list[Identifier] = Field(default_factory=list, max_length=5)
    source_pages: list[int] = Field(default_factory=list, max_length=500)
    seed_text: str = Field(min_length=100, max_length=8000)


class CampaignDesign(ContentRecord):
    title: str = Field(min_length=1, max_length=200)
    premise: str = Field(min_length=50, max_length=5000)
    opening_scene_id: Identifier
    acts: list[ActDesign] = Field(min_length=1, max_length=24)
    # Explicit shared identities/motives constrain all independently written acts.
    continuity: list[str] = Field(min_length=1, max_length=50)


class CanonicalAct(ContentRecord):
    """An act fragment; cross-act references are validated after exact global assembly."""

    act_id: Identifier
    scenes: list[Scene] = Field(min_length=1, max_length=12)
    entities: list[Entity] = Field(default_factory=list, max_length=40)
    passages: list[Passage] = Field(min_length=1, max_length=150)
    objectives: list[StoryDefinition] = Field(min_length=1, max_length=20)
    milestones: list[StoryDefinition] = Field(default_factory=list, max_length=30)
    objects: list[StoryDefinition] = Field(default_factory=list, max_length=40)
    promises: list[StoryDefinition] = Field(default_factory=list, max_length=20)
    clocks: list[StoryDefinition] = Field(default_factory=list, max_length=20)
    opening_journal: list[OpeningJournalSeed] = Field(default_factory=list, max_length=20)
    resolution_policies: list[ResolutionPolicy] = Field(min_length=1, max_length=50)
    act: PreparedAct
    quests: list[PreparedQuest] = Field(min_length=1, max_length=20)
    scene_packets: list[PreparedScenePacket] = Field(min_length=1, max_length=12)
    routes: list[PreparedRoute] = Field(default_factory=list, max_length=30)
    branches: list[PreparedBranch] = Field(min_length=2, max_length=50)
    outcomes: list[PreparedOutcome] = Field(min_length=4, max_length=100)
    extensions: list[PreparedExtension] = Field(default_factory=list, max_length=40)


class ScenePreparation(ContentRecord):
    act_id: Identifier
    packets: list[PreparedScenePacket] = Field(min_length=1, max_length=12)
    extensions: list[PreparedExtension] = Field(default_factory=list, max_length=40)
    resource_plan: str = Field(min_length=50, max_length=5000)
    unsupported_required_mechanics: list[str] = Field(default_factory=list, max_length=50)


class CompleteChapter(ContentRecord):
    act_id: Identifier
    # Exactly one complete act, not a summary or a retrieval chunk.
    text: str = Field(min_length=500, max_length=60000)


def design_from(artifacts) -> CampaignDesign:
    matches = [a for a in artifacts if a.artifact_kind == "campaign_design"]
    if len(matches) != 1:
        raise ValueError("One whole-campaign design is required.")
    return CampaignDesign.model_validate(matches[0].payload["design"])


def extraction_for_design(binding, design: CampaignDesign) -> Extraction:
    if binding.source_mode == "pdf":
        from .preparation_sources import load_source

        return load_source(binding)
    pages = [
        ExtractedPage(
            page=i, text=act.seed_text, method="generated", quality=check_text(act.seed_text)
        )
        for i, act in enumerate(design.acts, 1)
    ]
    extraction = Extraction(binding.source_slug, "0" * 64, len(pages), pages, binding.rules_edition)
    extraction.source_sha256 = extraction.text_sha256()
    return extraction


def validate_design(binding, design: CampaignDesign, extraction: Extraction | None = None) -> None:
    acts = [act.act_id for act in design.acts]
    scenes = [scene for act in design.acts for scene in act.scene_ids]
    if (
        len(acts) != len(set(acts))
        or len(scenes) != len(set(scenes))
        or design.opening_scene_id not in design.acts[0].scene_ids
    ):
        raise ValueError("Canonical design identities or opening are invalid.")
    count = binding.commission.get("act_count")
    if count is None and binding.source_mode == "ai":
        size = binding.commission.get("generation", {}).get("size")
        count = {"small": 3, "medium": 6, "large": 12, "gigantic": 24}.get(size)
    if count is not None and len(acts) != count:
        raise ValueError("Every commissioned act must be designed.")
    if binding.source_mode == "pdf":
        if extraction is None:
            raise ValueError("Exact source is required.")
        assigned = [page for act in design.acts for page in act.source_pages]
        required = {page.page for page in extraction.pages if page.text.strip()}
        if len(assigned) != len(set(assigned)) or set(assigned) != required:
            raise ValueError("Every nonblank physical page must belong to exactly one act.")


def validate_fragment(binding, design: CampaignDesign, fragment: CanonicalAct) -> None:
    act = next(act for act in design.acts if act.act_id == fragment.act_id)
    if fragment.act.act_id != act.act_id or set(fragment.act.scene_ids) != set(act.scene_ids):
        raise ValueError("Canonical act identity changed.")
    if {scene.scene_id for scene in fragment.scenes} != set(act.scene_ids):
        raise ValueError("Every planned scene must be compiled.")
    extraction = extraction_for_design(binding, design)
    allowed_pages = (
        set(act.source_pages) if binding.source_mode == "pdf" else {design.acts.index(act) + 1}
    )
    pages = dict(extraction.text_pages())
    spans = sorted(fragment.passages, key=lambda p: (p.page, p.start))
    if any(p.page not in allowed_pages for p in spans):
        raise ValueError("An act cannot claim another act's source pages.")
    for page in allowed_pages:
        cursor = 0
        for passage in (p for p in spans if p.page == page):
            if (
                passage.start < cursor
                or passage.end > len(pages[page])
                or pages[page][cursor : passage.start].strip()
            ):
                raise ValueError("Every exact source character must be classified once.")
            if (
                not pages[page][passage.start : passage.end].strip()
                or passage.audience == "unreviewed"
            ):
                raise ValueError("Every passage requires an explicit private DM classification.")
            passage.text_sha256 = text_digest(pages[page][passage.start : passage.end])
            cursor = passage.end
        if pages[page][cursor:].strip():
            raise ValueError("An act left source text unclassified.")


def assemble_manifest(
    binding, artifacts, *, final: bool = False
) -> tuple[AdventureManifest, Extraction]:
    design = design_from(artifacts)
    fragments = [
        CanonicalAct.model_validate(a.payload["fragment"])
        for a in artifacts
        if a.artifact_kind == "canonical_act"
    ]
    by_act = {fragment.act_id: fragment for fragment in fragments}
    if len(by_act) != len(fragments) or set(by_act) != {act.act_id for act in design.acts}:
        raise ValueError("Every act requires exactly one canonical fragment.")
    fragments = [by_act[act.act_id] for act in design.acts]
    extraction = extraction_for_design(binding, design)
    for fragment in fragments:
        validate_fragment(binding, design, fragment)
    fields = {
        name: [item for fragment in fragments for item in getattr(fragment, name)]
        for name in (
            "scenes",
            "entities",
            "passages",
            "objectives",
            "milestones",
            "objects",
            "promises",
            "clocks",
            "opening_journal",
            "resolution_policies",
        )
    }
    prepared = PreparedCampaign(
        source_mode=binding.source_mode,
        acts=[fragment.act for fragment in fragments],
        **{
            name: [item for fragment in fragments for item in getattr(fragment, name)]
            for name in ("quests", "scene_packets", "routes", "branches", "outcomes", "extensions")
        },
    )
    scene_work = [
        ScenePreparation.model_validate(a.payload["scenes"])
        for a in artifacts
        if a.artifact_kind == "runnable_scenes"
    ]
    if scene_work:
        if {a.act_id for a in scene_work} != set(by_act) or len(scene_work) != len(by_act):
            raise ValueError("Every act requires runnable scenes.")
        prepared.scene_packets = [packet for work in scene_work for packet in work.packets]
        prepared.extensions += [extension for work in scene_work for extension in work.extensions]
    if final and binding.source_mode == "ai":
        chapters = {
            a.payload["chapter"]["act_id"]: CompleteChapter.model_validate(a.payload["chapter"])
            for a in artifacts
            if a.artifact_kind == "complete_chapter"
        }
        if set(chapters) != set(by_act):
            raise ValueError("Every act needs a complete retained chapter.")
        final_pages = []
        for number, act in enumerate(design.acts, 1):
            chapter = chapters[act.act_id]
            # Exact source seed is retained verbatim at the start. Expanded private text
            # is classified by code as planner-only, with no new player disclosures.
            seed = extraction.pages[number - 1].text
            text = seed + "\n\n" + chapter.text
            final_pages.append(ExtractedPage(number, text, "generated", check_text(text)))
            fields["passages"].append(
                Passage(
                    passage_id=f"dm_chapter_{act.act_id}",
                    page=number,
                    start=len(seed) + 2,
                    end=len(text),
                    text_sha256=text_digest(chapter.text),
                    scene_ids=act.scene_ids,
                    audience="planner",
                )
            )
        extraction = Extraction(
            binding.source_slug, "0" * 64, len(final_pages), final_pages, binding.rules_edition
        )
        extraction.source_sha256 = extraction.text_sha256()
    manifest = AdventureManifest(
        source=binding.source_slug,
        source_sha256=extraction.source_sha256,
        extraction_sha256=extraction.text_sha256(),
        rules_edition=binding.rules_edition,
        opening_scene_id=design.opening_scene_id,
        prepared=prepared,
        **fields,
    )
    validate_spans(extraction, manifest)
    report = validate_prepared_campaign(manifest)
    if not report.valid:
        raise ValueError(
            "Canonical prepared campaign failed deterministic validation: "
            + json.dumps([f.code for f in report.findings])
        )
    manifest.reviewed_by = "edgar-private-dm-code-validation-v1"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return require_published(manifest), extraction
