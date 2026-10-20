"""Whole-adventure continuity, critic, path and repair contracts.

This module consumes the existing reviewed manifest and its one ``PreparedCampaign``.
It does not create another narrative graph, and it never treats a model's verdict as a
validation receipt. Reports are private and bound to the exact preparation artifacts.
"""

from __future__ import annotations

import hashlib
import re
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from heapq import heapify, heappop, heappush
from itertools import count
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.manifests import (
    AdventureManifest,
    ResolutionPolicy,
    manifest_digest,
    policy_fact_requirements_met,
    select_scene_context,
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
    CommittedPreparedFact,
    PreparedCampaign,
    PreparedFinding,
    _policy_effects,
    prepared_digest,
    prepared_policy_available,
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
    advance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    climax_ids: tuple[str, ...] = Field(default=(), max_length=32)
    passage_ids: tuple[str, ...] = ()
    continuity_assertions: tuple[ContinuityAssertion, ...] = Field(default=(), max_length=500)
    events: tuple[ManuscriptEvent, ...] = Field(default=(), max_length=500)
    text: str = Field(min_length=1, max_length=2_000_000)


class RetainedManuscript(CoherenceRecord):
    """Canonical full manuscript inventory used by the P09 critic and P10 traversal."""

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


class PathBudget(CoherenceRecord):
    """Bound state expansions, including shorter-path reopenings, and trace depth."""

    max_states: int = Field(default=512, ge=1)
    max_depth: int = Field(default=64, ge=1)


class PathExample(CoherenceRecord):
    scene_ids: tuple[str, ...]
    branch_ids: tuple[str, ...]
    outcome_ids: tuple[str, ...]
    policy_ids: tuple[str, ...] = ()
    encounter_ids: tuple[str, ...] = ()
    ending_entity_ids: tuple[str, ...] = ()
    completed_objective_ids: tuple[str, ...] = ()
    completed_milestone_ids: tuple[str, ...] = ()
    gated_route_ids: tuple[str, ...] = ()


class PathExplorationReport(CoherenceRecord):
    manifest_artifact_id: str
    prepared_artifact_id: str
    manuscript_artifact_id: str
    budget: PathBudget
    explored_states: int = Field(ge=0, description="State expansions, including shorter-path reopenings.")
    expected_outcome_ids: tuple[str, ...]
    reached_outcome_ids: tuple[str, ...]
    unreachable_outcome_ids: tuple[str, ...]
    unverified_outcome_ids: tuple[str, ...] = ()
    required_objective_ids: tuple[str, ...] = ()
    required_milestone_ids: tuple[str, ...] = ()
    expected_ending_ids: tuple[str, ...] = ()
    reached_ending_ids: tuple[str, ...] = ()
    ending_witnesses: tuple[PathExample, ...] = ()
    completion_witness: PathExample | None = None
    completion_unverified: bool = False
    expected_automatic_policy_ids: tuple[str, ...] = ()
    reached_automatic_policy_ids: tuple[str, ...] = ()
    unreachable_automatic_policy_ids: tuple[str, ...] = ()
    unverified_automatic_policy_ids: tuple[str, ...] = ()
    expected_gated_route_ids: tuple[str, ...] = ()
    reached_gated_route_ids: tuple[str, ...] = ()
    unreachable_gated_route_ids: tuple[str, ...] = ()
    unverified_gated_route_ids: tuple[str, ...] = ()
    gated_route_witnesses: dict[str, PathExample] = Field(default_factory=dict)
    exhausted: bool
    examples: tuple[PathExample, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_outcome_inventory(self) -> PathExplorationReport:
        groups = (
            self.expected_outcome_ids,
            self.reached_outcome_ids,
            self.unreachable_outcome_ids,
            self.unverified_outcome_ids,
        )
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("Path outcome inventories must not repeat IDs.")
        expected = set(self.expected_outcome_ids)
        reached = set(self.reached_outcome_ids)
        unreachable = set(self.unreachable_outcome_ids)
        unverified = set(self.unverified_outcome_ids)
        if reached - expected or unreachable - expected or unverified - expected:
            raise ValueError("Path outcome inventory contains an unknown outcome ID.")
        if reached & unreachable or reached & unverified or unreachable & unverified:
            raise ValueError("Reached, unreachable and unverified path outcomes must be disjoint.")
        if reached | unreachable | unverified != expected:
            raise ValueError("Path report must account for every expected outcome.")
        if unverified and not self.exhausted:
            raise ValueError("Only an exhausted path search may contain unverified outcomes.")
        if self.explored_states > self.budget.max_states:
            raise ValueError("Path search exceeded its declared state budget.")
        if set(self.reached_ending_ids) - set(self.expected_ending_ids):
            raise ValueError("Path report names an unknown reached ending.")
        witnessed_endings = {eid for trace in self.ending_witnesses for eid in trace.ending_entity_ids}
        if witnessed_endings != set(self.reached_ending_ids):
            raise ValueError("Every reached ending needs its own retained path witness.")
        if self.completion_unverified and (not self.exhausted or self.completion_witness is not None):
            raise ValueError("Only an exhausted search without a completion witness is unverified.")
        if self.completion_witness is not None and not self.completion_witness.ending_entity_ids:
            raise ValueError("A completion witness must reach a terminal ending.")
        if self.completion_witness is not None and (
            not set(self.required_objective_ids) <= set(self.completion_witness.completed_objective_ids)
            or not set(self.required_milestone_ids) <= set(self.completion_witness.completed_milestone_ids)
            or not set(self.completion_witness.ending_entity_ids) <= set(self.reached_ending_ids)
        ):
            raise ValueError("Completion witness must include every required achievement and a reached ending.")
        for expected_ids, reached_ids, unreachable_ids, unverified_ids in (
            (self.expected_automatic_policy_ids, self.reached_automatic_policy_ids,
             self.unreachable_automatic_policy_ids, self.unverified_automatic_policy_ids),
            (self.expected_gated_route_ids, self.reached_gated_route_ids,
             self.unreachable_gated_route_ids, self.unverified_gated_route_ids),
        ):
            classified = reached_ids + unreachable_ids + unverified_ids
            if (
                len(expected_ids) != len(set(expected_ids))
                or len(classified) != len(set(classified))
                or set(classified) != set(expected_ids)
                or (unverified_ids and not self.exhausted)
            ):
                raise ValueError("Path policy/route inventories need one classification per expected ID.")
        if set(self.gated_route_witnesses) != set(self.reached_gated_route_ids) or any(
            route_id not in trace.gated_route_ids
            for route_id, trace in self.gated_route_witnesses.items()
        ):
            raise ValueError("Every reached gated route needs an explicit traversal witness.")
        return self

    @property
    def exhaustive(self) -> bool:
        """Whether every authored outcome has a reachable or unreachable classification."""
        return not self.exhausted and not self.unverified_outcome_ids


class PreparedDependencyClosureReport(CoherenceRecord):
    """Optimistic possibilities are diagnostic hints; only impossible IDs are proofs."""

    manifest_artifact_id: str
    prepared_artifact_id: str
    iterations: int = Field(ge=1)
    possible_scene_ids: tuple[str, ...]
    possible_policy_ids: tuple[str, ...]
    possible_outcome_ids: tuple[str, ...]
    possible_gated_route_ids: tuple[str, ...]
    possible_fact_states: tuple[tuple[str, str, str], ...]
    possible_flag_values: dict[str, tuple[str, ...]]
    impossible_policy_ids: tuple[str, ...]
    impossible_automatic_policy_ids: tuple[str, ...]
    impossible_outcome_ids: tuple[str, ...]
    impossible_gated_route_ids: tuple[str, ...]
    impossible_required_objective_ids: tuple[str, ...]
    impossible_required_milestone_ids: tuple[str, ...]
    impossible_ending_ids: tuple[str, ...]

    @property
    def definitely_impossible(self) -> bool:
        return any((self.impossible_policy_ids, self.impossible_outcome_ids,
                    self.impossible_gated_route_ids, self.impossible_required_objective_ids,
                    self.impossible_required_milestone_ids, self.impossible_ending_ids))


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


class AdventureReviewReport(CoherenceRecord):
    schema_version: Literal[1] = 1
    bindings: ReviewBindings
    chapter_ids: tuple[str, ...]
    scene_ids: tuple[str, ...]
    findings: tuple[CoherenceFinding, ...] = Field(max_length=4000)
    path_report: PathExplorationReport | None = None


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


DEFAULT_PATH_BUDGET = PathBudget()
PathState = tuple[
    str,
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[tuple[str, str, str, tuple[str, ...]], ...],
    bool,
]
PathTrace = tuple[PathState, PathExample, int]


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
    required_advance_ids = {mid for quest in prepared.quests for mid in quest.required_milestone_ids}
    for chapter in manuscript.chapters:
        if not set(chapter.advance_ids) & required_advance_ids:
            findings.append(
                _coherence_finding(
                    "chapter_missing_required_advance",
                    f"Chapter {chapter.chapter_id} needs a declared required canonical milestone advance.",
                    evidence_ids, affected_ids=(chapter.chapter_id,),
                )
            )
        for advance in chapter.advance_ids:
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


def _inventory_names(snapshot: Mapping[str, Any]) -> set[str]:
    def collect(value: Any, *, in_inventory: bool = False) -> set[str]:
        names: set[str] = set()
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized_key = key.casefold()
                if (
                    in_inventory
                    and normalized_key in {"name", "item_name"}
                    and isinstance(child, str)
                ):
                    names.add(child)
                elif normalized_key in {"inventory", "items"}:
                    names.update(collect(child, in_inventory=True))
                else:
                    names.update(collect(child, in_inventory=in_inventory))
        elif isinstance(value, (list, tuple)):
            for child in value:
                if in_inventory and isinstance(child, str):
                    names.add(child)
                else:
                    names.update(collect(child, in_inventory=in_inventory))
        elif in_inventory and isinstance(value, str):
            names.add(value)
        return names

    return {name.casefold().strip() for name in collect(snapshot) if name.strip()}


_STOPWORDS = {"of", "the", "a", "an", "my", "some", "this", "that", "your", "his", "her"}


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token and token not in _STOPWORDS
    }


def _has_required_item(inventory: set[str], required: str) -> bool:
    if required.casefold().strip() in inventory:
        return True
    wanted = _tokens(required)
    if not wanted:
        return False
    for item in inventory:
        held = _tokens(item)
        if held and (wanted <= held or held <= wanted):
            return True
        if held and (
            {token.rstrip("s") for token in wanted} <= {token.rstrip("s") for token in held}
        ):
            return True
    return False


def _grounded_actor_events(
    binding: PreparationRunBinding,
    manifest: AdventureManifest,
    artifacts: tuple[PreparationArtifact, ...],
) -> dict[str, list[tuple[str, str]]]:
    """Allow hypothetical engine outcomes only for explicitly grounded encounter actors."""
    from edgar_core.grounding import (
        EncounterGroundingReceipt,
        EncounterPlan,
        HeroPartySnapshot,
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
            if artifact.artifact_kind == "grounding_report" and any(
                payload.get(key) != artifact.payload.get(key)
                for key in ("book_snapshot_sha256", "hero_party_sha256")
            ):
                raise ValueError("Aggregated encounter evidence changed its exact hero/book binding.")
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
    party = HeroPartySnapshot.model_validate(binding.hero_party_snapshot)
    book_digests = {
        VerifiedBookSnapshot.model_validate(row.payload["snapshot"]).snapshot_sha256
        for row in artifacts if row.artifact_kind == "book_dossier"
    }
    actors = {row.entity_id: row for row in manifest.entities if row.kind == "actor"}
    events: dict[str, list[tuple[str, str]]] = {}
    for _, payload in latest.values():
        if (
            payload.get("hero_party_sha256") != party.party_sha256
            or (book_digests and payload.get("book_snapshot_sha256") not in book_digests)
        ):
            raise ValueError("Encounter evidence does not match the frozen hero and book inputs.")
        plans = {
            row["encounter_id"]: EncounterPlan.model_validate(row)
            for row in payload["plan"]["encounters"]
        }
        for raw in payload.get("encounters", ()):
            receipt = EncounterGroundingReceipt.model_validate(raw)
            plan = plans.get(receipt.encounter_id)
            if (
                plan is None or not receipt.valid
                or receipt.plan_sha256 != plan.plan_sha256
                or receipt.scene_id != plan.scene_id
                or receipt.hero_party_sha256 != party.party_sha256
                or receipt.book_snapshot_sha256 != payload.get("book_snapshot_sha256")
                or receipt.rules_edition != manifest.rules_edition
                or set(receipt.actor_ids) != set(plan.actor_ids)
                or len(receipt.actor_ids) != len(set(receipt.actor_ids))
                or len(plan.actor_ids) != len(set(plan.actor_ids))
            ):
                raise ValueError("Actor event needs a valid exact accepted encounter receipt.")
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


def _gated_route_id(source: str, destination: str, requires: Mapping[str, str]) -> str:
    return "route_" + canonical_sha256([source, destination, dict(requires)])[:24]


def optimistic_prepared_dependency_closure(
    binding: PreparationRunBinding,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    *,
    initial_world_flags: Mapping[str, str] | None = None,
    grounding_artifacts: tuple[PreparationArtifact, ...] = (),
) -> PreparedDependencyClosureReport:
    """Reject missing/circular positive dependencies without enumerating exact states.

    Flags and all alternative fact statuses coexist forever. Different paths and check
    results may combine; terminal resolution, custody, consumption and item requirements
    are ignored. The resulting least fixed point contains every exact runtime trace.
    Absence from it proves impossibility, while membership never supplies a path witness.
    """
    assert_artifact_integrity(binding, manifest_artifact)
    assert_artifact_integrity(binding, prepared_artifact)
    manifest, prepared = _manifest(manifest_artifact), _prepared(prepared_artifact)
    if prepared.source_mode != binding.source_mode:
        raise ValueError("Prepared campaign must use the frozen source mode.")
    if binding.source_sha256 and manifest.source_sha256 != binding.source_sha256:
        raise ValueError("Manifest source digest does not match the frozen run binding.")
    if not manifest.opening_scene_id:
        raise ValueError("Bound manifest has no reviewed opening scene.")
    initial_flags = dict(initial_world_flags or {})
    if any(not isinstance(key, str) or not key or not isinstance(value, str)
           for key, value in initial_flags.items()):
        raise ValueError("Initial flags must map nonempty WorldFlag IDs to string values.")
    flags = set(initial_flags.items())
    facts: set[tuple[str, str, str]] = set()
    earned_effects: set[tuple[str, str, str]] = set()
    scenes = {manifest.opening_scene_id}
    policies = {row.policy_id: row for row in manifest.resolution_policies}
    entities = {row.entity_id: row for row in manifest.entities}
    passages = {row.passage_id: row for row in manifest.passages}
    scene_rows = {row.scene_id: row for row in manifest.scenes}
    quests = {row.quest_id: row for row in prepared.quests}
    outcomes = {row.outcome_id: row for row in prepared.outcomes}
    branches_by_policy: dict[str, list[Any]] = {}
    for branch in prepared.branches:
        for policy_id in branch.policy_ids:
            branches_by_policy.setdefault(policy_id, []).append(branch)
    definitions = {
        "objective": {row.canonical_id: row for row in manifest.objectives},
        "milestone": {row.canonical_id: row for row in manifest.milestones},
        "clock": {row.canonical_id: row for row in manifest.clocks},
    }
    objects = {row.canonical_id: row for row in manifest.objects}
    combat_events = _grounded_actor_events(binding, manifest, grounding_artifacts)
    executions: set[tuple[str, str, str]] = set()
    possible_outcomes: set[str] = set()
    possible_routes: set[str] = set()

    def predicates_possible(requirements):
        return all((key, value) in flags for key, value in requirements.items())

    def readable(scene_id):
        return {row.passage_id for row in manifest.passages
                if row.audience in {"player", "planner"}
                and (scene_id in row.scene_ids or row.passage_id in scene_rows[scene_id].references)
                and predicates_possible(row.requires)}

    def prepared_possible(policy_id, result, scene_id):
        bound = branches_by_policy.get(policy_id, ())
        if not bound:
            return True
        return any(
            branch.scene_id == scene_id and (quest := quests.get(branch.quest_id)) is not None
            and predicates_possible(quest.prerequisites) and predicates_possible(branch.requires)
            and any(outcome is not None and outcome.branch_id == branch.branch_id
                    and predicates_possible(outcome.requires)
                    and any(row.policy_id == policy_id and row.result == result
                            for row in outcome.policy_selections)
                    for outcome in (outcomes.get(oid) for oid in branch.outcome_ids))
            for branch in bound
        )

    def policy_possible(policy, result, scene_id):
        if (not predicates_possible(policy.requires)
                or not all(any((row.kind, row.target_id, status) in facts for status in row.statuses)
                           for row in policy.requires_facts)
                or not prepared_possible(policy.policy_id, result, scene_id)):
            return False
        selected = readable(scene_id)
        if not set(policy.passage_ids) <= selected:
            return False
        allowed_destinations = {scene_id} | {link.scene_id for link in scene_rows[scene_id].links
                                             if predicates_possible(link.requires)}
        for effect in _policy_effects(policy, result):
            # Runtime omits an identical already committed effect before validating the
            # remaining action. Joining histories permits that idempotent omission here.
            if (effect.kind, effect.target_id, effect.status) in earned_effects:
                continue
            if effect.kind == "discover":
                entity = entities[effect.target_id]
                if not any(pid in selected and passages[pid].audience == "player"
                           and effect.target_id in passages[pid].entity_ids for pid in policy.passage_ids):
                    return False
                evidence = {
                    pid for pid in effect.passage_ids
                    if passages[pid].audience == "player" and effect.target_id in passages[pid].entity_ids
                    and (pid in selected or (
                        scene_id in passages[pid].scene_ids and entity.discovery_flag
                        and passages[pid].requires.get(entity.discovery_flag) == "discovered"
                        and all(key == entity.discovery_flag or (key, value) in flags
                                for key, value in passages[pid].requires.items())
                    ))
                }
                if evidence != set(effect.passage_ids):
                    return False
            else:
                evidence = {pid for pid in effect.passage_ids
                            if pid in selected and passages[pid].audience == "player"}
            if not evidence:
                return False
            definition = objects.get(effect.target_id) if effect.kind in {
                "acquire", "consume", "transfer", "use", "environment"
            } else definitions.get(effect.kind, {}).get(effect.target_id)
            if definition is not None and (
                scene_id not in definition.scene_ids or not predicates_possible(definition.requires)
                or not evidence.intersection(definition.passage_ids)
            ):
                return False
            if effect.kind == "use" and effect.destination:
                destination = objects.get(effect.destination)
                if destination is not None and (
                    scene_id not in destination.scene_ids or not predicates_possible(destination.requires)
                ):
                    return False
            if effect.kind == "move":
                if effect.target_id not in allowed_destinations:
                    return False
        return True

    def add_effect(effect):
        earned_effects.add((effect.kind, effect.target_id, effect.status))
        if effect.kind in {"discover", "encounter", "relationship"}:
            facts.add(("entity", effect.target_id, effect.status))
        elif effect.kind in {"objective", "milestone"}:
            facts.add((effect.kind, effect.target_id, effect.status))
        if effect.kind == "discover" and entities[effect.target_id].discovery_flag:
            flags.add((entities[effect.target_id].discovery_flag, "discovered"))
        elif effect.kind == "move":
            scenes.add(effect.target_id)

    for seed in manifest.opening_journal:
        earned_effects.add((seed.kind, seed.target_id, seed.status))
        if seed.kind in {"discover", "encounter"}:
            facts.add(("entity", seed.target_id, seed.status))
        elif seed.kind in {"objective", "milestone"}:
            facts.add((seed.kind, seed.target_id, seed.status))
        if seed.kind == "discover" and entities[seed.target_id].discovery_flag:
            flags.add((entities[seed.target_id].discovery_flag, seed.status))

    def inventory_size():
        return (len(flags), len(facts), len(earned_effects), len(scenes), len(executions),
                len(possible_outcomes), len(possible_routes))

    iterations = 0
    while True:
        iterations += 1
        previous = inventory_size()
        for scene_id in sorted(scenes):
            for link in scene_rows[scene_id].links:
                if predicates_possible(link.requires):
                    scenes.add(link.scene_id)
                    if link.requires:
                        possible_routes.add(_gated_route_id(scene_id, link.scene_id, link.requires))
            visible_actors = {entity_id for pid in readable(scene_id) for entity_id in passages[pid].entity_ids}
            for _, actor_id in combat_events.get(scene_id, ()):
                if actor_id in visible_actors:
                    facts.update(("entity", actor_id, status)
                                 for status in ("captive", "dead", "defeated", "released"))
        for policy in manifest.resolution_policies:
            for scene_id in policy.scene_ids:
                if scene_id not in scenes:
                    continue
                for result in (("automatic",) if policy.mode == "automatic" else ("success", "failure")):
                    execution = (policy.policy_id, result, scene_id)
                    if execution not in executions and policy_possible(policy, result, scene_id):
                        executions.add(execution)
                        for effect in _policy_effects(policy, result):
                            add_effect(effect)
        for branch in prepared.branches:
            quest = quests.get(branch.quest_id)
            if (branch.scene_id not in scenes or quest is None
                    or not predicates_possible(quest.prerequisites) or not predicates_possible(branch.requires)):
                continue
            for outcome_id in branch.outcome_ids:
                outcome = outcomes.get(outcome_id)
                if (outcome is not None and outcome.branch_id == branch.branch_id
                        and predicates_possible(outcome.requires)):
                    # Separate selected actions may move, or omit an already earned
                    # move. Join those alternatives without inventing a concrete trace.
                    selected_scenes = {branch.scene_id}
                    for row in outcome.policy_selections:
                        origins = {scene_id for scene_id in selected_scenes
                                   if (row.policy_id, row.result, scene_id) in executions}
                        if not origins:
                            break
                        selected_scenes = origins | {
                            effect.target_id for effect in _policy_effects(policies[row.policy_id], row.result)
                            if effect.kind == "move"
                        }
                    else:
                        possible_outcomes.add(outcome_id)
        if inventory_size() == previous:
            break
    possible_policies = {policy_id for policy_id, _, _ in executions}
    expected_routes = {_gated_route_id(row.scene_id, link.scene_id, link.requires)
                       for row in manifest.scenes for link in row.links if link.requires}
    impossible_policies = set(policies) - possible_policies
    flag_values: dict[str, tuple[str, ...]] = {
        key: tuple(sorted(value for flag, value in flags if flag == key))
        for key in sorted({key for key, _ in flags})
    }
    return PreparedDependencyClosureReport(
        manifest_artifact_id=manifest_artifact.artifact_id, prepared_artifact_id=prepared_artifact.artifact_id,
        iterations=iterations, possible_scene_ids=tuple(sorted(scenes)),
        possible_policy_ids=tuple(sorted(possible_policies)), possible_outcome_ids=tuple(sorted(possible_outcomes)),
        possible_gated_route_ids=tuple(sorted(possible_routes)), possible_fact_states=tuple(sorted(facts)),
        possible_flag_values=flag_values, impossible_policy_ids=tuple(sorted(impossible_policies)),
        impossible_automatic_policy_ids=tuple(sorted(pid for pid in impossible_policies
                                                   if policies[pid].mode == "automatic")),
        impossible_outcome_ids=tuple(sorted(set(outcomes) - possible_outcomes)),
        impossible_gated_route_ids=tuple(sorted(expected_routes - possible_routes)),
        impossible_required_objective_ids=tuple(sorted(
            row.objective_id for row in prepared.quests if row.required_for_completion
            and ("objective", row.objective_id, "completed") not in facts)),
        impossible_required_milestone_ids=tuple(sorted({
            target for row in prepared.quests for target in row.required_milestone_ids
            if ("milestone", target, "completed") not in facts})),
        impossible_ending_ids=tuple(sorted(row.entity_id for row in manifest.entities if row.kind == "ending"
                                           and ("entity", row.entity_id, "discovered") not in facts)),
    )


def explore_prepared_paths(
    binding: PreparationRunBinding,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    *,
    initial_world_flags: Mapping[str, str] | None = None,
    grounding_artifacts: tuple[PreparationArtifact, ...] = (),
    budget: PathBudget = DEFAULT_PATH_BUDGET,
) -> PathExplorationReport:
    """Prove authored outcomes, separate endings, and one complete success trace.

    Actions consume the runtime's exact scene, passage, gear and canonical-fact guards.
    Automatic actions remain independent of check branches. Combat events are conditional
    engine-outcome witnesses tied to accepted encounter receipts, never scene-entry effects.
    """
    manifest = _manifest(manifest_artifact)
    prepared = _prepared(prepared_artifact)
    manuscript = _manuscript(manuscript_artifact)
    if prepared.source_mode != binding.source_mode or manuscript.source_mode != binding.source_mode:
        raise ValueError("Prepared campaign and manuscript must use the frozen source mode.")
    if binding.source_sha256 and manifest.source_sha256 != binding.source_sha256:
        raise ValueError("Manifest source digest does not match the frozen run binding.")
    if binding.manuscript_sha256 and binding.manuscript_sha256 != manuscript_artifact.content_sha256:
        raise ValueError("Manuscript digest does not match the frozen preparation binding.")
    if not manifest.opening_scene_id:
        raise ValueError("Bound manifest has no reviewed opening scene.")
    policies = {row.policy_id: row for row in manifest.resolution_policies}
    entities = {row.entity_id: row for row in manifest.entities}
    quests = {row.quest_id: row for row in prepared.quests}
    outcomes = {row.outcome_id: row for row in prepared.outcomes}
    expected_outcomes = set(outcomes)
    branches_by_scene: dict[str, list[Any]] = {}
    for branch in prepared.branches:
        branches_by_scene.setdefault(branch.scene_id, []).append(branch)
    automatic_by_scene: dict[str, list[ResolutionPolicy]] = {}
    for policy in manifest.resolution_policies:
        if policy.mode == "automatic":
            for scene_id in policy.scene_ids:
                automatic_by_scene.setdefault(scene_id, []).append(policy)
    required_objectives = {row.objective_id for row in prepared.quests if row.required_for_completion}
    required_milestones = {mid for row in prepared.quests for mid in row.required_milestone_ids}
    mandatory_achievements = {("objective", target) for target in required_objectives} | {
        ("milestone", target) for target in required_milestones
    }
    expected_endings = {row.entity_id for row in manifest.entities if row.kind == "ending"}
    expected_automatic = {row.policy_id for row in manifest.resolution_policies if row.mode == "automatic"}
    scenes_by_id = {row.scene_id: row for row in manifest.scenes}
    route_scenes = {
        _gated_route_id(row.scene_id, link.scene_id, link.requires): row.scene_id
        for row in manifest.scenes for link in row.links if link.requires
    }
    expected_routes = set(route_scenes)
    outcome_scenes = {outcome_id: branch.scene_id for branch in prepared.branches
                      for outcome_id in branch.outcome_ids}
    achievement_scenes: dict[tuple[str, str], set[str]] = {}
    ending_scenes: dict[str, set[str]] = {}
    for policy in manifest.resolution_policies:
        for effect in policy.on_automatic + policy.on_success + policy.on_failure:
            if effect.kind in {"objective", "milestone"} and effect.status == "completed":
                achievement_scenes.setdefault((effect.kind, effect.target_id), set()).update(policy.scene_ids)
            elif effect.kind == "discover" and effect.target_id in expected_endings:
                ending_scenes.setdefault(effect.target_id, set()).update(policy.scene_ids)
    # Distances only order work. They deliberately ignore guards and cannot establish
    # executability or absence; every actual successor still uses the full runtime guards.
    scene_distances: dict[str, dict[str, int]] = {}
    for scene_id in scenes_by_id:
        distances = {scene_id: 0}
        pending = deque([scene_id])
        while pending:
            origin = pending.popleft()
            for link in scenes_by_id[origin].links:
                if link.scene_id not in distances:
                    distances[link.scene_id] = distances[origin] + 1
                    pending.append(link.scene_id)
        scene_distances[scene_id] = distances
    object_by_id = {row.canonical_id: row for row in manifest.objects}
    definitions = {
        "objective": {row.canonical_id: row for row in manifest.objectives},
        "milestone": {row.canonical_id: row for row in manifest.milestones},
        "clock": {row.canonical_id: row for row in manifest.clocks},
    }
    passage_by_id = {row.passage_id: row for row in manifest.passages}
    combat_events = _grounded_actor_events(binding, manifest, grounding_artifacts)
    required_actor_statuses: dict[str, set[str]] = {}
    for policy in manifest.resolution_policies:
        for requirement in policy.requires_facts:
            if requirement.kind == "entity":
                required_actor_statuses.setdefault(requirement.target_id, set()).update(requirement.statuses)
    relevant_flags = {
        key for row in manifest.passages for key in row.requires
    } | {
        key for row in manifest.scenes for link in row.links for key in link.requires
    } | {
        key for row in manifest.resolution_policies for key in row.requires
    } | {
        key for row in prepared.quests for key in row.prerequisites
    } | {
        key for row in prepared.branches for key in row.requires
    } | {
        key for row in prepared.outcomes for key in row.requires
    } | {
        key for rows in (manifest.objects, manifest.objectives, manifest.milestones, manifest.clocks)
        for row in rows for key in row.requires
    }
    guarded_entity_ids = {
        requirement.target_id for policy in manifest.resolution_policies
        for requirement in policy.requires_facts if requirement.kind == "entity"
    }
    for entity_id in guarded_entity_ids | expected_endings:
        if entities[entity_id].discovery_flag:
            relevant_flags.add(entities[entity_id].discovery_flag)

    def normalize_state(state: PathState) -> PathState:
        scene_id, flags, items, locations, resolved, facts, terminal = state
        return (scene_id, tuple((key, value) for key, value in flags if key in relevant_flags),
                items, locations, resolved, facts, terminal)

    def canonical_facts(facts) -> list[CommittedPreparedFact]:
        return [CommittedPreparedFact(
            kind=(kind if kind in {"objective", "milestone", "clock"} else "entity"),
            target_id=target, status=status,
        ) for kind, target, status, _ in facts]

    def achieved(facts, endings: set[str]) -> bool:
        states = {(kind, target, status) for kind, target, status, _ in facts}
        return bool(endings) and all(
            ("objective", target, "completed") in states for target in required_objectives
        ) and all(("milestone", target, "completed") in states for target in required_milestones)

    def scene_selection(scene_id, flags):
        return select_scene_context(manifest, scene_id, flags)

    def apply_policies(state: PathState, selected: list[tuple[ResolutionPolicy, str]]):
        scene_id, flag_items, inventory_items, location_items, resolved_items, fact_items, _ = state
        flags, items, locations = dict(flag_items), set(inventory_items), dict(location_items)
        facts, resolved = set(fact_items), set(resolved_items)
        endings: set[str] = set()
        for policy, result in selected:
            effects = _policy_effects(policy, result)
            if sum(effect.kind == "move" for effect in effects) > 1:
                return None
            selection = scene_selection(scene_id, flags)
            readable = set(selection["player_passage_ids"] + selection["planner_passage_ids"])
            if (
                scene_id not in policy.scene_ids
                or not _matches_predicates(flags, policy.requires)
                or not policy_fact_requirements_met(policy.requires_facts, canonical_facts(facts))
                or not prepared_policy_available(
                    prepared, policy.policy_id, scene_id, flags, canonical_facts(facts), result=result
                )
                or not all(_has_required_item(items, item) for item in policy.requires_items)
                or not set(policy.passage_ids) <= readable
                or (result == "automatic") != (policy.mode == "automatic")
            ):
                return None
            # Runtime candidates use the pre-action snapshot. Accessible setup may authorize
            # its own private discovery reveal; every external predicate must already hold.
            pre_flags = dict(flags)
            pre_facts = set(facts)
            resulting_scene = scene_id
            for effect in effects:
                if effect.kind == "discover":
                    entity = entities[effect.target_id]
                    setup = {
                        pid for pid in policy.passage_ids if pid in readable
                        and passage_by_id[pid].audience == "player"
                        and effect.target_id in passage_by_id[pid].entity_ids
                    }
                    public_evidence = {
                        pid for pid in effect.passage_ids
                        if passage_by_id[pid].audience == "player"
                        and effect.target_id in passage_by_id[pid].entity_ids
                        and (pid in readable or (
                            scene_id in passage_by_id[pid].scene_ids
                            and entity.discovery_flag
                            and passage_by_id[pid].requires.get(entity.discovery_flag) == "discovered"
                            and all(key == entity.discovery_flag or pre_flags.get(key) == value
                                    for key, value in passage_by_id[pid].requires.items())
                        ))
                    }
                    if not setup or public_evidence != set(effect.passage_ids):
                        return None
                else:
                    public_evidence = {
                        pid for pid in effect.passage_ids if pid in readable
                        and passage_by_id[pid].audience == "player"
                    }
                if not public_evidence:
                    return None
                definition = object_by_id.get(effect.target_id) if effect.kind in {
                    "acquire", "consume", "transfer", "use", "environment"
                } else definitions.get(effect.kind, {}).get(effect.target_id)
                if definition is not None and (
                    scene_id not in definition.scene_ids
                    or not _matches_predicates(pre_flags, definition.requires)
                    or not public_evidence.intersection(definition.passage_ids)
                ):
                    return None
                prior = {row for row in pre_facts if row[0] == effect.kind and row[1] == effect.target_id}
                if prior and any(row[2] in {"completed", "failed"} and row[2] != effect.status for row in prior):
                    return None
                if effect.kind in {"acquire", "consume", "transfer", "use", "environment"}:
                    if definition is None:
                        return None
                    owner = locations.get(effect.target_id, definition.initial_owner or "")
                    if owner == "consumed":
                        return None
                    if effect.kind == "acquire":
                        if not definition.portable or owner == "party" or (owner and owner != scene_id):
                            return None
                        locations[effect.target_id] = "party"
                        items.add(definition.title.casefold().strip())
                    elif effect.kind == "consume":
                        if owner != "party" or not definition.consumable:
                            return None
                        locations[effect.target_id] = "consumed"
                        items.discard(definition.title.casefold().strip())
                    elif effect.kind == "transfer":
                        if owner != "party" or not definition.portable or effect.destination != scene_id:
                            return None
                        locations[effect.target_id] = scene_id
                        items.discard(definition.title.casefold().strip())
                    elif effect.kind == "use":
                        if owner != "party" or effect.operation not in definition.operations:
                            return None
                        destination = object_by_id.get(effect.destination) if effect.destination else None
                        if effect.destination and (destination is None or scene_id not in destination.scene_ids
                                                  or not _matches_predicates(pre_flags, destination.requires)):
                            return None
                    elif effect.operation not in definition.operations:
                        return None
                if effect.kind == "clock":
                    maximum = definition.clock_max if definition else None
                    prior_quantity = max((int(row[2]) for row in prior), default=0)
                    if maximum is None:
                        return None
                    quantity = min(maximum, prior_quantity + (effect.quantity_delta or 0))
                    facts.difference_update(prior)
                    facts.add(("clock", effect.target_id, str(quantity), tuple(effect.passage_ids)))
                elif effect.kind in {"objective", "milestone", "relationship", "encounter"}:
                    facts.difference_update(prior)
                    facts.add((effect.kind, effect.target_id, effect.status, tuple(effect.passage_ids)))
                elif effect.kind == "discover":
                    entity = entities[effect.target_id]
                    if not any(effect.target_id in passage_by_id[pid].entity_ids for pid in public_evidence):
                        return None
                    if entity.discovery_flag:
                        flags[entity.discovery_flag] = "discovered"
                    if effect.target_id in guarded_entity_ids or entity.kind == "ending":
                        facts.add(("discover", effect.target_id, effect.status, tuple(effect.passage_ids)))
                    if entity.kind == "ending":
                        endings.add(effect.target_id)
                elif effect.kind == "move":
                    if effect.target_id not in selection["allowed_scene_ids"]:
                        return None
                    resulting_scene = effect.target_id
            scene_id = resulting_scene
            if endings and policy is not selected[-1][0]:
                return None
        for quest in prepared.quests:
            if any(kind == "objective" and target == quest.objective_id and status in {"completed", "failed"}
                   for kind, target, status, _ in facts):
                resolved.add(quest.quest_id)
        return normalize_state((scene_id, tuple(sorted(flags.items())), tuple(sorted(items)),
                                tuple(sorted(locations.items())), tuple(sorted(resolved)),
                                tuple(sorted(facts)), bool(endings))), endings

    inventory = _inventory_names(binding.hero_party_snapshot)
    locations = {}
    for object_id, definition in object_by_id.items():
        if definition.initial_owner == "character" and definition.title.casefold().strip() in inventory:
            locations[object_id] = "party"
        elif definition.initial_owner:
            locations[object_id] = definition.initial_owner
    flags = dict(initial_world_flags or {})
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in flags.items()):
        raise ValueError("Initial flags must map nonempty WorldFlag IDs to string values.")
    initial_facts = set()
    initial_endings = set()
    for seed in manifest.opening_journal:
        if seed.kind == "discover":
            entity = entities[seed.target_id]
            if entity.discovery_flag:
                flags[entity.discovery_flag] = seed.status
            if entity.kind == "ending":
                initial_endings.add(seed.target_id)
        if seed.kind in {"objective", "milestone", "encounter", "discover"}:
            initial_facts.add((seed.kind, seed.target_id, seed.status, tuple(seed.passage_ids)))
    initial_resolved = {quest.quest_id for quest in prepared.quests if any(
        kind == "objective" and target == quest.objective_id and status in {"completed", "failed"}
        for kind, target, status, _ in initial_facts
    )}
    start = normalize_state((manifest.opening_scene_id, tuple(sorted(flags.items())),
                             tuple(sorted(inventory)), tuple(sorted(locations.items())),
                             tuple(sorted(initial_resolved)), tuple(sorted(initial_facts)), bool(initial_endings)))
    initial_trace = PathExample(scene_ids=(manifest.opening_scene_id,), branch_ids=(), outcome_ids=(),
                                ending_entity_ids=tuple(sorted(initial_endings)),
                                completed_objective_ids=tuple(sorted(target for kind, target, status, _ in initial_facts
                                                                     if kind == "objective" and status == "completed")),
                                completed_milestone_ids=tuple(sorted(target for kind, target, status, _ in initial_facts
                                                                     if kind == "milestone" and status == "completed")))
    reached_outcomes: set[str] = set()
    reached_automatic: set[str] = set()
    reached_routes: set[str] = set()
    route_witnesses: dict[str, PathExample] = {}
    reached_endings = set(initial_endings)
    ending_witnesses = {ending: initial_trace for ending in initial_endings}
    completion = initial_trace if achieved(initial_facts, initial_endings) else None
    examples: list[PathExample] = []
    frontier: list[tuple[tuple[int, ...], int, int, PathTrace]] = []
    insertion_order = count()
    goal_revision = 0
    best_depth: dict[PathState, int] = {}
    expanded_depth: dict[PathState, int] = {}
    expansions = 0
    state_budget_exhausted = False
    depth_limited_states: set[PathState] = set()
    depth_limited_outcomes: set[str] = set()
    depth_limited_automatic: set[str] = set()
    depth_limited_endings: set[str] = set()
    depth_limited_routes: set[str] = set()

    def all_goals_reached():
        return (reached_outcomes == expected_outcomes and reached_endings == expected_endings
                and reached_automatic == expected_automatic and reached_routes == expected_routes
                and completion is not None)

    def enqueue(state, trace, depth):
        if depth > budget.max_depth:
            depth_limited_states.add(state)
            return
        if depth >= best_depth.get(state, budget.max_depth + 1):
            return
        # Guards and completion depend only on canonical state. A shorter trace has
        # every continuation available to a longer one, plus more remaining depth.
        # Reopen previously expanded states whenever that dominating trace is found.
        best_depth[state] = depth
        heappush(frontier, (search_priority(state, depth), next(insertion_order), goal_revision,
                            (state, trace, depth)))

    def goal_inventory():
        return (len(reached_outcomes), len(reached_automatic), len(reached_routes),
                len(reached_endings), completion is not None)

    def search_priority(state, depth):
        current = {(kind, target, status) for kind, target, status, _ in state[5]}
        completed = {key for key in mandatory_achievements if (*key, "completed") in current}
        failed = sum((*key, "failed") in current for key in mandatory_achievements)
        targets: set[str] = set()
        if completion is None:
            for key in mandatory_achievements - completed:
                targets.update(achievement_scenes.get(key, ()))
            if not (mandatory_achievements - completed):
                for scenes in ending_scenes.values():
                    targets.update(scenes)
        else:
            targets.update(outcome_scenes[outcome_id] for outcome_id in expected_outcomes - reached_outcomes)
            targets.update(route_scenes[route_id] for route_id in expected_routes - reached_routes)
            for policy_id in expected_automatic - reached_automatic:
                targets.update(policies[policy_id].scene_ids)
            for ending_id in expected_endings - reached_endings:
                targets.update(ending_scenes.get(ending_id, ()))
        distance = min((scene_distances[state[0]].get(target, len(scenes_by_id) + 1)
                        for target in targets), default=0)
        # Follow promising traces deeply before enumerating independent shallow action
        # combinations. Remaining-goal distance chooses between equally deep successors.
        # This is ordering only: no state or authored transition is pruned by the score.
        return (failed if completion is None else 0, -len(completed) if completion is None else 0,
                -depth, distance)

    enqueue(start, initial_trace, 0)
    while frontier and not all_goals_reached():
        _, _, revision, (state, trace, depth) = heappop(frontier)
        if depth != best_depth[state] or expanded_depth.get(state, budget.max_depth + 1) <= depth:
            continue
        if revision != goal_revision:
            heappush(frontier, (search_priority(state, depth), next(insertion_order), goal_revision,
                                (state, trace, depth)))
            continue
        if expansions >= budget.max_states:
            state_budget_exhausted = True
            break
        expansions += 1
        expanded_depth[state] = depth
        previous_goals = goal_inventory()
        scene_id, flag_items, items, location_items, resolved, facts, terminal = state
        if terminal:
            continue
        selection = scene_selection(scene_id, dict(flag_items))
        for link in scenes_by_id[scene_id].links:
            if not _matches_predicates(dict(flag_items), link.requires):
                continue
            next_scene = link.scene_id
            route_id = _gated_route_id(scene_id, next_scene, link.requires) if link.requires else None
            next_trace = trace.model_copy(update={
                "scene_ids": trace.scene_ids + (next_scene,),
                "gated_route_ids": trace.gated_route_ids + ((route_id,) if route_id else ()),
            })
            next_state = normalize_state((next_scene, flag_items, items, location_items, resolved, facts, False))
            if route_id:
                if depth >= budget.max_depth:
                    depth_limited_routes.add(route_id)
                    depth_limited_states.add(next_state)
                    continue
                reached_routes.add(route_id)
                route_witnesses.setdefault(route_id, next_trace)
            enqueue(next_state, next_trace, depth + 1)

        # These transitions represent possible real engine combatant dispositions from an
        # executable accepted encounter. A released state requires an earlier captive state.
        for encounter_id, actor_id in combat_events.get(scene_id, ()):
            if actor_id not in {row["entity_id"] for row in selection["entities"]}:
                continue
            wanted = required_actor_statuses.get(actor_id, set()) & {"captive", "dead", "defeated", "released"}
            prior = {row for row in facts if row[0] == "actor_event" and row[1] == actor_id}
            statuses = ({"released"} if any(row[2] == "captive" for row in prior) else set())
            if not prior:
                statuses.update(wanted - {"released"})
                if "released" in wanted:
                    statuses.add("captive")
            for status in sorted(statuses & (wanted | {"captive"})):
                next_facts = (set(facts) - prior) | {("actor_event", actor_id, status, ())}
                next_state = normalize_state((scene_id, flag_items, items, location_items, resolved,
                                               tuple(sorted(next_facts)), False))
                next_trace = trace.model_copy(update={"encounter_ids": trace.encounter_ids + (encounter_id,)})
                enqueue(next_state, next_trace, depth + 1)

        candidates = []
        for policy in automatic_by_scene.get(scene_id, ()):
            candidates.append((None, None, [(policy, "automatic")]))
        for branch in branches_by_scene.get(scene_id, ()):
            quest = quests.get(branch.quest_id)
            if (quest is None or quest.quest_id in resolved
                    or not _matches_predicates(dict(flag_items), quest.prerequisites)
                    or not _matches_predicates(dict(flag_items), branch.requires)):
                continue
            for outcome_id in branch.outcome_ids:
                outcome = outcomes.get(outcome_id)
                if (outcome is None or outcome.branch_id != branch.branch_id
                        or not _matches_predicates(dict(flag_items), outcome.requires)):
                    continue
                selected = []
                for row in outcome.policy_selections:
                    policy = policies.get(row.policy_id)
                    if policy is None or policy.policy_id not in branch.policy_ids:
                        break
                    selected.append((policy, row.result))
                else:
                    candidates.append((branch, outcome, selected))
        for branch, outcome, selected in candidates:
            result = apply_policies(state, selected)
            if result is None:
                continue
            next_state, endings = result
            if depth >= budget.max_depth:
                depth_limited_states.add(next_state)
                if outcome is not None:
                    depth_limited_outcomes.add(outcome.outcome_id)
                depth_limited_endings.update(endings)
                depth_limited_automatic.update(policy.policy_id for policy, _ in selected
                                               if policy.mode == "automatic")
                continue
            if outcome is not None:
                reached_outcomes.add(outcome.outcome_id)
            reached_automatic.update(policy.policy_id for policy, _ in selected if policy.mode == "automatic")
            reached_endings.update(endings)
            next_scene = next_state[0]
            next_trace = PathExample(
                scene_ids=trace.scene_ids if next_scene == trace.scene_ids[-1] else trace.scene_ids + (next_scene,),
                branch_ids=trace.branch_ids + ((branch.branch_id,) if branch else ()),
                outcome_ids=trace.outcome_ids + ((outcome.outcome_id,) if outcome else ()),
                policy_ids=trace.policy_ids + tuple(policy.policy_id for policy, _ in selected),
                encounter_ids=trace.encounter_ids,
                ending_entity_ids=tuple(sorted(endings)),
                completed_objective_ids=tuple(sorted(target for kind, target, status, _ in next_state[5]
                                                    if kind == "objective" and status == "completed")),
                completed_milestone_ids=tuple(sorted(target for kind, target, status, _ in next_state[5]
                                                    if kind == "milestone" and status == "completed")),
                gated_route_ids=trace.gated_route_ids,
            )
            for ending in endings:
                ending_witnesses.setdefault(ending, next_trace)
            if completion is None and achieved(next_state[5], endings):
                completion = next_trace
            if len(examples) < 64 and (outcome is not None or endings):
                examples.append(next_trace)
            enqueue(next_state, next_trace, depth + 1)
        if completion is not None and goal_inventory() != previous_goals:
            goal_revision += 1
            if not previous_goals[-1]:
                # Completing the main trace switches the heuristic to all remaining
                # witness locations. Refresh once because those priorities can improve;
                # later target removal only increases distance and permits lazy refresh.
                frontier = [(search_priority(candidate[0], candidate[2]), order, goal_revision, candidate)
                            for _, order, _, candidate in frontier]
                heapify(frontier)
    exhausted = bool(
        state_budget_exhausted
        or depth_limited_states - best_depth.keys()
        or depth_limited_outcomes - reached_outcomes
        or depth_limited_automatic - reached_automatic
        or depth_limited_endings - reached_endings
        or depth_limited_routes - reached_routes
    ) and not all_goals_reached()
    missing = expected_outcomes - reached_outcomes
    missing_automatic = expected_automatic - reached_automatic
    missing_routes = expected_routes - reached_routes
    return PathExplorationReport(
        manifest_artifact_id=manifest_artifact.artifact_id,
        prepared_artifact_id=prepared_artifact.artifact_id,
        manuscript_artifact_id=manuscript_artifact.artifact_id,
        budget=budget, explored_states=expansions,
        expected_outcome_ids=tuple(sorted(outcomes)), reached_outcome_ids=tuple(sorted(reached_outcomes)),
        unreachable_outcome_ids=tuple(sorted(missing)) if not exhausted else (),
        unverified_outcome_ids=tuple(sorted(missing)) if exhausted else (),
        required_objective_ids=tuple(sorted(required_objectives)),
        required_milestone_ids=tuple(sorted(required_milestones)),
        expected_ending_ids=tuple(sorted(expected_endings)), reached_ending_ids=tuple(sorted(reached_endings)),
        ending_witnesses=tuple(ending_witnesses[ending] for ending in sorted(ending_witnesses)),
        completion_witness=completion, completion_unverified=exhausted and completion is None,
        expected_automatic_policy_ids=tuple(sorted(expected_automatic)),
        reached_automatic_policy_ids=tuple(sorted(reached_automatic)),
        unreachable_automatic_policy_ids=tuple(sorted(missing_automatic)) if not exhausted else (),
        unverified_automatic_policy_ids=tuple(sorted(missing_automatic)) if exhausted else (),
        expected_gated_route_ids=tuple(sorted(expected_routes)),
        reached_gated_route_ids=tuple(sorted(reached_routes)),
        unreachable_gated_route_ids=tuple(sorted(missing_routes)) if not exhausted else (),
        unverified_gated_route_ids=tuple(sorted(missing_routes)) if exhausted else (),
        gated_route_witnesses=route_witnesses,
        exhausted=exhausted, examples=tuple(examples),
    )


def _matches_predicates(flags: Mapping[str, str], predicates: Mapping[str, str]) -> bool:
    return all(flags.get(key) == value for key, value in predicates.items())


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


def build_path_exploration_artifact(
    binding: PreparationRunBinding,
    *,
    dependency_artifacts: tuple[PreparationArtifact, ...],
    created_at: str,
    budget: PathBudget = DEFAULT_PATH_BUDGET,
    initial_world_flags: Mapping[str, str] | None = None,
    task_id: str = "p10_path_exploration",
    scope_keys: tuple[str, ...] | None = None,
) -> PreparationArtifact:
    """Build the deterministic P10 path report from the latest accepted P09 subjects."""

    manifest_artifact = _latest_artifact_by_kind(dependency_artifacts, "canonical_manifest")
    prepared_artifact = _latest_artifact_by_kind(dependency_artifacts, "prepared_campaign")
    manuscript_artifact = _latest_artifact_by_kind(dependency_artifacts, "retained_manuscript")
    report = explore_prepared_paths(
        binding,
        manifest_artifact,
        prepared_artifact,
        manuscript_artifact,
        initial_world_flags=initial_world_flags,
        grounding_artifacts=dependency_artifacts,
        budget=budget,
    )
    return build_artifact(
        binding,
        stage=PreparationStage.P10,
        task_id=task_id,
        artifact_kind="path_exploration_report",
        payload={"path_report": report.model_dump(mode="json")},
        dependency_artifacts=_unique_artifacts(dependency_artifacts),
        created_at=created_at,
        scope_keys=scope_keys if scope_keys is not None else report.expected_outcome_ids,
    )


def make_path_exploration_validator(
    *,
    budget: PathBudget | None = None,
    initial_world_flags: Mapping[str, str] | None = None,
) -> Callable[..., ValidationResult]:
    """Create the required P10 validator; its receipt covers every proposed artifact."""

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
            actual = PathExplorationReport.model_validate(
                report_artifact.payload["path_report"], strict=False
            )
            expected = explore_prepared_paths(
                binding,
                manifest_artifact,
                prepared_artifact,
                manuscript_artifact,
                initial_world_flags=initial_world_flags,
                grounding_artifacts=dependencies,
                budget=budget if budget is not None else actual.budget,
            )
            if actual != expected:
                raise ValueError(
                    "Path report differs from bounded runtime-compatible recomputation."
                )
            if report_artifact.dependency_digests != {
                a.artifact_id: a.content_sha256 for a in dependencies
            }:
                raise ValueError("Path report does not bind exact accepted task dependencies.")
            evidence_ids = tuple(a.artifact_id for a in dependencies) + tuple(
                a.artifact_id for a in proposed_artifacts
            )
            findings: list[PreparationFinding] = []
            if actual.exhausted:
                finding = _coherence_finding(
                    "path_budget_exhausted",
                    "Bounded authored-path exploration reached its state or depth limit; complete path coverage was not established.",
                    evidence_ids,
                    affected_ids=actual.unverified_outcome_ids,
                    severity="medium",
                )
                findings.append(_preparation_finding(finding))
            for outcome_id in actual.unreachable_outcome_ids:
                finding = _coherence_finding(
                    "unreachable_prepared_outcome",
                    f"Prepared outcome {outcome_id} has no feasible path from the reviewed opening under runtime policy and route checks.",
                    evidence_ids,
                    affected_ids=(outcome_id,),
                    severity="high",
                )
                findings.append(_preparation_finding(finding))
            if actual.completion_witness is None:
                finding = _coherence_finding(
                    "campaign_completion_unverified" if actual.completion_unverified else "campaign_completion_unreachable",
                    "No single success path completes every required objective and milestone before reaching a terminal ending.",
                    evidence_ids,
                    affected_ids=actual.required_objective_ids + actual.required_milestone_ids,
                    severity="high",
                )
                findings.append(_preparation_finding(finding))
            for ending_id in sorted(set(actual.expected_ending_ids) - set(actual.reached_ending_ids)):
                finding = _coherence_finding(
                    "ending_unverified" if actual.exhausted else "ending_unreachable",
                    f"Terminal ending {ending_id} has no runtime-compatible path witness.",
                    evidence_ids, affected_ids=(ending_id,), severity="high",
                )
                findings.append(_preparation_finding(finding))
            for policy_id in actual.unreachable_automatic_policy_ids + actual.unverified_automatic_policy_ids:
                finding = _coherence_finding(
                    "automatic_policy_unverified" if actual.exhausted else "automatic_policy_unreachable",
                    f"Automatic policy {policy_id} has no executable path from the reviewed opening.",
                    evidence_ids, affected_ids=(policy_id,), severity="high",
                )
                findings.append(_preparation_finding(finding))
            routes = {
                _gated_route_id(scene.scene_id, link.scene_id, link.requires): (scene.scene_id, link.scene_id)
                for scene in _manifest(manifest_artifact).scenes for link in scene.links if link.requires
            }
            for route_id in actual.unreachable_gated_route_ids + actual.unverified_gated_route_ids:
                source, destination = routes[route_id]
                finding = _coherence_finding(
                    "gated_route_unverified" if actual.exhausted else "gated_route_unreachable",
                    f"Gated route {source} to {destination} has no causal traversal witness.",
                    evidence_ids, affected_ids=(source, destination, route_id), severity="high",
                )
                findings.append(_preparation_finding(finding))
            receipt = build_validation_receipt(
                "p10_path_exploration",
                artifacts=proposed_artifacts,
                dependencies=dependencies,
                passed=True,
                details={"path_report_sha256": report_artifact.content_sha256},
                created_at=_utc_now(),
            )
            return ValidationResult(passed=True, receipts=(receipt,), findings=tuple(findings))
        except Exception:
            failure = PreparationFinding(
                finding_id="p10-path-validator-failed",
                code="path_exploration_invalid",
                severity="critical",
                message="The path exploration report did not match its exact inputs or bounds.",
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

    The executor must pass the entire retained-manuscript artifact to the frozen Sol/high
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
