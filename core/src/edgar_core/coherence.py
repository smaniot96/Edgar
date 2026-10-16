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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.manifests import (
    AdventureManifest,
    ResolutionPolicy,
    manifest_digest,
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
    PreparedCampaign,
    PreparedFinding,
    _policy_effects,
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
    advance_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    climax_ids: tuple[str, ...] = Field(default=(), max_length=32)
    passage_ids: tuple[str, ...] = Field(default=(), max_length=200)
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
    max_states: int = Field(default=512, ge=1, le=100_000)
    max_depth: int = Field(default=64, ge=1, le=1000)


class PathExample(CoherenceRecord):
    scene_ids: tuple[str, ...] = Field(max_length=256)
    branch_ids: tuple[str, ...] = Field(max_length=256)
    outcome_ids: tuple[str, ...] = Field(max_length=256)


class PathExplorationReport(CoherenceRecord):
    manifest_artifact_id: str
    prepared_artifact_id: str
    manuscript_artifact_id: str
    budget: PathBudget
    explored_states: int = Field(ge=0)
    expected_outcome_ids: tuple[str, ...]
    reached_outcome_ids: tuple[str, ...]
    unreachable_outcome_ids: tuple[str, ...]
    unverified_outcome_ids: tuple[str, ...] = ()
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
        return self

    @property
    def exhaustive(self) -> bool:
        """Whether every authored outcome has a reachable or unreachable classification."""
        return not self.exhausted and not self.unverified_outcome_ids


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
PathTrace = tuple[PathState, tuple[str, ...], tuple[str, ...], tuple[str, ...], int]


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


def explore_prepared_paths(
    binding: PreparationRunBinding,
    manifest_artifact: PreparationArtifact,
    prepared_artifact: PreparationArtifact,
    manuscript_artifact: PreparationArtifact,
    *,
    initial_world_flags: Mapping[str, str] | None = None,
    budget: PathBudget = DEFAULT_PATH_BUDGET,
) -> PathExplorationReport:
    """Find a witness for each authored outcome under runtime route and policy rules.

    Movement uses ``select_scene_context`` (the same reviewed WorldFlag gate consumed by
    runtime). Policy predicates and inventory prerequisites are checked before an outcome;
    discovery outcomes set only the canonical session discovery flag, and move outcomes can
    move only into the runtime's currently allowed scene set. Once every authored outcome has
    a witness, the search stops without enumerating every combination of those alternatives.
    """

    manifest = _manifest(manifest_artifact)
    prepared = _prepared(prepared_artifact)
    manuscript = _manuscript(manuscript_artifact)
    if prepared.source_mode != binding.source_mode or manuscript.source_mode != binding.source_mode:
        raise ValueError(
            "Prepared campaign and retained manuscript must use the frozen source mode."
        )
    if (
        manifest.source_sha256
        and binding.source_sha256
        and manifest.source_sha256 != binding.source_sha256
    ):
        raise ValueError("Reviewed manifest source digest does not match the frozen run binding.")
    if (
        binding.manuscript_sha256
        and binding.manuscript_sha256 != manuscript_artifact.content_sha256
    ):
        raise ValueError(
            "Retained manuscript digest does not match the frozen preparation binding."
        )
    if not manifest.opening_scene_id:
        raise ValueError("Bound manifest has no reviewed opening scene.")
    policies = {policy.policy_id: policy for policy in manifest.resolution_policies}
    entities = {entity.entity_id: entity for entity in manifest.entities}
    quests = {quest.quest_id: quest for quest in prepared.quests}
    branches_by_scene: dict[str, list[Any]] = {}
    outcomes = {outcome.outcome_id: outcome for outcome in prepared.outcomes}
    for branch in prepared.branches:
        branches_by_scene.setdefault(branch.scene_id, []).append(branch)
    inventory = _inventory_names(binding.hero_party_snapshot)
    object_by_id = {row.canonical_id: row for row in manifest.objects}
    scene_links = {
        scene.scene_id: {link.scene_id for link in scene.links} for scene in manifest.scenes
    }
    reachable_cache: dict[str, set[str]] = {}

    def reachable_scenes(scene_id: str) -> set[str]:
        if scene_id not in reachable_cache:
            reachable = {scene_id}
            pending = [scene_id]
            while pending:
                current = pending.pop()
                for destination in scene_links.get(current, set()) - reachable:
                    reachable.add(destination)
                    pending.append(destination)
            reachable_cache[scene_id] = reachable
        return reachable_cache[scene_id]

    def normalize_state(state: PathState) -> PathState:
        (
            scene_id,
            flag_items,
            inventory_items,
            object_location_items,
            resolved_quest_items,
            durable_fact_items,
            terminal,
        ) = state
        future_scene_ids = reachable_scenes(scene_id)
        future_policies = [
            policy
            for policy in manifest.resolution_policies
            if future_scene_ids.intersection(policy.scene_ids)
        ]
        future_branches = [
            branch for branch in prepared.branches if branch.scene_id in future_scene_ids
        ]
        future_quest_ids = {branch.quest_id for branch in future_branches}
        future_branch_ids = {branch.branch_id for branch in future_branches}
        future_fact_targets = {
            (effect.kind, effect.target_id)
            for policy in future_policies
            for result in ("automatic", "success", "failure")
            for effect in _policy_effects(policy, result)
            if effect.kind in {"objective", "milestone", "relationship", "encounter"}
        }
        future_object_ids = {
            effect.target_id
            for policy in future_policies
            for result in ("automatic", "success", "failure")
            for effect in _policy_effects(policy, result)
            if effect.kind in {"acquire", "consume", "transfer", "use"}
        }
        relevant_flags = {
            key
            for candidate_scene in manifest.scenes
            if candidate_scene.scene_id in future_scene_ids
            for link in candidate_scene.links
            for key in link.requires
        }
        relevant_flags.update(key for policy in future_policies for key in policy.requires)
        relevant_flags.update(
            key
            for quest in prepared.quests
            if quest.quest_id in future_quest_ids
            for key in quest.prerequisites
        )
        relevant_flags.update(key for branch in future_branches for key in branch.requires)
        relevant_flags.update(
            key
            for outcome in prepared.outcomes
            if outcome.branch_id in future_branch_ids
            for key in outcome.requires
        )
        relevant_items = {
            item.casefold().strip() for policy in future_policies for item in policy.requires_items
        }
        relevant_items.update(
            object_by_id[object_id].title.casefold().strip()
            for object_id in future_object_ids
            if object_id in object_by_id
        )
        return (
            scene_id,
            tuple((key, value) for key, value in flag_items if key in relevant_flags),
            tuple(item for item in inventory_items if item in relevant_items),
            tuple(
                (object_id, location)
                for object_id, location in object_location_items
                if object_id in future_object_ids
            ),
            tuple(quest_id for quest_id in resolved_quest_items if quest_id in future_quest_ids),
            tuple(fact for fact in durable_fact_items if (fact[0], fact[1]) in future_fact_targets),
            terminal,
        )

    # The party snapshot is the authoritative source for initially carried items. Object
    # definitions supply location and consumption rules for later authored effects.
    object_locations: dict[str, str] = {}
    for object_id, definition in object_by_id.items():
        if _has_required_item(inventory, definition.title):
            object_locations[object_id] = "party"
        elif definition.initial_owner:
            object_locations[object_id] = definition.initial_owner
    flags = dict(initial_world_flags or {})
    if any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in flags.items()
    ):
        raise ValueError("Initial path flags must map nonempty WorldFlag IDs to string values.")
    # Include the opening's unconditional canonical state only; do not infer authored future facts.
    start: PathState = normalize_state(
        (
            manifest.opening_scene_id,
            tuple(sorted(flags.items())),
            tuple(sorted(inventory)),
            tuple(sorted(object_locations.items())),
            (),
            (),
            False,
        )
    )
    queue: deque[PathTrace] = deque([(start, (), (), (manifest.opening_scene_id,), 0)])
    queued = {start}
    visited: set[PathState] = set()
    reached_outcomes: set[str] = set()
    expected = set(outcomes)
    examples: list[PathExample] = []
    exhausted = False
    while queue and reached_outcomes != expected:
        state, path_branches, path_outcomes, path_scenes, depth = queue.popleft()
        if state in visited:
            continue
        if len(visited) >= budget.max_states:
            exhausted = True
            break
        (
            scene_id,
            flag_items,
            inventory_items,
            object_location_items,
            resolved_quest_items,
            durable_fact_items,
            terminal,
        ) = state
        visited.add(state)
        current_flags = dict(flag_items)
        current_inventory = set(inventory_items)
        current_object_locations = dict(object_location_items)
        resolved_quests = set(resolved_quest_items)
        durable_facts = set(durable_fact_items)
        try:
            scene = select_scene_context(manifest, scene_id, current_flags)
        except Exception:
            continue

        if terminal:
            continue

        # Scene navigation is constrained by the exact links and conditions used in play.
        for next_scene in sorted(set(scene.get("allowed_scene_ids", ()))):
            if next_scene == scene_id:
                continue
            next_state: PathState = normalize_state(
                (
                    next_scene,
                    flag_items,
                    inventory_items,
                    object_location_items,
                    resolved_quest_items,
                    durable_fact_items,
                    terminal,
                )
            )
            if depth >= budget.max_depth:
                if next_state not in visited and next_state not in queued:
                    exhausted = True
                continue
            if next_state not in visited and next_state not in queued:
                queued.add(next_state)
                queue.append(
                    (
                        next_state,
                        path_branches,
                        path_outcomes,
                        path_scenes + (next_scene,),
                        depth + 1,
                    )
                )

        for branch in branches_by_scene.get(scene_id, ()):
            quest = quests.get(branch.quest_id)
            if quest is None or quest.quest_id in resolved_quests:
                continue
            if not _matches_predicates(
                current_flags, quest.prerequisites
            ) or not _matches_predicates(current_flags, branch.requires):
                continue
            for outcome_id in branch.outcome_ids:
                outcome = outcomes.get(outcome_id)
                if outcome is None or outcome.branch_id != branch.branch_id:
                    continue
                if not _matches_predicates(current_flags, outcome.requires):
                    continue
                selected: list[tuple[ResolutionPolicy, str]] = []
                unavailable = False
                for selection in outcome.policy_selections:
                    policy = policies.get(selection.policy_id)
                    if (
                        policy is None
                        or policy.policy_id not in branch.policy_ids
                        or scene_id not in policy.scene_ids
                    ):
                        unavailable = True
                        break
                    if not _matches_predicates(current_flags, policy.requires):
                        unavailable = True
                        break
                    if not all(
                        _has_required_item(current_inventory, item)
                        for item in policy.requires_items
                    ):
                        unavailable = True
                        break
                    if selection.result == "automatic" and policy.mode != "automatic":
                        unavailable = True
                        break
                    if selection.result in {"success", "failure"} and policy.mode != "check":
                        unavailable = True
                        break
                    selected.append((policy, selection.result))
                if unavailable:
                    continue
                next_flags = dict(current_flags)
                next_inventory = set(current_inventory)
                next_object_locations = dict(current_object_locations)
                next_durable_facts = set(durable_facts)
                next_scene = scene_id
                next_terminal = False
                move_allowed = set(scene.get("allowed_scene_ids", ()))
                for policy, result in selected:
                    for effect in _policy_effects(policy, result):
                        definition = object_by_id.get(effect.target_id)
                        if effect.kind in {"acquire", "consume", "transfer", "use"}:
                            if definition is None:
                                unavailable = True
                                break
                            owner = next_object_locations.get(
                                effect.target_id, definition.initial_owner or ""
                            )
                            if effect.kind == "acquire":
                                if (
                                    not definition.portable
                                    or owner in {"party", "consumed"}
                                    or (owner and owner != scene_id)
                                ):
                                    unavailable = True
                                    break
                                next_object_locations[effect.target_id] = "party"
                                next_inventory.add(definition.title.casefold().strip())
                            elif effect.kind == "consume":
                                if owner != "party" or not definition.consumable:
                                    unavailable = True
                                    break
                                next_object_locations[effect.target_id] = "consumed"
                                next_inventory.discard(definition.title.casefold().strip())
                            elif effect.kind == "transfer":
                                if (
                                    owner != "party"
                                    or not definition.portable
                                    or effect.destination != scene_id
                                ):
                                    unavailable = True
                                    break
                                next_object_locations[effect.target_id] = effect.destination
                                next_inventory.discard(definition.title.casefold().strip())
                            elif owner != "party":
                                unavailable = True
                                break
                        if effect.kind in {"objective", "milestone", "relationship", "encounter"}:
                            for prior_kind, prior_target, prior_status, _ in next_durable_facts:
                                conflicts = {
                                    frozenset({"completed", "failed"}),
                                    frozenset({"friendly", "hostile"}),
                                }
                                if (
                                    effect.kind == prior_kind
                                    and effect.target_id == prior_target
                                    and effect.status != prior_status
                                    and frozenset({effect.status, prior_status}) in conflicts
                                ):
                                    unavailable = True
                                    break
                            if unavailable:
                                break
                            next_durable_facts.add(
                                (
                                    effect.kind,
                                    effect.target_id,
                                    effect.status,
                                    tuple(effect.passage_ids),
                                )
                            )
                        if effect.kind == "discover":
                            entity = entities.get(effect.target_id)
                            if entity and entity.discovery_flag:
                                next_flags[entity.discovery_flag] = "discovered"
                            if entity and entity.kind == "ending":
                                next_terminal = True
                        elif effect.kind == "move":
                            if effect.target_id in move_allowed:
                                next_scene = effect.target_id
                            else:
                                unavailable = True
                                break
                    if unavailable:
                        break
                if unavailable:
                    continue
                reached_outcomes.add(outcome.outcome_id)
                new_branches = path_branches + (branch.branch_id,)
                new_outcomes = path_outcomes + (outcome.outcome_id,)
                next_state = normalize_state(
                    (
                        next_scene,
                        tuple(sorted(next_flags.items())),
                        tuple(sorted(next_inventory)),
                        tuple(sorted(next_object_locations.items())),
                        tuple(sorted(resolved_quests | {branch.quest_id})),
                        tuple(sorted(next_durable_facts)),
                        next_terminal or bool(outcome.ending_entity_ids),
                    )
                )
                next_scenes = (
                    path_scenes if path_scenes[-1] == next_scene else path_scenes + (next_scene,)
                )
                if len(examples) < 64:
                    examples.append(
                        PathExample(
                            scene_ids=next_scenes,
                            branch_ids=new_branches,
                            outcome_ids=new_outcomes,
                        )
                    )
                if next_state not in visited and next_state not in queued:
                    if depth >= budget.max_depth:
                        exhausted = True
                    else:
                        queued.add(next_state)
                        queue.append(
                            (next_state, new_branches, new_outcomes, next_scenes, depth + 1)
                        )
    unverified = expected - reached_outcomes if exhausted else set()
    unreachable = expected - reached_outcomes if not exhausted else set()
    # The report carries exact source artifacts, not claims about arbitrary player prompts.
    return PathExplorationReport(
        manifest_artifact_id=manifest_artifact.artifact_id,
        prepared_artifact_id=prepared_artifact.artifact_id,
        manuscript_artifact_id=manuscript_artifact.artifact_id,
        budget=budget,
        explored_states=len(visited),
        expected_outcome_ids=tuple(sorted(expected)),
        reached_outcome_ids=tuple(sorted(reached_outcomes)),
        unreachable_outcome_ids=tuple(sorted(unreachable)),
        unverified_outcome_ids=tuple(sorted(unverified)),
        exhausted=exhausted,
        examples=tuple(examples),
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
    budget: PathBudget = DEFAULT_PATH_BUDGET,
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
            expected = explore_prepared_paths(
                binding,
                manifest_artifact,
                prepared_artifact,
                manuscript_artifact,
                initial_world_flags=initial_world_flags,
                budget=budget,
            )
            actual = PathExplorationReport.model_validate(
                report_artifact.payload["path_report"], strict=False
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
