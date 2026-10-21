"""Private campaign-preparation run, artifact, review and publication contracts.

These records describe durable work. They do not make generated content trustworthy: a
PreparationArtifact is accepted only after a registered code validator returns receipts
bound to its exact digest and dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.manifests import Digest

RunId = Annotated[str, Field(pattern=r"^prep-[0-9a-f]{32}$")]
ArtifactId = Annotated[str, Field(pattern=r"^a-[0-9a-f]{32}$")]
TaskId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,95}$")]
Nonnegative = Annotated[int, Field(ge=0)]


class PreparationStage(StrEnum):
    P01 = "P01"
    P02 = "P02"
    P03 = "P03"
    P04 = "P04"
    P05 = "P05"
    P06 = "P06"
    P07 = "P07"
    P08 = "P08"
    P09 = "P09"
    P10 = "P10"
    P11 = "P11"
    P12 = "P12"

    @property
    def ordinal(self) -> int:
        return int(self.value[1:])

    @property
    def predecessor(self) -> PreparationStage | None:
        return PreparationStage(f"P{self.ordinal - 1:02d}") if self.ordinal > 1 else None


class PreparationRecord(BaseModel):
    """Strict frozen base for private persisted contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PreparationBudget(PreparationRecord):
    """Hard upper bounds reserved before any provider request is dispatched."""

    max_provider_calls: Annotated[int, Field(ge=1, le=10000)]
    max_total_tokens: Annotated[int, Field(ge=1, le=100_000_000)]
    max_cost_microusd: Annotated[int, Field(ge=0, le=10_000_000_000)]
    max_attempts_per_task: Annotated[int, Field(ge=1, le=20)] = 3
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)] = 2
    max_parallel_tasks: Annotated[int, Field(ge=1, le=32)] = 3


class PreparationRunBinding(PreparationRecord):
    """Immutable source, commission and runtime-profile snapshot for one run.

    For AI requests manuscript_sha256 is absent at commissioning and is filled by a P08
    artifact. A PDF keeps the exact original source/extraction identity throughout.
    """

    run_id: RunId
    source_mode: Literal["pdf", "ai"]
    source_slug: str
    source_version: str
    source_sha256: Digest | None = None
    extraction_sha256: Digest | None = None
    manuscript_sha256: Digest | None = None
    rules_edition: Literal["2014", "2024", "unspecified"]
    commission: dict[str, Any]
    commission_sha256: Digest
    # Historical integrity metadata. New modules use an empty snapshot and never
    # bind content or playthrough eligibility to a specific character.
    hero_party_snapshot: dict[str, Any]
    hero_party_sha256: Digest
    profile_snapshot: dict[str, Any]
    profile_snapshot_sha256: Digest
    verified_book_bindings: tuple[dict[str, Any], ...] = ()
    budget: PreparationBudget

    @model_validator(mode="after")
    def validate_source_identity(self) -> PreparationRunBinding:
        if self.source_mode == "pdf" and (self.source_sha256 is None or self.extraction_sha256 is None):
            raise ValueError("PDF preparation requires exact source and extraction digests.")
        if self.source_mode == "ai" and self.source_sha256 is not None:
            raise ValueError("AI commissioning cannot claim a source PDF digest.")
        if canonical_sha256(self.commission) != self.commission_sha256:
            raise ValueError("Commission digest does not match its frozen payload.")
        if canonical_sha256(self.hero_party_snapshot) != self.hero_party_sha256:
            raise ValueError("Hero/party digest does not match its frozen snapshot.")
        if canonical_sha256(self.profile_snapshot) != self.profile_snapshot_sha256:
            raise ValueError("Profile digest does not match its frozen snapshot.")
        return self

    @property
    def content_commission(self) -> dict[str, Any]:
        """Preferences safe to author from, excluding historical hero selection."""
        import copy

        commission = copy.deepcopy(self.commission)
        legacy_hero_setup = bool(self.hero_party_snapshot.get("heroes"))
        for payload in (commission, commission.get("setup", {})):
            if isinstance(payload, dict):
                for key in ("character_ids", "heroes", "party", "actual_party", "hero_party_snapshot"):
                    payload.pop(key, None)
                # The old form defaulted this value from the selected hero sheet.
                # Keep its frozen identity, but never use it as new authoring input.
                if legacy_hero_setup:
                    payload.pop("target_level", None)
        return commission

    @property
    def binding_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))

    @property
    def source_binding_sha256(self) -> str:
        return source_binding_sha256(self)

    def assert_integrity(self) -> None:
        """Detect mutation of nested JSON values inside an otherwise frozen model."""

        if canonical_sha256(self.commission) != self.commission_sha256:
            raise ValueError("Frozen commission changed after run creation.")
        if canonical_sha256(self.hero_party_snapshot) != self.hero_party_sha256:
            raise ValueError("Frozen hero/party snapshot changed after run creation.")
        if canonical_sha256(self.profile_snapshot) != self.profile_snapshot_sha256:
            raise ValueError("Frozen profile snapshot changed after run creation.")


class PreparationTask(PreparationRecord):
    """One bounded unit of work with exact accepted dependencies and scope coverage."""

    task_id: TaskId
    stage: PreparationStage
    task_kind: TaskId
    scope_keys: tuple[str, ...] = ()
    dependency_artifact_ids: tuple[ArtifactId, ...] = ()
    max_input_tokens: Annotated[int, Field(ge=0, le=20_000_000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=2_000_000)]
    max_provider_calls: Annotated[int, Field(ge=1, le=100)] = 1
    max_cost_microusd: Annotated[int, Field(ge=0, le=10_000_000_000)]
    requires_provider: bool = True
    repair_round_id: str | None = None

    @model_validator(mode="after")
    def unique_dependencies_and_scope(self) -> PreparationTask:
        if len(set(self.dependency_artifact_ids)) != len(self.dependency_artifact_ids):
            raise ValueError("Task dependencies must be unique.")
        if len(set(self.scope_keys)) != len(self.scope_keys):
            raise ValueError("Task scope keys must be unique.")
        if self.repair_round_id is not None and self.stage != PreparationStage.P11:
            raise ValueError("Repair tasks are permitted only in P11.")
        return self


class StagePlan(PreparationRecord):
    """Frozen expected task inventory; every planned task must cover the declared scope."""

    stage: PreparationStage
    tasks: tuple[PreparationTask, ...] = Field(min_length=1)
    required_scope_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_inventory(self) -> StagePlan:
        if any(task.stage != self.stage for task in self.tasks):
            raise ValueError("Every task in a plan must belong to its stage.")
        ids = [task.task_id for task in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("Task IDs must be unique within a stage plan.")
        covered = {key for task in self.tasks for key in task.scope_keys}
        if set(self.required_scope_keys) != covered:
            raise ValueError("The stage task inventory must exactly cover its required scope.")
        return self


class PreparationArtifact(PreparationRecord):
    """Immutable private proposal or tool output; never accepted by model declaration alone."""

    artifact_id: ArtifactId
    run_id: RunId
    stage: PreparationStage
    task_id: TaskId
    artifact_kind: TaskId
    version: Annotated[int, Field(ge=1)]
    content_sha256: Digest
    payload: dict[str, Any]
    dependency_digests: dict[ArtifactId, Digest]
    run_binding_sha256: Digest
    source_binding_sha256: Digest
    hero_party_sha256: Digest
    profile_snapshot_sha256: Digest
    created_at: str
    scope_keys: tuple[str, ...] = ()


class ResponseUsage(PreparationRecord):
    """Known usage or an explicit zero-inference refusal, counted once per response."""

    response_id: str
    input_tokens: Nonnegative
    output_tokens: Nonnegative
    cost_microusd: Nonnegative
    dispatch_id: str | None = None


class UnresolvedProviderCall(PreparationRecord):
    """An uncertain inference outcome; estimates are never reported as spent usage."""

    dispatch_id: str
    response_id: str | None = None
    call_count: Annotated[int, Field(ge=1)] = 1
    estimated_tokens: Nonnegative = 0
    estimated_cost_microusd: Nonnegative = 0


class ProviderUsage(PreparationRecord):
    """Successful/failed call accounting without credentials or prompt text.

    Numeric totals retain the known subtotal even when a later call is unresolved.
    Unknown inference costs live only in explicitly labelled unresolved estimates.
    """

    provider_called: bool
    usage_known: bool = True
    call_count: Nonnegative = 0
    input_tokens: Nonnegative = 0
    output_tokens: Nonnegative = 0
    cost_microusd: Nonnegative = 0
    requested_model: str | None = None
    reasoning_effort: str | None = None
    resolved_model: str | None = None
    task_version: str
    latency_ms: Nonnegative = 0
    failure_code: str | None = None
    responses: tuple[ResponseUsage, ...] = ()
    unresolved_calls: tuple[UnresolvedProviderCall, ...] = ()

    @model_validator(mode="after")
    def validate_provider_usage(self) -> ProviderUsage:
        if not self.provider_called and any(
            (self.call_count, self.input_tokens, self.output_tokens, self.cost_microusd)
        ):
            raise ValueError("No-provider work cannot report provider usage.")
        if self.provider_called and self.call_count < 1:
            raise ValueError("A provider attempt must record at least one call.")
        if self.unresolved_calls and (not self.provider_called or self.usage_known):
            raise ValueError("Unresolved calls require an unknown provider usage outcome.")
        if sum(call.call_count for call in self.unresolved_calls) > self.call_count:
            raise ValueError("Unresolved calls cannot exceed the recorded request count.")
        return self


class PreparationFinding(PreparationRecord):
    """Private review finding with evidence and affected IDs; never projected publicly."""

    finding_id: str
    code: TaskId
    severity: Literal["info", "low", "medium", "high", "critical"]
    message: str
    evidence_artifact_ids: tuple[ArtifactId, ...] = ()
    affected_ids: tuple[str, ...] = ()
    status: Literal["open", "resolved", "accepted_with_evidence"] = "open"
    resolution_artifact_ids: tuple[ArtifactId, ...] = ()


class ValidationReceipt(PreparationRecord):
    """Code-created result bound to exact proposal and dependency digests."""

    validator_id: TaskId
    passed: bool
    artifact_digests: dict[ArtifactId, Digest]
    dependency_digests: dict[ArtifactId, Digest]
    details_sha256: Digest
    created_at: str


class ValidationResult(PreparationRecord):
    """Explicit validator answer. Missing validators are handled as a block by the supervisor."""

    passed: bool
    receipts: tuple[ValidationReceipt, ...] = ()
    findings: tuple[PreparationFinding, ...] = ()
    block_code: TaskId | None = None

    @model_validator(mode="after")
    def require_receipt_for_pass(self) -> ValidationResult:
        if self.passed and (self.block_code is not None or not self.receipts):
            raise ValueError("A passing validation needs at least one exact receipt and no block code.")
        if not self.passed and self.block_code is None and not self.findings:
            raise ValueError("A blocked validation must explain the failure privately.")
        return self


class StageExecutionResult(PreparationRecord):
    """Untrusted proposed payloads plus mandatory usage accounting for one task."""

    proposed_artifacts: tuple[PreparationArtifact, ...] = Field(min_length=1)
    usage: ProviderUsage


class PreparationJobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    PUBLISHING = "publishing"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SafePreparationFailure(PreparationRecord):
    """Fixed taxonomy projection safe to expose in a preparation job response."""

    code: str
    category: Literal[
        "source", "review", "validation", "assignment", "busy", "provider_unavailable",
        "rate_limited", "quota_exceeded", "provider_configuration", "cancelled",
        "interrupted", "internal", "not_found",
    ]
    message: str
    recovery_action: Literal[
        "repair_source", "review_source", "fix_input", "assign_character", "wait", "retry",
        "check_existing_result", "configure_provider", "restart_preparation", "contact_support",
        "read_history",
    ]
    retryable: bool
    support_ref: str = Field(pattern=r"^err-[0-9a-f]{16}$")
    outcome: Literal["not_committed", "committed", "unknown"]


class PreparationTaskStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"


class PreparationTaskState(PreparationRecord):
    task: PreparationTask
    status: PreparationTaskStatus = PreparationTaskStatus.PENDING
    attempt_count: Nonnegative = 0
    # Retained for checkpoint compatibility; attempt counts are history, not limits.
    retry_attempt_limit: Nonnegative = 0
    lease_owner: str | None = None
    lease_fence: Nonnegative = 0
    lease_expires_at: str | None = None
    reserved_provider_calls: Nonnegative = 0
    reserved_tokens: Nonnegative = 0
    reserved_cost_microusd: Nonnegative = 0
    provider_idempotency_key: str | None = None
    accepted_artifact_ids: tuple[ArtifactId, ...] = ()
    validation_receipt_ids: tuple[str, ...] = ()
    last_safe_error_code: TaskId | None = None
    updated_at: str

    def attempt_limit(self, binding: PreparationRunBinding) -> int:
        """Legacy estimate for checkpoint compatibility; never a supervisor stop gate."""
        return max(binding.budget.max_attempts_per_task, self.retry_attempt_limit)


class PreparationRunState(PreparationRecord):
    """Private durable supervisor checkpoint; payloads never enter the public job schema."""

    state_version: Literal[1] = 1
    revision: Nonnegative = 0
    binding: PreparationRunBinding
    status: PreparationJobStatus = PreparationJobStatus.QUEUED
    current_stage: PreparationStage | None = None
    last_safe_error_code: TaskId | None = None
    last_safe_error_ref: str | None = Field(default=None, pattern=r"^err-[0-9a-f]{16}$")
    cancel_requested: bool = False
    stage_statuses: dict[str, PreparationTaskStatus] = Field(default_factory=dict)
    stage_plans: dict[str, StagePlan] = Field(default_factory=dict)
    tasks: dict[str, PreparationTaskState] = Field(default_factory=dict)
    artifacts: dict[ArtifactId, PreparationArtifact] = Field(default_factory=dict)
    validation_receipts: dict[str, ValidationReceipt] = Field(default_factory=dict)
    findings: tuple[PreparationFinding, ...] = ()
    usage_attempts: tuple[ProviderUsage, ...] = ()
    repair_round_ids: tuple[str, ...] = ()
    attempts_total: Nonnegative = 0
    provider_calls_spent: Nonnegative = 0
    tokens_spent: Nonnegative = 0
    cost_microusd_spent: Nonnegative = 0
    provider_calls_unresolved: Nonnegative = 0
    tokens_unresolved_estimate: Nonnegative = 0
    cost_microusd_unresolved_estimate: Nonnegative = 0
    unresolved_estimates_complete: bool = True
    provider_calls_reserved: Nonnegative = 0
    tokens_reserved: Nonnegative = 0
    cost_microusd_reserved: Nonnegative = 0
    repair_rounds: Nonnegative = 0
    repair_history: tuple[dict[str, Any], ...] = ()
    publication_inventory: dict[ArtifactId, Digest] | None = None
    published_bundle_sha256: Digest | None = None
    created_at: str
    updated_at: str


class PublicPreparationStatus(PreparationRecord):
    """Allowlisted player status; contains no titles, names, counts, findings or errors."""

    run_id: RunId
    status: PreparationJobStatus
    stage: str
    retryable: bool
    action: Literal["wait", "retry", "restart", "none"]
    failure: SafePreparationFailure | None = None


class PreparationPublication(PreparationRecord):
    """Exact immutable artifact and validation inventory eligible for atomic publication."""

    run_id: RunId
    binding_sha256: Digest
    artifacts: tuple[PreparationArtifact, ...] = Field(min_length=1)
    artifact_digests: dict[ArtifactId, Digest]
    validation_receipts: tuple[ValidationReceipt, ...] = Field(min_length=1)
    findings: tuple[PreparationFinding, ...] = ()
    bundle_sha256: Digest

    @model_validator(mode="after")
    def exact_inventory(self) -> PreparationPublication:
        actual = {artifact.artifact_id: artifact.content_sha256 for artifact in self.artifacts}
        if actual != self.artifact_digests:
            raise ValueError("Publication inventory must exactly match its artifacts.")
        if any(not receipt.passed for receipt in self.validation_receipts):
            raise ValueError("Every publication validation receipt must pass.")
        if any(finding.status == "open" and finding.severity in ("medium", "high", "critical") for finding in self.findings):
            raise ValueError("Material findings must be resolved before publication.")
        identity = {
            "run_id": self.run_id,
            "binding_sha256": self.binding_sha256,
            "artifacts": actual,
            "receipts": [receipt.model_dump(mode="json") for receipt in self.validation_receipts],
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
        }
        if self.bundle_sha256 != canonical_sha256(identity):
            raise ValueError("Publication digest does not match its exact inventory and receipts.")
        return self


def canonical_sha256(value: Any) -> str:
    """Hash strict canonical JSON values, matching the repository's canonical digest rules."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_content_sha256(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)


def source_binding_sha256(binding: PreparationRunBinding) -> str:
    """Hash the exact source identity independent of campaign prose and provider profile."""

    return canonical_sha256(
        {
            "source_mode": binding.source_mode,
            "source_slug": binding.source_slug,
            "source_version": binding.source_version,
            "source_sha256": binding.source_sha256,
            "extraction_sha256": binding.extraction_sha256,
        }
    )


def make_artifact_id(
    *,
    run_id: str,
    stage: PreparationStage,
    task_id: str,
    artifact_kind: str,
    version: int,
    content_sha256: str,
    dependency_digests: dict[str, str],
    run_binding_sha256: str,
) -> str:
    """Return a stable opaque ID bound to content, run and exact dependencies."""

    identity = canonical_sha256(
        {
            "run_id": run_id,
            "stage": stage.value,
            "task_id": task_id,
            "artifact_kind": artifact_kind,
            "version": version,
            "content_sha256": content_sha256,
            "dependency_digests": dependency_digests,
            "run_binding_sha256": run_binding_sha256,
        }
    )
    return f"a-{identity[:32]}"


def build_artifact(
    binding: PreparationRunBinding,
    *,
    stage: PreparationStage,
    task_id: str,
    artifact_kind: str,
    payload: dict[str, Any],
    dependency_artifacts: tuple[PreparationArtifact, ...],
    created_at: str,
    scope_keys: tuple[str, ...] = (),
    version: int = 1,
) -> PreparationArtifact:
    """Build an exact-digest proposal carrying every frozen run and dependency binding."""

    binding.assert_integrity()
    dependencies = {artifact.artifact_id: artifact.content_sha256 for artifact in dependency_artifacts}
    if len(dependencies) != len(dependency_artifacts):
        raise ValueError("Artifact dependencies must be unique.")
    if any(artifact.run_id != binding.run_id for artifact in dependency_artifacts):
        raise ValueError("Artifact dependencies must belong to the same preparation run.")
    content_sha256 = artifact_content_sha256(payload)
    artifact_id = make_artifact_id(
        run_id=binding.run_id,
        stage=stage,
        task_id=task_id,
        artifact_kind=artifact_kind,
        version=version,
        content_sha256=content_sha256,
        dependency_digests=dependencies,
        run_binding_sha256=binding.binding_sha256,
    )
    return PreparationArtifact(
        artifact_id=artifact_id,
        run_id=binding.run_id,
        stage=stage,
        task_id=task_id,
        artifact_kind=artifact_kind,
        version=version,
        content_sha256=content_sha256,
        payload=payload,
        dependency_digests=dependencies,
        run_binding_sha256=binding.binding_sha256,
        source_binding_sha256=binding.source_binding_sha256,
        hero_party_sha256=binding.hero_party_sha256,
        profile_snapshot_sha256=binding.profile_snapshot_sha256,
        created_at=created_at,
        scope_keys=scope_keys,
    )


def assert_artifact_integrity(
    binding: PreparationRunBinding,
    artifact: PreparationArtifact,
) -> None:
    """Verify every immutable artifact identity field against its frozen run payload."""

    binding.assert_integrity()
    if artifact.run_id != binding.run_id:
        raise ValueError("Artifact belongs to another preparation run.")
    if artifact.content_sha256 != artifact_content_sha256(artifact.payload):
        raise ValueError("Artifact payload digest changed after validation.")
    if artifact.run_binding_sha256 != binding.binding_sha256:
        raise ValueError("Artifact frozen run binding does not match.")
    if artifact.source_binding_sha256 != binding.source_binding_sha256:
        raise ValueError("Artifact source identity does not match.")
    if artifact.hero_party_sha256 != binding.hero_party_sha256:
        raise ValueError("Artifact hero/party snapshot does not match.")
    if artifact.profile_snapshot_sha256 != binding.profile_snapshot_sha256:
        raise ValueError("Artifact runtime profile snapshot does not match.")
    expected_id = make_artifact_id(
        run_id=artifact.run_id,
        stage=artifact.stage,
        task_id=artifact.task_id,
        artifact_kind=artifact.artifact_kind,
        version=artifact.version,
        content_sha256=artifact.content_sha256,
        dependency_digests=artifact.dependency_digests,
        run_binding_sha256=artifact.run_binding_sha256,
    )
    if artifact.artifact_id != expected_id:
        raise ValueError("Artifact identity does not match its content and dependency digests.")


def build_validation_receipt(
    validator_id: str,
    *,
    artifacts: tuple[PreparationArtifact, ...],
    dependencies: tuple[PreparationArtifact, ...],
    passed: bool,
    details: dict[str, Any],
    created_at: str,
) -> ValidationReceipt:
    """Create a receipt sealed to the exact proposal and accepted dependency inventory."""

    return ValidationReceipt(
        validator_id=validator_id,
        passed=passed,
        artifact_digests={a.artifact_id: a.content_sha256 for a in artifacts},
        dependency_digests={a.artifact_id: a.content_sha256 for a in dependencies},
        details_sha256=canonical_sha256(details),
        created_at=created_at,
    )


def build_publication(
    binding: PreparationRunBinding,
    *,
    artifacts: tuple[PreparationArtifact, ...],
    validation_receipts: tuple[ValidationReceipt, ...],
    findings: tuple[PreparationFinding, ...],
) -> PreparationPublication:
    """Create the exact complete inventory digest for the P12 atomic commit callback."""

    binding.assert_integrity()
    inventory = {artifact.artifact_id: artifact.content_sha256 for artifact in artifacts}
    identity = {
        "run_id": binding.run_id,
        "binding_sha256": binding.binding_sha256,
        "artifacts": inventory,
        "receipts": [receipt.model_dump(mode="json") for receipt in validation_receipts],
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }
    return PreparationPublication(
        run_id=binding.run_id,
        binding_sha256=binding.binding_sha256,
        artifacts=artifacts,
        artifact_digests=inventory,
        validation_receipts=validation_receipts,
        findings=findings,
        bundle_sha256=canonical_sha256(identity),
    )


def safe_public_stage(stage: PreparationStage) -> str:
    """Safe fixed progress copy; no secret campaign content can be interpolated."""

    return {
        PreparationStage.P01: "Starting preparation",
        PreparationStage.P02: "Checking sources",
        PreparationStage.P03: "Planning your adventure",
        PreparationStage.P04: "Preparing the campaign world",
        PreparationStage.P05: "Checking game rules",
        PreparationStage.P06: "Preparing scenes",
        PreparationStage.P07: "Planning campaign materials",
        PreparationStage.P08: "Writing your adventure",
        PreparationStage.P09: "Checking consistency",
        PreparationStage.P10: "Checking possible paths",
        PreparationStage.P11: "Finishing preparation checks",
        PreparationStage.P12: "Making your adventure ready",
    }[stage]


def validate_digest(value: str) -> bool:
    """Small helper for callers accepting untrusted digest fields outside Pydantic."""

    return re.fullmatch(r"[0-9a-f]{64}", value) is not None
