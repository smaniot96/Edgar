"""Whole-adventure source consistency, critic and repair contracts.

This module consumes the existing reviewed manifest and its one ``PreparedCampaign``.
It does not create another narrative graph, and it never treats a model's verdict as a
validation receipt. Reports are private and bound to the exact preparation artifacts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.manifests import (
    AdventureManifest,
    manifest_digest,
)
from edgar_core.preparation import (
    PreparationArtifact,
    PreparationFinding,
    PreparationRunBinding,
    PreparationStage,
    PreparationTask,
    ValidationResult,
    assert_artifact_integrity,
    build_artifact,
    build_validation_receipt,
    canonical_sha256,
)
from edgar_core.prepared import (
    PreparedCampaign,
    PreparedFinding,
    prepared_digest,
    validate_prepared_campaign,
)


class CoherenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ContinuityAssertion(CoherenceRecord):
    """A structured identity/motive/knowledge statement for deterministic comparison."""

    subject_id: str = Field(min_length=1, max_length=128)
    field: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1000)
    passage_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    temporal_scope: Literal["stable", "chapter_local"] = "stable"


class ManuscriptEvent(CoherenceRecord):
    event_id: str = Field(min_length=1, max_length=128)
    chapter_id: str = Field(min_length=1, max_length=128)
    order: int = Field(default=1, ge=1, le=1000)
    after_event_ids: tuple[str, ...] = Field(default=(), max_length=128)


class RetainedChapter(CoherenceRecord):
    """One complete retained chapter, never a retrieval chunk or critic excerpt."""

    chapter_id: str = Field(min_length=1, max_length=128)
    act_id: str = Field(min_length=1, max_length=128)
    order: int = Field(ge=1, le=1000)
    role: Literal[
        "setup",
        "discovery",
        "escalation",
        "reversal",
        "confrontation",
        "resolution",
        "aftermath",
        "transition",
    ]
    scene_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    advance_ids: tuple[str, ...] = Field(max_length=128)
    climax_ids: tuple[str, ...] = Field(default=(), max_length=32)
    passage_ids: tuple[str, ...] = ()
    continuity_assertions: tuple[ContinuityAssertion, ...] = Field(default=(), max_length=500)
    events: tuple[ManuscriptEvent, ...] = Field(default=(), max_length=500)
    text: str = Field(min_length=1, max_length=2_000_000)


class RetainedManuscript(CoherenceRecord):
    """Canonical full manuscript inventory used by the P09 critic and P10 review."""

    schema_version: Literal[1] = 1
    source_mode: Literal["pdf", "ai"]
    chapters: tuple[RetainedChapter, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_chapters(self) -> RetainedManuscript:
        ids = [chapter.chapter_id for chapter in self.chapters]
        orders = [chapter.order for chapter in self.chapters]
        if len(ids) != len(set(ids)) or len(orders) != len(set(orders)):
            raise ValueError("Retained manuscript chapter IDs and order values must be unique.")
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("Retained manuscript chapters must have contiguous order values.")
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("Retained manuscript chapters must be stored in canonical order.")
        return self


class CoherenceFinding(CoherenceRecord):
    finding_id: str = Field(pattern=r"^coh-[0-9a-f]{24}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    severity: Literal["info", "low", "medium", "high", "critical"]
    message: str = Field(min_length=1, max_length=1000)
    evidence_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    affected_ids: tuple[str, ...] = Field(default=(), max_length=128)
    status: Literal["open", "resolved", "accepted_with_evidence"] = "open"
    resolution_artifact_ids: tuple[str, ...] = ()


class ReviewBindings(CoherenceRecord):
    run_id: str
    run_binding_sha256: str
    source_binding_sha256: str
    hero_party_sha256: str
    profile_snapshot_sha256: str
    manifest_sha256: str
    manifest_artifact_id: str
    prepared_sha256: str
    prepared_artifact_id: str
    manuscript_sha256: str
    manuscript_artifact_id: str
    grounding_artifact_ids: tuple[str, ...]
    artifact_digests: dict[str, str]


class SourceScaffoldReport(CoherenceRecord):
    """Source and structure consistency, without a simulated play/completion claim."""

    schema_version: Literal[1] = 1
    report_mode: Literal["source_scaffold"] = "source_scaffold"
    bindings: ReviewBindings
    opening_scene_id: str | None
    scene_ids: tuple[str, ...]
    topology_semantics: Literal["directed_routes_ignoring_predicate_satisfaction"] = (
        "directed_routes_ignoring_predicate_satisfaction"
    )
    topologically_reachable_scene_ids: tuple[str, ...]
    route_ids: tuple[str, ...]
    classified_route_ids: tuple[str, ...]
    objective_ids: tuple[str, ...]
    required_objective_ids: tuple[str, ...]
    source_backed_objective_ids: tuple[str, ...]
    packet_covered_objective_ids: tuple[str, ...]
    milestone_ids: tuple[str, ...]
    required_milestone_ids: tuple[str, ...]
    source_backed_milestone_ids: tuple[str, ...]
    packet_covered_milestone_ids: tuple[str, ...]
    ending_entity_ids: tuple[str, ...]
    source_backed_ending_entity_ids: tuple[str, ...]
    grounded_actor_encounter_ids: tuple[str, ...]
    grounded_actor_ids: tuple[str, ...]
    findings: tuple[CoherenceFinding, ...] = Field(max_length=4000)


class AdventureReviewReport(CoherenceRecord):
    schema_version: Literal[1] = 1
    bindings: ReviewBindings
    chapter_ids: tuple[str, ...]
    scene_ids: tuple[str, ...]
    findings: tuple[CoherenceFinding, ...] = Field(max_length=4000)
    # Archived P09 reports may retain an old path payload; it is never executed.
    path_report: dict[str, Any] | None = None


class CriticEvidence(CoherenceRecord):
    artifact_id: str
    artifact_sha256: str
    json_pointer: str = Field(pattern=r"^/(?:[^/~]|~[01])*(?:/(?:[^/~]|~[01])*)*$")
    excerpt: str = Field(min_length=1, max_length=4000)
    excerpt_sha256: str


class CriticFinding(CoherenceRecord):
    finding_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    severity: Literal["info", "low", "medium", "high", "critical"]
    message: str = Field(min_length=1, max_length=1000)
    evidence: tuple[CriticEvidence, ...] = Field(min_length=1, max_length=16)
    affected_ids: tuple[str, ...] = Field(default=(), max_length=128)


class CriticReport(CoherenceRecord):
    """Typed semantic critic answer. This object is evidence, never approval."""

    schema_version: Literal[1] = 1
    bindings: ReviewBindings
    reviewed_chapter_text_sha256: dict[str, str]
    findings: tuple[CriticFinding, ...] = Field(max_length=1000)


class RepairReplacement(CoherenceRecord):
    replaces_artifact_id: str
    replacement_artifact_id: str
    original_version: int = Field(ge=1)
    replacement_version: int = Field(ge=2)

    @model_validator(mode="after")
    def version_increments(self) -> RepairReplacement:
        if self.replacement_version <= self.original_version:
            raise ValueError("A replacement must use a newer artifact version.")
        return self


class RepairEnvelope(CoherenceRecord):
    """P11 receipt of its bounded targets and immutable evidence snapshot."""

    schema_version: Literal[1] = 1
    run_binding_sha256: str
    source_binding_sha256: str
    finding_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    affected_artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=256)
    replacements: tuple[RepairReplacement, ...] = Field(min_length=1, max_length=256)
    preserved_source_artifact_digests: dict[str, str]
    preserved_revealed_fact_digests: dict[str, str]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _finding_id(
    code: str, path: str, evidence_ids: tuple[str, ...], affected: tuple[str, ...]
) -> str:
    return (
        "coh-"
        + str(
            canonical_sha256(
                {
                    "code": code,
                    "path": path,
                    "evidence": sorted(evidence_ids),
                    "affected": sorted(affected),
                }
            )
        )[:24]
    )


def _coherence_finding(
    code: str,
    message: str,
    evidence_ids: tuple[str, ...],
    *,
    affected_ids: tuple[str, ...] = (),
    severity: str = "high",
) -> CoherenceFinding:
    return CoherenceFinding(
        finding_id=_finding_id(code, message, evidence_ids, affected_ids),
        code=code,
        severity=severity,  # type: ignore[arg-type]
        message=message,
        evidence_artifact_ids=tuple(dict.fromkeys(evidence_ids)),
        affected_ids=tuple(dict.fromkeys(affected_ids)),
    )


def _manifest(artifact: PreparationArtifact) -> AdventureManifest:
    return AdventureManifest.model_validate(artifact.payload["manifest"])


def _prepared(artifact: PreparationArtifact) -> PreparedCampaign:
    return PreparedCampaign.model_validate(artifact.payload["prepared"])


def _manuscript(artifact: PreparationArtifact) -> RetainedManuscript:
    return RetainedManuscript.model_validate(artifact.payload["manuscript"], strict=False)


def _unique_artifacts(
    artifacts: tuple[PreparationArtifact, ...],
) -> tuple[PreparationArtifact, ...]:
    by_id: dict[str, PreparationArtifact] = {}
    for artifact in artifacts:
        existing = by_id.get(artifact.artifact_id)
        if existing is not None and existing.content_sha256 != artifact.content_sha256:
            raise ValueError("One artifact ID is bound to conflicting content digests.")
        by_id[artifact.artifact_id] = artifact
    return tuple(by_id[key] for key in sorted(by_id))


def build_review_bindings(
    binding: PreparationRunBinding,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    artifacts: tuple[PreparationArtifact, ...],
) -> ReviewBindings:
    manifest = _manifest(manifest_artifact)
    prepared = _prepared(prepared_artifact)
    manuscript = _manuscript(manuscript_artifact)
    grounding = tuple(
        sorted(a.artifact_id for a in artifacts if a.artifact_kind == "grounding_report")
    )
    if len(grounding) != 1:
        raise ValueError(
            "A whole-adventure review must bind exactly one accepted grounding report."
        )
    if (
        manifest.source_sha256
        and binding.source_sha256
        and manifest.source_sha256 != binding.source_sha256
    ):
        raise ValueError(
            "Reviewed manifest source digest does not match the frozen preparation binding."
        )
    if manuscript.source_mode != binding.source_mode or prepared.source_mode != binding.source_mode:
        raise ValueError(
            "Prepared campaign and retained manuscript must use the frozen source mode."
        )
    if (
        binding.manuscript_sha256
        and binding.manuscript_sha256 != manuscript_artifact.content_sha256
    ):
        raise ValueError(
            "Retained manuscript digest does not match the frozen preparation binding."
        )
    return ReviewBindings(
        run_id=binding.run_id,
        run_binding_sha256=binding.binding_sha256,
        source_binding_sha256=binding.source_binding_sha256,
        hero_party_sha256=binding.hero_party_sha256,
        profile_snapshot_sha256=binding.profile_snapshot_sha256,
        manifest_sha256=manifest_digest(manifest),
        manifest_artifact_id=manifest_artifact.artifact_id,
        prepared_sha256=prepared_digest(prepared),
        prepared_artifact_id=prepared_artifact.artifact_id,
        manuscript_sha256=manuscript_artifact.content_sha256,
        manuscript_artifact_id=manuscript_artifact.artifact_id,
        grounding_artifact_ids=grounding,
        artifact_digests={a.artifact_id: a.content_sha256 for a in artifacts},
    )


def _review_subjects(
    proposed_artifacts: tuple[PreparationArtifact, ...],
    dependencies: tuple[PreparationArtifact, ...],
) -> tuple[
    PreparationArtifact, PreparationArtifact, PreparationArtifact, tuple[PreparationArtifact, ...]
]:
    """Resolve final P09 subject artifacts from the proposal and retain exact dependencies.

    P09 is the stage that materializes the final manifest, prepared campaign and full
    manuscript. Earlier versions can remain in the accepted dependency inventory, but they
    are never silently selected as the subject of a review.
    """

    selected: list[PreparationArtifact] = []
    for kind in ("canonical_manifest", "prepared_campaign", "retained_manuscript"):
        matches = [artifact for artifact in proposed_artifacts if artifact.artifact_kind == kind]
        if len(matches) != 1:
            raise ValueError(f"P09 must propose exactly one final {kind} artifact.")
        selected.append(matches[0])
    manifest_artifact, prepared_artifact, manuscript_artifact = selected
    reports = {"deterministic_review_report", "semantic_critic_report"}
    additional_subjects = tuple(
        artifact for artifact in proposed_artifacts if artifact.artifact_kind not in reports
    )
    inventory = _unique_artifacts((*dependencies, *additional_subjects))
    return manifest_artifact, prepared_artifact, manuscript_artifact, inventory


def _prepared_findings(
    rows: list[PreparedFinding], evidence_ids: tuple[str, ...]
) -> list[CoherenceFinding]:
    return [
        _coherence_finding(
            row.code,
            f"{row.path}: {row.message}",
            evidence_ids,
            affected_ids=(row.path,),
            severity="high",
        )
        for row in rows
    ]


def _structural_findings(
    manifest: AdventureManifest,
    prepared: PreparedCampaign,
    manuscript: RetainedManuscript,
    evidence_ids: tuple[str, ...],
) -> list[CoherenceFinding]:
    findings = _prepared_findings(
        validate_prepared_campaign(manifest, prepared).findings, evidence_ids
    )
    acts = {act.act_id: act for act in prepared.acts}
    chapter_by_id = {chapter.chapter_id: chapter for chapter in manuscript.chapters}
    chapter_scenes = [scene_id for chapter in manuscript.chapters for scene_id in chapter.scene_ids]
    if len(chapter_scenes) != len(set(chapter_scenes)):
        findings.append(
            _coherence_finding(
                "chapter_scene_repeated",
                "A canonical scene is assigned to more than one manuscript chapter.",
                evidence_ids,
                affected_ids=tuple(chapter_scenes),
            )
        )
    known_scenes = {scene.scene_id for scene in manifest.scenes}
    if set(chapter_scenes) != known_scenes:
        findings.append(
            _coherence_finding(
                "chapter_scene_coverage",
                "The retained manuscript must cover every canonical scene exactly once.",
                evidence_ids,
                affected_ids=tuple(sorted(known_scenes - set(chapter_scenes))),
            )
        )
    for chapter in manuscript.chapters:
        if chapter.act_id not in acts:
            findings.append(
                _coherence_finding(
                    "chapter_unknown_act",
                    f"Chapter {chapter.chapter_id} references an unknown prepared act.",
                    evidence_ids,
                    affected_ids=(chapter.chapter_id, chapter.act_id),
                )
            )
        elif not set(chapter.scene_ids) <= set(acts[chapter.act_id].scene_ids):
            findings.append(
                _coherence_finding(
                    "chapter_act_scene_mismatch",
                    f"Chapter {chapter.chapter_id} uses scenes outside its prepared act.",
                    evidence_ids,
                    affected_ids=(chapter.chapter_id, chapter.act_id),
                )
            )
        if (
            set(chapter.passage_ids)
            - {passage.passage_id for passage in manifest.passages}
            - {extension.passage_id for extension in prepared.extensions}
        ):
            findings.append(
                _coherence_finding(
                    "chapter_unknown_passage",
                    f"Chapter {chapter.chapter_id} cites a passage absent from the retained manifest and DM extensions.",
                    evidence_ids,
                    affected_ids=(chapter.chapter_id,),
                )
            )

    advances: dict[str, list[str]] = {}
    climaxes: dict[str, list[str]] = {}
    for chapter in manuscript.chapters:
        for advance in chapter.advance_ids:
            if advance not in {
                row.canonical_id for row in (*manifest.objectives, *manifest.milestones)
            }:
                findings.append(
                    _coherence_finding(
                        "chapter_unknown_advance",
                        f"Chapter {chapter.chapter_id} declares an unknown objective or milestone advance.",
                        evidence_ids,
                        affected_ids=(chapter.chapter_id, advance),
                    )
                )
            advances.setdefault(advance, []).append(chapter.chapter_id)
        for climax in chapter.climax_ids:
            climaxes.setdefault(climax, []).append(chapter.chapter_id)
    for advance_id, chapter_ids in advances.items():
        if len(chapter_ids) > 1:
            findings.append(
                _coherence_finding(
                    "repeated_chapter_advance",
                    f"The same declared story advance is assigned to multiple chapters: {advance_id}.",
                    evidence_ids,
                    affected_ids=tuple(chapter_ids),
                )
            )
    for climax_id, chapter_ids in climaxes.items():
        if len(chapter_ids) > 1:
            findings.append(
                _coherence_finding(
                    "repeated_climax",
                    f"A declared climax repeats across chapters: {climax_id}.",
                    evidence_ids,
                    affected_ids=tuple(chapter_ids),
                )
            )
    # A campaign with chapters in every act must make at least one new declared advance.
    expected_act_ids = {act.act_id for act in prepared.acts}
    covered_act_ids = {chapter.act_id for chapter in manuscript.chapters}
    if covered_act_ids != expected_act_ids:
        findings.append(
            _coherence_finding(
                "manuscript_act_coverage",
                "Every prepared act must have retained chapter prose.",
                evidence_ids,
                affected_ids=tuple(sorted(expected_act_ids - covered_act_ids)),
            )
        )

    # Compare stable actor/clue/quest IDs, not display names, when checking motives and facts.
    assertion_values: dict[tuple[str, str], dict[str, list[str]]] = {}
    for chapter in manuscript.chapters:
        for assertion in chapter.continuity_assertions:
            if assertion.subject_id not in _canonical_identity_ids(manifest, prepared):
                findings.append(
                    _coherence_finding(
                        "unknown_continuity_identity",
                        "A continuity assertion uses an unknown canonical identity.",
                        evidence_ids,
                        affected_ids=(chapter.chapter_id, assertion.subject_id),
                    )
                )
            if (
                set(assertion.passage_ids)
                - {row.passage_id for row in manifest.passages}
                - {extension.passage_id for extension in prepared.extensions}
            ):
                findings.append(
                    _coherence_finding(
                        "continuity_evidence_missing",
                        "A continuity assertion cites evidence absent from the retained source or extensions.",
                        evidence_ids,
                        affected_ids=(chapter.chapter_id, assertion.subject_id),
                    )
                )
            if assertion.temporal_scope == "stable":
                assertion_values.setdefault((assertion.subject_id, assertion.field), {}).setdefault(
                    assertion.value, []
                ).append(chapter.chapter_id)
    for (subject_id, field), values in assertion_values.items():
        if len(values) > 1:
            affected = tuple(sorted({chapter_id for ids in values.values() for chapter_id in ids}))
            findings.append(
                _coherence_finding(
                    "continuity_contradiction",
                    f"Canonical identity {subject_id} has conflicting {field} assertions across chapters.",
                    evidence_ids,
                    affected_ids=affected + (subject_id,),
                )
            )

    event_by_id = {
        event.event_id: event for chapter in manuscript.chapters for event in chapter.events
    }
    if len(event_by_id) != sum(len(chapter.events) for chapter in manuscript.chapters):
        findings.append(
            _coherence_finding(
                "duplicate_event_identity",
                "Retained manuscript event IDs must be unique.",
                evidence_ids,
            )
        )
    order_by_chapter = {chapter.chapter_id: chapter.order for chapter in manuscript.chapters}
    event_orders: dict[tuple[str, int], str] = {}
    for event in event_by_id.values():
        if event.chapter_id not in chapter_by_id:
            findings.append(
                _coherence_finding(
                    "event_unknown_chapter",
                    f"Event {event.event_id} references an unknown chapter.",
                    evidence_ids,
                    affected_ids=(event.event_id, event.chapter_id),
                )
            )
            continue
        event_key = (event.chapter_id, event.order)
        if event_key in event_orders:
            findings.append(
                _coherence_finding(
                    "duplicate_chapter_event_order",
                    f"Chapter {event.chapter_id} repeats event order {event.order}.",
                    evidence_ids,
                    affected_ids=(event_orders[event_key], event.event_id),
                )
            )
        else:
            event_orders[event_key] = event.event_id
        for predecessor_id in event.after_event_ids:
            predecessor = event_by_id.get(predecessor_id)
            if predecessor is None:
                findings.append(
                    _coherence_finding(
                        "event_prerequisite_missing",
                        f"Event {event.event_id} depends on an unknown event.",
                        evidence_ids,
                        affected_ids=(event.event_id, predecessor_id),
                    )
                )
            elif (
                order_by_chapter.get(predecessor.chapter_id, 0) > order_by_chapter[event.chapter_id]
            ):
                findings.append(
                    _coherence_finding(
                        "chronology_inversion",
                        f"Event {event.event_id} requires an event from a later chapter.",
                        evidence_ids,
                        affected_ids=(event.event_id, predecessor_id),
                    )
                )
            elif predecessor.chapter_id == event.chapter_id and predecessor.order >= event.order:
                findings.append(
                    _coherence_finding(
                        "chronology_inversion",
                        f"Event {event.event_id} requires an event that is not earlier in its chapter.",
                        evidence_ids,
                        affected_ids=(event.event_id, predecessor_id),
                    )
                )
    if _has_event_cycle(event_by_id):
        findings.append(
            _coherence_finding(
                "chronology_cycle",
                "Retained event prerequisites contain a chronology cycle.",
                evidence_ids,
            )
        )
    return _unique_findings(findings)


def _canonical_identity_ids(manifest: AdventureManifest, prepared: PreparedCampaign) -> set[str]:
    return (
        {row.scene_id for row in manifest.scenes}
        | {row.entity_id for row in manifest.entities}
        | {row.passage_id for row in manifest.passages}
        | {
            row.canonical_id
            for group in (
                manifest.objects,
                manifest.objectives,
                manifest.milestones,
                manifest.promises,
                manifest.clocks,
            )
            for row in group
        }
        | {row.quest_id for row in prepared.quests}
        | {row.branch_id for row in prepared.branches}
        | {row.outcome_id for row in prepared.outcomes}
        | {extension.passage_id for extension in prepared.extensions}
    )


def _has_event_cycle(events: Mapping[str, ManuscriptEvent]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(event_id: str) -> bool:
        if event_id in visiting:
            return True
        if event_id in visited:
            return False
        visiting.add(event_id)
        event = events[event_id]
        if any(parent in events and visit(parent) for parent in event.after_event_ids):
            return True
        visiting.remove(event_id)
        visited.add(event_id)
        return False

    return any(visit(event_id) for event_id in events)


def _unique_findings(rows: list[CoherenceFinding]) -> list[CoherenceFinding]:
    by_id = {row.finding_id: row for row in rows}
    return [by_id[key] for key in sorted(by_id)]


def _grounded_actor_events(
    binding: PreparationRunBinding,
    manifest: AdventureManifest,
    artifacts: tuple[PreparationArtifact, ...],
) -> dict[str, list[tuple[str, str]]]:
    """Validate source/stat bindings, including untouched historical party receipts."""
    from edgar_core.grounding import (
        EncounterGroundingReceipt,
        EncounterPlan,
        HeroPartySnapshot,
        SourceEncounterGroundingReceipt,
        VerifiedBookSnapshot,
    )

    grounded = []
    for artifact in artifacts:
        if artifact.artifact_kind not in {"act_grounding", "grounding_report"}:
            continue
        assert_artifact_integrity(binding, artifact)
        payloads = ([artifact.payload] if artifact.artifact_kind == "act_grounding"
                    else artifact.payload.get("acts", ()))
        for payload in payloads:
            if artifact.artifact_kind == "grounding_report" and (
                payload.get("book_snapshot_sha256") != artifact.payload.get("book_snapshot_sha256")
                or ("hero_party_sha256" in artifact.payload
                    and payload.get("grounding_scope") != "source"
                    and payload.get("hero_party_sha256") != artifact.payload["hero_party_sha256"])
            ):
                raise ValueError("Aggregated encounter evidence changed its exact input binding.")
            grounded.append((artifact, payload))
    if not grounded:
        return {}
    latest = {}
    for artifact, payload in grounded:
        act_id = payload["plan"]["act_id"]
        previous = latest.get(act_id)
        revision = (artifact.stage.ordinal, artifact.version)
        if previous is None or revision > previous[0]:
            latest[act_id] = (revision, payload)
        elif revision == previous[0] and payload != previous[1]:
            raise ValueError("Accepted encounter evidence repeats one act with conflicting plans.")
    # Historical party data remains an integrity anchor only for old receipts.
    # New source receipts never need a character snapshot to be sealed or reused.
    legacy_party_sha = None
    if any(payload.get("grounding_scope") != "source" for _, payload in latest.values()):
        legacy_party_sha = HeroPartySnapshot.model_validate(binding.hero_party_snapshot).party_sha256
    book_digests = {
        VerifiedBookSnapshot.model_validate(row.payload["snapshot"]).snapshot_sha256
        for row in artifacts if row.artifact_kind == "book_dossier"
    }
    actors = {row.entity_id: row for row in manifest.entities if row.kind == "actor"}
    events: dict[str, list[tuple[str, str]]] = {}
    for _, payload in latest.values():
        source_only = payload.get("grounding_scope") == "source"
        if (
            (not source_only and payload.get("hero_party_sha256") != legacy_party_sha)
            or (book_digests and payload.get("book_snapshot_sha256") not in book_digests)
        ):
            raise ValueError("Encounter evidence does not match its exact accepted inputs.")
        plans = {
            row["encounter_id"]: EncounterPlan.model_validate(row)
            for row in payload["plan"]["encounters"]
        }
        for raw in payload.get("encounters", ()):
            receipt = (SourceEncounterGroundingReceipt.model_validate(raw) if source_only
                       else EncounterGroundingReceipt.model_validate(raw))
            plan = plans.get(receipt.encounter_id)
            if (
                plan is None or not receipt.valid
                or receipt.plan_sha256 != plan.plan_sha256
                or receipt.scene_id != plan.scene_id
                or (not source_only and receipt.hero_party_sha256 != legacy_party_sha)
                or receipt.book_snapshot_sha256 != payload.get("book_snapshot_sha256")
                or receipt.rules_edition != manifest.rules_edition
                or set(receipt.actor_ids) != set(plan.actor_ids)
                or len(receipt.actor_ids) != len(set(receipt.actor_ids))
                or len(plan.actor_ids) != len(set(plan.actor_ids))
            ):
                raise ValueError("Actor event needs a valid exact accepted encounter receipt.")
            if source_only:
                requested = sorted((" ".join(row.creature_name.casefold().split()), row.actor_id or "", row.quantity, row.role)
                                   for row in plan.creatures)
                grounded_roster = sorted((" ".join(row.creature.creature_name.casefold().split()), row.actor_id or "",
                                         row.requested_quantity, row.role)
                                        for row in receipt.creatures)
                if requested != grounded_roster:
                    raise ValueError("Source encounter receipt changed its exact requested roster.")
            allocated: dict[tuple[str, str | None], int] = {}
            for actor_id in receipt.actor_ids:
                actor = actors.get(actor_id)
                reference = actor.stat_reference if actor else None
                if (
                    reference is None
                    or not any(actor_id in row.entity_ids and receipt.scene_id in row.scene_ids
                               for row in manifest.passages)
                ):
                    raise ValueError("Encounter actor is absent from its reviewed scene/stat binding.")
                matches = [
                    member for member in receipt.creatures
                    if member.effective_quantity > 0
                    and member.actor_id == actor_id
                    and member.creature.creature_name == reference.creature_name
                    and member.creature.rules_edition == manifest.rules_edition
                    and (
                        (reference.kind == "template"
                         and member.creature.collection == reference.collection
                         and member.creature.source_sha256 == reference.source_sha256)
                        or (reference.kind == "module"
                            and member.creature.collection == binding.source_slug
                            and member.creature.source_sha256 == manifest.source_sha256
                            and member.creature.extraction_sha256 == manifest.extraction_sha256
                            and member.creature.evidence_origin == "prepared_module"
                            and member.creature.module_evidence is not None
                            and member.creature.module_evidence.actor_id == actor_id
                            and member.creature.module_evidence.passage_id == reference.passage_id
                            and any(row.passage_id == reference.passage_id
                                    and row.text_sha256 == member.creature.text_sha256
                                    and row.page == member.creature.page
                                    for row in manifest.passages))
                    )
                ]
                if len(matches) != 1:
                    raise ValueError("Encounter actor has no unique exact grounded creature binding.")
                member = matches[0]
                key = (member.creature.creature_id, member.actor_id)
                allocated[key] = allocated.get(key, 0) + 1
                if allocated[key] > member.effective_quantity:
                    raise ValueError("Grounded encounter adaptation removed a declared actor.")
                events.setdefault(receipt.scene_id, []).append((receipt.encounter_id, actor_id))
    return {scene_id: sorted(set(rows)) for scene_id, rows in events.items()}


def _scaffold_route_id(source: str, destination: str, requires: Mapping[str, str]) -> str:
    return "route_" + canonical_sha256([source, destination, dict(requires)])[:24]


def _latest_artifact_by_kind(
    artifacts: tuple[PreparationArtifact, ...], kind: str
) -> PreparationArtifact:
    candidates = [artifact for artifact in artifacts if artifact.artifact_kind == kind]
    if not candidates:
        raise ValueError(f"Expected an accepted {kind} artifact.")
    latest_stage = max(artifact.stage.ordinal for artifact in candidates)
    candidates = [artifact for artifact in candidates if artifact.stage.ordinal == latest_stage]
    latest_version = max(artifact.version for artifact in candidates)
    candidates = [artifact for artifact in candidates if artifact.version == latest_version]
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one latest {kind} artifact.")
    return candidates[0]


def build_source_scaffold_report(
    binding: PreparationRunBinding,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    *,
    dependency_artifacts: tuple[PreparationArtifact, ...],
) -> SourceScaffoldReport:
    """Check an agent's reviewed source scaffold without enumerating ways to play it.

    Route connectivity deliberately ignores predicate satisfaction. Source/packet goal
    coverage establishes that the live DM has canonical achievement criteria and evidence;
    it does not assert that a goal has been earned or that a campaign was completed.
    """

    inventory = _unique_artifacts(
        (*dependency_artifacts, manifest_artifact, prepared_artifact, manuscript_artifact)
    )
    for artifact in inventory:
        assert_artifact_integrity(binding, artifact)
    bindings = build_review_bindings(
        binding, manifest_artifact, prepared_artifact, manuscript_artifact, inventory
    )
    manifest, prepared, manuscript = (
        _manifest(manifest_artifact), _prepared(prepared_artifact), _manuscript(manuscript_artifact)
    )
    if manifest.prepared != prepared:
        raise ValueError("Manifest and prepared artifacts must contain the same canonical scaffold.")
    if binding.extraction_sha256 and manifest.extraction_sha256 != binding.extraction_sha256:
        raise ValueError("Manifest extraction digest does not match the frozen source binding.")
    if manifest.rules_edition != binding.rules_edition:
        raise ValueError("Manifest rules edition does not match the frozen run binding.")
    evidence_ids = (
        manifest_artifact.artifact_id, prepared_artifact.artifact_id, manuscript_artifact.artifact_id
    )
    findings = _structural_findings(manifest, prepared, manuscript, evidence_ids)
    if not manifest.reviewed_by or manifest.reviewed_sha256 != manifest_digest(manifest):
        findings.append(_coherence_finding(
            "source_scaffold_manifest_unreviewed",
            "The source scaffold must bind the current explicitly reviewed manifest digest.",
            evidence_ids,
            severity="critical",
        ))
    if any(row.audience == "unreviewed" or not row.text_sha256 for row in manifest.passages):
        findings.append(_coherence_finding(
            "source_scaffold_passage_unreviewed",
            "Every retained source passage needs its reviewed audience classification and exact text digest.",
            evidence_ids,
        ))
    if not manifest.opening_scene_id or not any(
        row.audience == "player" and manifest.opening_scene_id in row.scene_ids and not row.requires
        for row in manifest.passages
    ):
        findings.append(_coherence_finding(
            "source_scaffold_opening_missing",
            "The opening scene needs unconditional reviewed player text.",
            evidence_ids,
        ))

    scene_by_id = {row.scene_id: row for row in manifest.scenes}
    reachable = {manifest.opening_scene_id} if manifest.opening_scene_id else set()
    pending = list(reachable)
    while pending:
        source = pending.pop()
        for link in scene_by_id[source].links:
            if link.scene_id not in reachable:
                reachable.add(link.scene_id)
                pending.append(link.scene_id)
    route_ids = {
        _scaffold_route_id(row.scene_id, link.scene_id, link.requires)
        for row in manifest.scenes for link in row.links
    }
    classified_routes = {
        _scaffold_route_id(row.from_scene_id, row.to_scene_id, row.traversal_requires)
        for row in prepared.routes
    } & route_ids
    passages = {row.passage_id: row for row in manifest.passages}
    packets = {row.scene_id: row for row in prepared.scene_packets}
    source_covered: dict[str, set[str]] = {"objective": set(), "milestone": set()}
    packet_covered: dict[str, set[str]] = {"objective": set(), "milestone": set()}
    for kind, definitions in (("objective", manifest.objectives), ("milestone", manifest.milestones)):
        for goal in definitions:
            evidence = [passages[pid] for pid in goal.passage_ids]
            evidence_scenes = {
                scene_id for row in evidence if row.text_sha256 and row.audience in {"player", "planner"}
                for scene_id in row.scene_ids
            }
            if (
                all(row.text_sha256 and row.audience in {"player", "planner"} for row in evidence)
                and set(goal.scene_ids) <= evidence_scenes
            ):
                source_covered[kind].add(goal.canonical_id)
            else:
                findings.append(_coherence_finding(
                    "source_scaffold_goal_evidence_missing",
                    f"Canonical {kind} {goal.canonical_id} needs reviewed source evidence covering its declared scenes.",
                    evidence_ids, affected_ids=(goal.canonical_id,),
                ))
            if all(
                scene_id in packets
                and goal.canonical_id in getattr(packets[scene_id], f"{kind}_ids")
                for scene_id in goal.scene_ids
            ):
                packet_covered[kind].add(goal.canonical_id)
            else:
                findings.append(_coherence_finding(
                    "source_scaffold_goal_packet_missing",
                    f"Canonical {kind} {goal.canonical_id} must be available in every declared scene packet.",
                    evidence_ids, affected_ids=(goal.canonical_id,),
                ))
        definitions_by_id = {row.canonical_id: row for row in definitions}
        for packet in prepared.scene_packets:
            for goal_id in getattr(packet, f"{kind}_ids"):
                goal = definitions_by_id.get(goal_id)
                if goal is not None and packet.scene_id not in goal.scene_ids:
                    findings.append(_coherence_finding(
                        "source_scaffold_goal_scene_mismatch",
                        f"Scene packet {packet.scene_id} lists {kind} {goal_id} outside its reviewed scene scope.",
                        evidence_ids, affected_ids=(packet.scene_id, goal_id),
                    ))

    endings = {row.entity_id for row in manifest.entities if row.kind == "ending"}
    source_endings = {
        ending_id for ending_id in endings if any(
            ending_id in row.entity_ids and row.text_sha256 and row.audience in {"player", "planner"}
            for row in manifest.passages
        )
    }
    for ending_id in sorted(endings - source_endings):
        findings.append(_coherence_finding(
            "source_scaffold_ending_evidence_missing",
            f"Ending identity {ending_id} needs reviewed retained source evidence.",
            evidence_ids, affected_ids=(ending_id,),
        ))
    try:
        grounded_events = _grounded_actor_events(binding, manifest, inventory)
    except (KeyError, TypeError, ValueError):
        grounded_events = {}
        findings.append(_coherence_finding(
            "source_scaffold_grounding_invalid",
            "Authored encounter actors must retain exact accepted source, scene, stat and book bindings.",
            bindings.grounding_artifact_ids,
            severity="critical",
        ))
    return SourceScaffoldReport(
        bindings=bindings,
        opening_scene_id=manifest.opening_scene_id,
        scene_ids=tuple(sorted(scene_by_id)),
        topologically_reachable_scene_ids=tuple(sorted(reachable)),
        route_ids=tuple(sorted(route_ids)),
        classified_route_ids=tuple(sorted(classified_routes)),
        objective_ids=tuple(sorted(row.canonical_id for row in manifest.objectives)),
        required_objective_ids=tuple(sorted(
            row.objective_id for row in prepared.quests if row.required_for_completion
        )),
        source_backed_objective_ids=tuple(sorted(source_covered["objective"])),
        packet_covered_objective_ids=tuple(sorted(packet_covered["objective"])),
        milestone_ids=tuple(sorted(row.canonical_id for row in manifest.milestones)),
        required_milestone_ids=tuple(sorted({
            mid for row in prepared.quests for mid in row.required_milestone_ids
        })),
        source_backed_milestone_ids=tuple(sorted(source_covered["milestone"])),
        packet_covered_milestone_ids=tuple(sorted(packet_covered["milestone"])),
        ending_entity_ids=tuple(sorted(endings)),
        source_backed_ending_entity_ids=tuple(sorted(source_endings)),
        grounded_actor_encounter_ids=tuple(sorted({
            encounter_id for rows in grounded_events.values() for encounter_id, _ in rows
        })),
        grounded_actor_ids=tuple(sorted({
            actor_id for rows in grounded_events.values() for _, actor_id in rows
        })),
        findings=tuple(_unique_findings(findings)),
    )


def build_path_exploration_artifact(
    binding: PreparationRunBinding,
    *,
    dependency_artifacts: tuple[PreparationArtifact, ...],
    created_at: str,
    task_id: str = "p10_path_exploration",
    scope_keys: tuple[str, ...] | None = None,
) -> PreparationArtifact:
    """Build P10 source consistency; artifact identity retains the saved-run contract."""

    manifest_artifact = _latest_artifact_by_kind(dependency_artifacts, "canonical_manifest")
    prepared_artifact = _latest_artifact_by_kind(dependency_artifacts, "prepared_campaign")
    manuscript_artifact = _latest_artifact_by_kind(dependency_artifacts, "retained_manuscript")
    report = build_source_scaffold_report(
        binding, manifest_artifact, prepared_artifact, manuscript_artifact,
        dependency_artifacts=dependency_artifacts,
    )
    return build_artifact(
        binding,
        stage=PreparationStage.P10,
        task_id=task_id,
        artifact_kind="path_exploration_report",
        payload={
            "report_mode": "source_scaffold",
            "scaffold_report": report.model_dump(mode="json"),
        },
        dependency_artifacts=_unique_artifacts(dependency_artifacts),
        created_at=created_at,
        scope_keys=scope_keys if scope_keys is not None else report.scene_ids,
    )


def make_path_exploration_validator() -> Callable[..., ValidationResult]:
    """Recompute P10 source consistency using the existing saved-run validator ID."""

    def validate(
        binding: PreparationRunBinding,
        task: PreparationTask,
        proposed_artifacts: tuple[PreparationArtifact, ...],
        dependencies: tuple[PreparationArtifact, ...],
    ) -> ValidationResult:
        try:
            report_artifact = _single_proposed(proposed_artifacts, "path_exploration_report")
            manifest_artifact = _latest_artifact_by_kind(dependencies, "canonical_manifest")
            prepared_artifact = _latest_artifact_by_kind(dependencies, "prepared_campaign")
            manuscript_artifact = _latest_artifact_by_kind(dependencies, "retained_manuscript")
            for artifact in _unique_artifacts((*dependencies, *proposed_artifacts)):
                assert_artifact_integrity(binding, artifact)
            if report_artifact.dependency_digests != {
                a.artifact_id: a.content_sha256 for a in dependencies
            }:
                raise ValueError("P10 report does not bind exact accepted task dependencies.")
            if (
                report_artifact.payload.get("report_mode") != "source_scaffold"
                or set(report_artifact.payload) != {"report_mode", "scaffold_report"}
            ):
                raise ValueError("P10 requires the source-scaffold report schema.")
            actual = SourceScaffoldReport.model_validate(
                report_artifact.payload["scaffold_report"], strict=False
            )
            expected = build_source_scaffold_report(
                binding, manifest_artifact, prepared_artifact, manuscript_artifact,
                dependency_artifacts=dependencies,
            )
            if actual != expected:
                raise ValueError("Source-scaffold report differs from exact deterministic recomputation.")
            receipt = build_validation_receipt(
                "p10_path_exploration",
                artifacts=proposed_artifacts,
                dependencies=dependencies,
                passed=True,
                details={
                    "report_mode": "source_scaffold",
                    "scaffold_report_sha256": report_artifact.content_sha256,
                },
                created_at=_utc_now(),
            )
            return ValidationResult(
                passed=True,
                receipts=(receipt,),
                findings=tuple(_preparation_finding(row) for row in actual.findings),
            )
        except Exception:
            failure = PreparationFinding(
                finding_id="p10-path-validator-failed",
                code="path_exploration_invalid",
                severity="critical",
                message="The P10 source-scaffold report did not match its exact reviewed subjects and source bindings.",
                evidence_artifact_ids=tuple(a.artifact_id for a in proposed_artifacts),
            )
            return ValidationResult(
                passed=False, findings=(failure,), block_code="path_exploration_invalid"
            )

    validate.validator_id = "p10_path_exploration"  # type: ignore[attr-defined]
    return validate


def build_adventure_review(
    binding: PreparationRunBinding,
    *,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    source_artifacts: tuple[PreparationArtifact, ...],
) -> AdventureReviewReport:
    """Build a deterministic, whole-adventure report over exact retained artifacts."""

    dependencies = tuple(
        {
            a.artifact_id: a
            for a in (*source_artifacts, manifest_artifact, prepared_artifact, manuscript_artifact)
        }.values()
    )
    bindings = build_review_bindings(
        binding, manifest_artifact, prepared_artifact, manuscript_artifact, dependencies
    )
    manifest = _manifest(manifest_artifact)
    prepared = _prepared(prepared_artifact)
    manuscript = _manuscript(manuscript_artifact)
    evidence_ids = tuple(sorted(bindings.artifact_digests))
    findings = _structural_findings(manifest, prepared, manuscript, evidence_ids)
    return AdventureReviewReport(
        bindings=bindings,
        chapter_ids=tuple(chapter.chapter_id for chapter in manuscript.chapters),
        scene_ids=tuple(scene.scene_id for scene in manifest.scenes),
        findings=tuple(_unique_findings(findings)),
        path_report=None,
    )


def build_deterministic_review_artifact(
    binding: PreparationRunBinding,
    *,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    dependency_artifacts: tuple[PreparationArtifact, ...],
    created_at: str,
    task_id: str = "p09_deterministic_review",
    scope_keys: tuple[str, ...] | None = None,
) -> PreparationArtifact:
    """P09 offline StageAdapter helper. No provider call is made."""

    dependencies = _unique_artifacts(dependency_artifacts)
    subject_ids = {
        manifest_artifact.artifact_id,
        prepared_artifact.artifact_id,
        manuscript_artifact.artifact_id,
    }
    inventory = _unique_artifacts(
        (*dependencies, manifest_artifact, prepared_artifact, manuscript_artifact)
    )
    source_artifacts = tuple(
        artifact for artifact in inventory if artifact.artifact_id not in subject_ids
    )
    report = build_adventure_review(
        binding,
        manifest_artifact=manifest_artifact,
        prepared_artifact=prepared_artifact,
        manuscript_artifact=manuscript_artifact,
        source_artifacts=source_artifacts,
    )
    payload = {"review_report": report.model_dump(mode="json")}
    return build_artifact(
        binding,
        stage=PreparationStage.P09,
        task_id=task_id,
        artifact_kind="deterministic_review_report",
        payload=payload,
        dependency_artifacts=dependencies,
        created_at=created_at,
        scope_keys=scope_keys if scope_keys is not None else tuple(report.chapter_ids),
    )


def make_deterministic_review_validator() -> Callable[..., ValidationResult]:
    """Create the required supervisor validator for a deterministic P09 review artifact."""

    def validate(
        binding: PreparationRunBinding,
        task: PreparationTask,
        proposed_artifacts: tuple[PreparationArtifact, ...],
        dependencies: tuple[PreparationArtifact, ...],
    ) -> ValidationResult:
        try:
            report_artifact = _single_proposed(proposed_artifacts, "deterministic_review_report")
            manifest_artifact, prepared_artifact, manuscript_artifact, inventory = _review_subjects(
                proposed_artifacts, dependencies
            )
            subject_ids = {
                manifest_artifact.artifact_id,
                prepared_artifact.artifact_id,
                manuscript_artifact.artifact_id,
            }
            source_artifacts = tuple(
                artifact for artifact in inventory if artifact.artifact_id not in subject_ids
            )
            expected = build_adventure_review(
                binding,
                manifest_artifact=manifest_artifact,
                prepared_artifact=prepared_artifact,
                manuscript_artifact=manuscript_artifact,
                source_artifacts=source_artifacts,
            )
            actual = AdventureReviewReport.model_validate(
                report_artifact.payload["review_report"], strict=False
            )
            if actual != expected:
                raise ValueError("Deterministic report differs from the code recomputation.")
            if report_artifact.dependency_digests != {
                a.artifact_id: a.content_sha256 for a in dependencies
            }:
                raise ValueError(
                    "Deterministic report does not bind the exact accepted task dependencies."
                )
            findings = tuple(_preparation_finding(row) for row in actual.findings)
            receipt = build_validation_receipt(
                "p09_deterministic_review",
                artifacts=proposed_artifacts,
                dependencies=dependencies,
                passed=True,
                details={"review_report_sha256": report_artifact.content_sha256},
                created_at=_utc_now(),
            )
            return ValidationResult(passed=True, receipts=(receipt,), findings=findings)
        except Exception:
            failure = PreparationFinding(
                finding_id="p09-deterministic-validator-failed",
                code="deterministic_review_invalid",
                severity="critical",
                message="The deterministic campaign review did not match its exact inputs.",
                evidence_artifact_ids=tuple(a.artifact_id for a in proposed_artifacts),
            )
            return ValidationResult(
                passed=False, findings=(failure,), block_code="deterministic_review_invalid"
            )

    validate.validator_id = "p09_deterministic_review"  # type: ignore[attr-defined]
    return validate


def _single_proposed(artifacts: tuple[PreparationArtifact, ...], kind: str) -> PreparationArtifact:
    matches = [artifact for artifact in artifacts if artifact.artifact_kind == kind]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one proposed {kind} artifact.")
    return matches[0]


def _preparation_finding(row: CoherenceFinding | CriticFinding) -> PreparationFinding:
    evidence_ids = (
        row.evidence_artifact_ids
        if isinstance(row, CoherenceFinding)
        else tuple(dict.fromkeys(item.artifact_id for item in row.evidence))
    )
    return PreparationFinding(
        finding_id=row.finding_id,
        code=row.code,
        severity=row.severity,
        message=row.message,
        evidence_artifact_ids=evidence_ids,
        affected_ids=row.affected_ids,
        status="open",
    )


def validate_critic_report(
    report: CriticReport | dict[str, Any],
    *,
    expected_bindings: ReviewBindings,
    expected_chapters: RetainedManuscript,
    artifacts: tuple[PreparationArtifact, ...],
) -> tuple[CriticFinding, ...]:
    """Validate full-coverage semantic evidence against exact retained artifact bytes."""

    candidate = (
        report
        if isinstance(report, CriticReport)
        else CriticReport.model_validate(report, strict=False)
    )
    if candidate.bindings != expected_bindings:
        raise ValueError(
            "Critic result is stale or belongs to a different source, hero or artifact inventory."
        )
    expected_chapter_digests = {
        chapter.chapter_id: _digest(chapter.text) for chapter in expected_chapters.chapters
    }
    if candidate.reviewed_chapter_text_sha256 != expected_chapter_digests:
        raise ValueError("Critic did not cover every exact retained chapter in full.")
    by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    for finding in candidate.findings:
        for evidence in finding.evidence:
            artifact = by_id.get(evidence.artifact_id)
            if artifact is None or artifact.content_sha256 != evidence.artifact_sha256:
                raise ValueError(
                    "Critic evidence points outside the exact accepted review inventory."
                )
            value = resolve_json_pointer(artifact.payload, evidence.json_pointer)
            if not isinstance(value, str) or evidence.excerpt not in value:
                raise ValueError(
                    "Critic evidence excerpt is not present at its cited exact source pointer."
                )
            if evidence.excerpt_sha256 != _digest(evidence.excerpt):
                raise ValueError("Critic evidence excerpt digest is invalid.")
    return candidate.findings


def resolve_json_pointer(value: Any, pointer: str) -> Any:
    """Resolve an RFC 6901 JSON pointer into a JSON-compatible artifact payload."""

    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or begin with '/'.")
    current = value
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError("JSON pointer contains an invalid escape sequence.")
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("JSON pointer array index is invalid.")
            current = current[int(token)]
        else:
            raise ValueError("JSON pointer traverses a scalar value.")
    return current


def make_independent_critic_validator() -> Callable[..., ValidationResult]:
    """Create the required validator for root-owned semantic critic output.

    The executor must pass the entire retained-manuscript artifact to the effective campaign
    provider seam. The validator checks full chapter coverage and exact evidence; it does
    not set report findings to resolved or approve publication.
    """

    def validate(
        binding: PreparationRunBinding,
        task: PreparationTask,
        proposed_artifacts: tuple[PreparationArtifact, ...],
        dependencies: tuple[PreparationArtifact, ...],
    ) -> ValidationResult:
        try:
            report_artifact = _single_proposed(proposed_artifacts, "semantic_critic_report")
            manifest_artifact, prepared_artifact, manuscript_artifact, inventory = _review_subjects(
                proposed_artifacts, dependencies
            )
            bindings = build_review_bindings(
                binding, manifest_artifact, prepared_artifact, manuscript_artifact, inventory
            )
            manuscript = _manuscript(manuscript_artifact)
            findings = validate_critic_report(
                report_artifact.payload["critic_report"],
                expected_bindings=bindings,
                expected_chapters=manuscript,
                artifacts=inventory,
            )
            if report_artifact.dependency_digests != {
                a.artifact_id: a.content_sha256 for a in dependencies
            }:
                raise ValueError("Critic report does not bind the exact input inventory.")
            receipt = build_validation_receipt(
                "p09_independent_critic",
                artifacts=proposed_artifacts,
                dependencies=dependencies,
                passed=True,
                details={"critic_report_sha256": report_artifact.content_sha256},
                created_at=_utc_now(),
            )
            return ValidationResult(
                passed=True,
                receipts=(receipt,),
                findings=tuple(_preparation_finding(row) for row in findings),
            )
        except Exception:
            failure = PreparationFinding(
                finding_id="p09-independent-critic-validator-failed",
                code="independent_critic_invalid",
                severity="critical",
                message="The independent campaign critique did not match its exact inputs or evidence.",
                evidence_artifact_ids=tuple(a.artifact_id for a in proposed_artifacts),
            )
            return ValidationResult(
                passed=False, findings=(failure,), block_code="independent_critic_invalid"
            )

    validate.validator_id = "p09_independent_critic"  # type: ignore[attr-defined]
    return validate


def build_critic_artifact(
    binding: PreparationRunBinding,
    report: CriticReport,
    *,
    dependency_artifacts: tuple[PreparationArtifact, ...],
    created_at: str,
    task_id: str = "p09_independent_critic",
    scope_keys: tuple[str, ...] | None = None,
) -> PreparationArtifact:
    """Build a version-bound P09 artifact after the root-owned provider call."""

    return build_artifact(
        binding,
        stage=PreparationStage.P09,
        task_id=task_id,
        artifact_kind="semantic_critic_report",
        payload={"critic_report": report.model_dump(mode="json")},
        dependency_artifacts=dependency_artifacts,
        created_at=created_at,
        scope_keys=scope_keys
        if scope_keys is not None
        else tuple(sorted(report.reviewed_chapter_text_sha256)),
    )


def validate_repair_envelope(
    envelope: RepairEnvelope | dict[str, Any],
    *,
    binding: PreparationRunBinding,
    originals: tuple[PreparationArtifact, ...],
    replacements: tuple[PreparationArtifact, ...],
    expected_finding_ids: tuple[str, ...],
    preserved_revealed_fact_digests: Mapping[str, str],
) -> RepairEnvelope:
    """Fail closed unless a bounded P11 replacement preserves bindings and earned facts.

    Physical PDF/source spans stay immutable because all retained source artifacts must keep
    their exact prior digests. Revealed facts are supplied by the caller's session snapshot;
    a repair cannot rewrite or omit their sealed identities. New artifacts still require the
    normal deterministic and critic validators before their findings can be resolved.
    """

    candidate = (
        envelope
        if isinstance(envelope, RepairEnvelope)
        else RepairEnvelope.model_validate(envelope, strict=False)
    )
    binding.assert_integrity()
    if (
        candidate.run_binding_sha256 != binding.binding_sha256
        or candidate.source_binding_sha256 != binding.source_binding_sha256
    ):
        raise ValueError("Repair envelope belongs to a different frozen source or run binding.")
    if len(set(candidate.finding_ids)) != len(candidate.finding_ids) or len(
        set(candidate.affected_artifact_ids)
    ) != len(candidate.affected_artifact_ids):
        raise ValueError("Repair targets cannot repeat finding or artifact identities.")
    if set(candidate.finding_ids) != set(expected_finding_ids):
        raise ValueError(
            "Repair may address only the exact findings selected for this bounded round."
        )
    original_by_id = {artifact.artifact_id: artifact for artifact in originals}
    replacement_by_id = {artifact.artifact_id: artifact for artifact in replacements}
    if len(original_by_id) != len(originals) or len(replacement_by_id) != len(replacements):
        raise ValueError("Repair inventories cannot repeat artifact identities.")
    if set(original_by_id) & set(replacement_by_id):
        raise ValueError("A replacement must receive a new artifact identity.")
    if set(candidate.affected_artifact_ids) - set(original_by_id):
        raise ValueError("Repair scope references an artifact outside the accepted run inventory.")
    if set(candidate.preserved_revealed_fact_digests) != set(
        preserved_revealed_fact_digests
    ) or dict(candidate.preserved_revealed_fact_digests) != dict(preserved_revealed_fact_digests):
        raise ValueError("Repair changed the exact sealed revealed-fact inventory.")
    source_inventory = {
        artifact.artifact_id: artifact.content_sha256
        for artifact in originals
        if artifact.artifact_kind in {"retained_source", "grounding_report"}
    }
    if dict(candidate.preserved_source_artifact_digests) != source_inventory:
        raise ValueError("Repair changed or omitted an exact source or grounding artifact digest.")
    if len({row.replaces_artifact_id for row in candidate.replacements}) != len(
        candidate.replacements
    ):
        raise ValueError("A repair may replace each artifact only once per round.")
    if set(row.replaces_artifact_id for row in candidate.replacements) != set(
        candidate.affected_artifact_ids
    ):
        raise ValueError("Every affected artifact needs exactly one newer replacement.")
    if {row.replacement_artifact_id for row in candidate.replacements} != set(replacement_by_id):
        raise ValueError(
            "Repair replacement inventory contains an unreferenced or missing artifact."
        )
    for row in candidate.replacements:
        original = original_by_id[row.replaces_artifact_id]
        if original.artifact_kind in {"retained_source", "grounding_report"}:
            raise ValueError("Retained source and grounding artifacts are immutable during repair.")
        replacement = replacement_by_id.get(row.replacement_artifact_id)
        if (
            replacement is None
            or replacement.version != row.replacement_version
            or original.version != row.original_version
        ):
            raise ValueError("Repair replacement version does not match its artifact inventory.")
        if replacement.artifact_kind != original.artifact_kind:
            raise ValueError("Repair cannot change an artifact's canonical kind.")
        if replacement.stage != PreparationStage.P11:
            raise ValueError("Repair replacements must be created by the P11 repair stage.")
        if (
            replacement.run_binding_sha256 != binding.binding_sha256
            or replacement.source_binding_sha256 != binding.source_binding_sha256
            or replacement.hero_party_sha256 != binding.hero_party_sha256
            or replacement.profile_snapshot_sha256 != binding.profile_snapshot_sha256
        ):
            raise ValueError(
                "Repair replacement changed a frozen run, source, hero or profile binding."
            )
        assert_artifact_integrity(binding, replacement)
    return candidate


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
