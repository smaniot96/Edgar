"""Fail-closed, durable supervisor for private campaign preparation runs.

Stage workers propose artifacts only. This module owns leases, budget reservations, exact
binding checks, validator receipts, retries, cancellation and the final publication gate.
Provider execution and publication callbacks always run outside the lifecycle file lock.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog

from edgar_core.preparation import (
    PreparationArtifact,
    PreparationFinding,
    PreparationJobStatus,
    PreparationPublication,
    PreparationRunBinding,
    PreparationRunState,
    PreparationStage,
    PreparationTask,
    PreparationTaskState,
    PreparationTaskStatus,
    ProviderUsage,
    PublicPreparationStatus,
    SafePreparationFailure,
    StageExecutionResult,
    StagePlan,
    UnresolvedProviderCall,
    ValidationReceipt,
    ValidationResult,
    assert_artifact_integrity,
    build_artifact,
    build_publication,
    build_validation_receipt,
    canonical_sha256,
    safe_public_stage,
)
from edgar_core.safe_errors import OutcomeState, classify_failure

from .preparation_store import PreparationStore, StalePreparationRevision

log = structlog.get_logger(__name__)

_WORKER_GUARD: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "preparation_worker_guard", default=None
)


def assert_current_preparation_worker() -> None:
    """Fence each external subcall when running inside a supervised stage."""
    guard = _WORKER_GUARD.get()
    if guard is not None:
        guard()


class PreparationError(RuntimeError):
    """A preparation run cannot safely make the requested transition."""


class PreparationTaskFailure(PreparationError):
    """Worker failure carrying provider usage when the provider reports it."""

    def __init__(
        self,
        code: str,
        usage: ProviderUsage | None = None,
        findings: tuple[PreparationFinding, ...] = (),
    ) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", code):
            raise ValueError("Failure codes must be safe fixed identifiers.")
        super().__init__(code)
        self.code = code
        self.usage = usage
        self.findings = findings


class SourceGuard(Protocol):
    def __call__(self, binding: PreparationRunBinding) -> bool | None: ...


class Planner(Protocol):
    def __call__(
        self, binding: PreparationRunBinding, accepted_artifacts: tuple[PreparationArtifact, ...]
    ) -> StagePlan | Awaitable[StagePlan]: ...


class Executor(Protocol):
    def __call__(
        self,
        binding: PreparationRunBinding,
        task: PreparationTask,
        dependency_artifacts: tuple[PreparationArtifact, ...],
        attempt: int,
        idempotency_key: str,
    ) -> StageExecutionResult | Awaitable[StageExecutionResult]: ...


class Validator(Protocol):
    def __call__(
        self,
        binding: PreparationRunBinding,
        task: PreparationTask,
        proposed_artifacts: tuple[PreparationArtifact, ...],
        dependency_artifacts: tuple[PreparationArtifact, ...],
    ) -> ValidationResult | Awaitable[ValidationResult]: ...


class Publisher(Protocol):
    def __call__(self, publication: PreparationPublication) -> str | Awaitable[str]: ...


@dataclass(frozen=True)
class StageAdapter:
    plan: Planner
    execute: Executor


@dataclass(frozen=True)
class PreparationStep:
    state: PreparationRunState
    completed_task_id: str | None = None
    publication_digest: str | None = None


_P09_REQUIRED_VALIDATORS = {"p09_deterministic_review", "p09_independent_critic"}
_P04_REQUIRED_VALIDATORS = {"p04_prepared_campaign"}
_P12_REQUIRED_VALIDATORS = {"p12_publication_inventory"}
_CONFIG_RETRY_CODES = {
    "stage_contract_missing",
    "stage_plan_invalid",
    "publication_callback_missing",
    "publication_failed",
}
_AUTOMATIC_RETRY_CODES = {"provider_unavailable", "rate_limited", "provider_rate_limited"}


def automatically_retryable_preparation_task(task: PreparationTaskState) -> bool:
    """Only a confirmed transient failure may loop without an external change."""
    return (task.status == PreparationTaskStatus.FAILED
            and task.last_safe_error_code in _AUTOMATIC_RETRY_CODES)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _expired(value: str | None, now: datetime) -> bool:
    if value is None:
        return True
    try:
        return datetime.fromisoformat(value) <= now
    except ValueError:
        return True


def _task_key(task: PreparationTask) -> str:
    return f"{task.stage.value}:{task.task_id}"


def _dependency_map(artifacts: tuple[PreparationArtifact, ...]) -> dict[str, str]:
    return {artifact.artifact_id: artifact.content_sha256 for artifact in artifacts}


class PreparationSupervisor:
    """Coordinates bounded stage workers over durable, private checkpoints."""

    def __init__(
        self,
        *,
        adapters: Mapping[PreparationStage, StageAdapter],
        validators: Mapping[PreparationStage, tuple[Validator, ...]],
        store: PreparationStore | None = None,
        lease_seconds: int = 300,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("Lease duration must be positive.")
        self.adapters = dict(adapters)
        self.validators = {key: tuple(value) for key, value in validators.items()}
        self.store = store or PreparationStore()
        self.lease_seconds = lease_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    def create_run(self, binding: PreparationRunBinding) -> PreparationRunState:
        binding.assert_integrity()
        return self.store.create_run(binding)

    def status(self, slug: str, run_id: str) -> PublicPreparationStatus:
        """Project only fixed player-safe state labels and retry actions."""
        state = self.store.load_run(slug, run_id)
        stage = state.current_stage
        if state.status == PreparationJobStatus.READY:
            stage_label, action = "Ready to play", "none"
        elif state.status in (PreparationJobStatus.BLOCKED, PreparationJobStatus.FAILED):
            stage_label = safe_public_stage(stage) if stage else "Preparation needs attention"
            action = "retry" if self._has_retryable_work(state) or state.last_safe_error_code in _CONFIG_RETRY_CODES else "restart"
        elif state.status == PreparationJobStatus.CANCELLED:
            stage_label, action = "Preparation cancelled", "restart"
        else:
            stage_label = safe_public_stage(stage) if stage else "Starting preparation"
            action = "wait"
        return PublicPreparationStatus(
            run_id=run_id,
            status=state.status,
            stage=stage_label,
            retryable=action in ("retry", "restart"),
            action=action,
            failure=(
                _safe_preparation_failure(
                    state.last_safe_error_code, state.last_safe_error_ref, run_id
                )
                if state.status in (PreparationJobStatus.BLOCKED, PreparationJobStatus.FAILED)
                else None
            ),
        )

    async def step(
        self,
        slug: str,
        run_id: str,
        worker_id: str,
        source_guard: SourceGuard,
        publisher: Publisher | None = None,
    ) -> PreparationStep:
        """Execute at most one task, or publish after every exact gate has passed."""
        if not worker_id or len(worker_id) > 128:
            raise ValueError("Worker identity is required.")
        state = self.store.load_run(slug, run_id)
        self._check_live(state, source_guard)
        if state.status == PreparationJobStatus.READY:
            return PreparationStep(state, publication_digest=state.published_bundle_sha256)
        if state.status == PreparationJobStatus.CANCELLED:
            raise PreparationError(f"Run is {state.status.value}.")

        state = self._recover_expired_leases(slug, run_id, state)
        state = self._retry_failed_tasks(slug, run_id, state)
        state = await self._ensure_current_plan(slug, run_id, state)
        if state.status == PreparationJobStatus.BLOCKED:
            return PreparationStep(state)
        stage = state.current_stage
        if stage is None:
            raise PreparationError("No current stage exists before publication.")
        plan = state.stage_plans[stage.value]
        task_state = next(
            (state.tasks[_task_key(task)] for task in plan.tasks
             if state.tasks[_task_key(task)].status == PreparationTaskStatus.PENDING),
            None,
        )
        if task_state is not None:
            return await self._execute_task(slug, run_id, worker_id, source_guard, state, task_state)
        if all(state.tasks[_task_key(task)].status == PreparationTaskStatus.ACCEPTED for task in plan.tasks):
            if stage == PreparationStage.P11 and any(a.artifact_kind == "repair_plan" for a in state.artifacts.values()):
                return PreparationStep(self._restart_for_repair(slug, run_id, state))
            state = self._finish_stage(slug, run_id, state, stage)
            if state.status == PreparationJobStatus.BLOCKED:
                return PreparationStep(state)
            if stage != PreparationStage.P12:
                state = await self._ensure_current_plan(slug, run_id, state)
                return PreparationStep(state)
            return await self._publish(slug, run_id, source_guard, publisher, state)
        return PreparationStep(state)

    def cancel(self, slug: str, run_id: str) -> PreparationRunState:
        def update(payload: dict[str, Any]) -> None:
            status = payload["status"]
            if status == PreparationJobStatus.READY.value:
                raise PreparationError("A ready run cannot be cancelled.")
            if status == PreparationJobStatus.CANCELLED.value:
                return
            payload["cancel_requested"] = True
            payload["status"] = PreparationJobStatus.CANCELLED.value
            for item in payload["tasks"].values():
                if item["status"] in ("pending", "leased", "running"):
                    if item["status"] in ("leased", "running"):
                        task = item["task"]
                        called = task["requires_provider"]
                        usage = ProviderUsage(
                            provider_called=called,
                            usage_known=False,
                            call_count=max(item["reserved_provider_calls"], 1) if called else 0,
                            task_version=(
                                f"{task['stage']}.{task['task_kind']}.attempt-{item['attempt_count']}"
                            ),
                            failure_code="cancelled",
                        )
                        payload["usage_attempts"].append(_with_unresolved_estimates(
                            usage, item["reserved_provider_calls"], item["reserved_tokens"],
                            item["reserved_cost_microusd"],
                            canonical_sha256({"run": run_id, "task": task["task_id"],
                                              "attempt": item["attempt_count"]}),
                        ).model_dump(mode="json"))
                    item["status"] = "cancelled"
                    item["lease_owner"] = None
                    item["lease_expires_at"] = None
                    item["lease_fence"] += 1
            # Cancellation does not establish inference usage or a charge.
            _refresh_unresolved_usage(payload)
            payload["provider_calls_reserved"] = 0
            payload["tokens_reserved"] = 0
            payload["cost_microusd_reserved"] = 0

        return self.store.mutate_run(slug, run_id, update=update)

    def retry(self, slug: str, run_id: str) -> PreparationRunState:
        """Explicitly resume safely retained work after a recorded failure/block."""
        state = self.store.load_run(slug, run_id)
        if state.status not in (PreparationJobStatus.BLOCKED, PreparationJobStatus.FAILED):
            raise PreparationError("Only a blocked or failed run can be retried.")
        if state.cancel_requested:
            raise PreparationError("A cancelled run must be restarted with a new run identity.")

        def update(payload: dict[str, Any]) -> None:
            changed = False
            for item in payload["tasks"].values():
                if item["status"] in ("failed", "blocked"):
                    item["status"] = "pending"
                    item["last_safe_error_code"] = None
                    item["lease_owner"] = None
                    item["lease_expires_at"] = None
                    changed = True
            if (not changed and payload["status"] == "blocked"
                    and payload.get("last_safe_error_code") not in _CONFIG_RETRY_CODES):
                raise PreparationError("No failed task is retryable; fix the missing stage contract first.")
            payload["status"] = "queued"
            payload["last_safe_error_code"] = None
            payload["last_safe_error_ref"] = None

        return self.store.mutate_run(slug, run_id, update=update)

    def _retry_failed_tasks(
        self, slug: str, run_id: str, state: PreparationRunState
    ) -> PreparationRunState:
        """Resume retryable provider failures without losing their durable attempt record."""
        retryable = [task for task in state.tasks.values()
                     if automatically_retryable_preparation_task(task)]
        if not retryable:
            return state

        def update(payload: dict[str, Any]) -> None:
            for task in payload["tasks"].values():
                if (task["status"] == "failed"
                        and task.get("last_safe_error_code") in _AUTOMATIC_RETRY_CODES):
                    task["status"] = "pending"
            payload["status"] = "preparing"
            payload["last_safe_error_code"] = None
            payload["last_safe_error_ref"] = None
        return self.store.mutate_run(slug, run_id, update=update)

    def invalidate_dependents(
        self, slug: str, run_id: str, artifact_ids: tuple[str, ...]
    ) -> PreparationRunState:
        """Invalidate the transitive dependent closure when a source artifact is amended."""
        state = self.store.load_run(slug, run_id)
        if state.status == PreparationJobStatus.READY:
            raise PreparationError("Published preparation is immutable; create a new version.")
        known = set(state.artifacts)
        if not artifact_ids or not set(artifact_ids) <= known:
            raise PreparationError("Invalidation roots must name accepted run artifacts.")
        invalid = set(artifact_ids)
        changed = True
        while changed:
            changed = False
            for artifact in state.artifacts.values():
                if artifact.artifact_id not in invalid and set(artifact.dependency_digests) & invalid:
                    invalid.add(artifact.artifact_id)
                    changed = True

        def update(payload: dict[str, Any]) -> None:
            if payload["status"] == "ready":
                raise PreparationError("Published preparation is immutable.")
            root_tasks = {
                f"{artifact['stage']}:{artifact['task_id']}"
                for key, artifact in payload["artifacts"].items() if key in invalid
            }
            first_stage = min((
                PreparationStage(payload["tasks"][key]["task"]["stage"]).ordinal
                for key in root_tasks if key in payload["tasks"]
            ), default=1)
            removed_artifacts = {
                artifact_id
                for artifact_id, artifact in payload["artifacts"].items()
                if PreparationStage(artifact["stage"]).ordinal >= first_stage
            }
            removed_tasks = {
                key for key, task in payload["tasks"].items()
                if PreparationStage(task["task"]["stage"]).ordinal >= first_stage
            }
            if any(payload["tasks"][key]["status"] in ("leased", "running") for key in removed_tasks):
                raise PreparationError("Dependent work must finish or be cancelled before invalidation.")
            for key in removed_artifacts:
                payload["artifacts"].pop(key, None)
            for key in removed_tasks:
                payload["tasks"].pop(key, None)
            for stage in PreparationStage:
                if stage.ordinal >= first_stage:
                    payload["stage_plans"].pop(stage.value, None)
                    payload["stage_statuses"].pop(stage.value, None)
            used_artifacts = {a for t in payload["tasks"].values() for a in t["accepted_artifact_ids"]}
            payload["validation_receipts"] = {
                key: receipt for key, receipt in payload["validation_receipts"].items()
                if (set(receipt["artifact_digests"]) <= used_artifacts
                    and set(receipt["dependency_digests"]) <= used_artifacts)
            }
            payload["findings"] = [
                finding for finding in payload["findings"]
                if not (set(finding["evidence_artifact_ids"]) | set(finding["resolution_artifact_ids"]))
                & removed_artifacts
            ]
            payload["current_stage"] = f"P{first_stage:02d}"
            payload["status"] = "queued"
            payload["last_safe_error_code"] = None
            payload["last_safe_error_ref"] = None
            payload["publication_inventory"] = None
            payload["published_bundle_sha256"] = None

        return self.store.mutate_run(slug, run_id, update=update)

    def _restart_for_repair(self, slug: str, run_id: str, state: PreparationRunState) -> PreparationRunState:
        """Archive exact review evidence and rewind blueprint compilation atomically.

        P11 retains local repair directions and the accepted blueprint. Source study
        and design stay accepted; compiled dependents and independent reviews must
        be rebuilt from the retained blueprint and earn fresh clearance.
        """
        plans = [a for a in state.artifacts.values() if a.artifact_kind == "repair_plan"]
        if len(plans) != 1:
            return self._block_run(slug, run_id, "repair_scope_invalid")
        if state.repair_rounds >= state.binding.budget.max_repair_rounds:
            log.warning("preparation_repair_estimate_exceeded", run_id=run_id,
                        repair_rounds=state.repair_rounds)
        proposal = plans[0]
        material = {f.finding_id for f in state.findings
                    if f.status == "open" and f.severity in ("medium", "high", "critical")}
        directions = proposal.payload.get("directions", {})
        if not material or set(directions.get("finding_ids", ())) != material or directions.get("preserve_exact_source") is not True:
            return self._block_run(slug, run_id, "repair_scope_invalid")
        if any(t.status in (PreparationTaskStatus.RUNNING, PreparationTaskStatus.LEASED) for t in state.tasks.values()):
            return state
        restart_ordinal = 4
        retained = tuple(a for a in state.artifacts.values() if a.stage.ordinal < restart_ordinal)
        discarded = tuple(a for a in state.artifacts.values() if a.stage.ordinal >= restart_ordinal)
        blueprints = [a for a in discarded if a.artifact_kind == "compact_blueprint"]
        if len(blueprints) != 1:
            log.warning("preparation_blueprint_conversion_required", run_id=run_id,
                        reason="Historical authoring needs one retained campaign blueprint; completed work is saved.")
            return self._block_run(slug, run_id, "preparation_compile_invalid")
        round_number = state.repair_rounds + 1
        round_id = f"repair_{round_number}"
        archive = {"round_id": round_id, "artifacts": [a.model_dump(mode="json") for a in discarded],
                   "validation_receipts": [r.model_dump(mode="json") for r in state.validation_receipts.values()],
                   "findings": [f.model_dump(mode="json") for f in state.findings],
                   "prior_inventory": {a.artifact_id: a.content_sha256 for a in state.artifacts.values()}}
        context_task = PreparationTask(task_id=round_id, stage=PreparationStage.P02,
            task_kind="repair_context", scope_keys=(), dependency_artifact_ids=tuple(a.artifact_id for a in retained),
            max_input_tokens=0, max_output_tokens=1, max_cost_microusd=0, requires_provider=False)
        context = build_artifact(state.binding, stage=PreparationStage.P02, task_id=round_id,
            artifact_kind="repair_context", payload={"round_id": round_id,
                "directions": directions, "findings": proposal.payload.get("findings", []),
                "retained_blueprint": blueprints[0].payload,
                "archive_sha256": canonical_sha256(archive)}, dependency_artifacts=retained,
            version=round_number, created_at=_iso(_now()), scope_keys=())
        receipt = build_validation_receipt("p02_repair_context", artifacts=(context,), dependencies=retained,
            passed=True, details={"archive_sha256": canonical_sha256(archive)}, created_at=_iso(_now()))
        receipt_key = receipt.validator_id + ":" + receipt.details_sha256
        def restart(payload: dict[str, Any]) -> None:
            if payload["cancel_requested"] or payload["status"] in ("ready", "cancelled"):
                raise PreparationError("Repair was superseded.")
            payload["repair_history"].append(archive)
            payload["repair_rounds"] = round_number
            payload["repair_round_ids"].append(round_id)
            payload["artifacts"] = {a.artifact_id: a.model_dump(mode="json") for a in retained}
            payload["artifacts"][context.artifact_id] = context.model_dump(mode="json")
            payload["tasks"] = {k: t for k, t in payload["tasks"].items()
                                if PreparationStage(t["task"]["stage"]).ordinal < restart_ordinal}
            payload["tasks"][_task_key(context_task)] = PreparationTaskState(task=context_task,
                status=PreparationTaskStatus.ACCEPTED, accepted_artifact_ids=(context.artifact_id,),
                validation_receipt_ids=(receipt_key,), updated_at=_iso(_now())).model_dump(mode="json")
            kept_ids = {a.artifact_id for a in retained}
            payload["validation_receipts"] = {k: r for k, r in payload["validation_receipts"].items()
                if set(r["artifact_digests"]) <= kept_ids and set(r["dependency_digests"]) <= kept_ids}
            payload["validation_receipts"][receipt_key] = receipt.model_dump(mode="json")
            for stage in PreparationStage:
                if stage.ordinal >= restart_ordinal:
                    payload["stage_plans"].pop(stage.value, None)
                    payload["stage_statuses"].pop(stage.value, None)
            payload["stage_plans"]["P02"]["tasks"].append(context_task.model_dump(mode="json"))
            payload.update(findings=[], current_stage=f"P{restart_ordinal:02}", status="repairing", last_safe_error_code=None,
                           last_safe_error_ref=None,
                           publication_inventory=None, published_bundle_sha256=None)
        try:
            return self.store.update_run(slug, run_id, expected_revision=state.revision, update=restart)
        except StalePreparationRevision:
            return self.store.load_run(slug, run_id)

    async def _ensure_current_plan(
        self, slug: str, run_id: str, state: PreparationRunState
    ) -> PreparationRunState:
        accepted = tuple(state.artifacts.values())
        if state.current_stage is None:
            next_stage = next((stage for stage in PreparationStage
                               if state.stage_statuses.get(stage.value) != "accepted"), None)
            if next_stage is None:
                return state
            state = self.store.mutate_run(slug, run_id, update=lambda payload: payload.update(
                current_stage=next_stage.value, status="preparing"
            ))
        stage = state.current_stage
        if stage is None:
            return state
        expected = self._required_validator_ids(stage)
        registered = self.validators.get(stage, ())
        registered_ids = {getattr(v, "validator_id", "") for v in registered}
        if stage not in self.adapters or not registered or not expected <= registered_ids:
            return self._block_run(slug, run_id, "stage_contract_missing")
        if stage.value in state.stage_plans:
            return state
        adapter = self.adapters[stage]
        try:
            plan = await _call(adapter.plan, state.binding, accepted)
            if not isinstance(plan, StagePlan) or plan.stage != stage:
                raise ValueError("invalid stage plan")
            self._validate_plan(state, plan)
        except Exception as exc:
            from .preparation_proposals import save_diagnostic

            save_diagnostic(state.binding, f"{stage.value}:plan", exc, dependencies=accepted)
            return self._block_run(slug, run_id, "stage_plan_invalid")

        def save(payload: dict[str, Any]) -> None:
            if payload["current_stage"] != stage.value or stage.value in payload["stage_plans"]:
                return
            payload["stage_plans"][stage.value] = plan.model_dump(mode="json")
            for task in plan.tasks:
                key = _task_key(task)
                if key not in payload["tasks"]:
                    payload["tasks"][key] = PreparationTaskState(
                        task=task, updated_at=_iso(_now())
                    ).model_dump(mode="json")
            payload["stage_statuses"][stage.value] = "pending"
            payload["status"] = "preparing"
        return self.store.mutate_run(slug, run_id, update=save)

    @staticmethod
    def _required_validator_ids(stage: PreparationStage) -> set[str]:
        return {
            PreparationStage.P04: _P04_REQUIRED_VALIDATORS,
            PreparationStage.P09: _P09_REQUIRED_VALIDATORS,
            PreparationStage.P12: _P12_REQUIRED_VALIDATORS,
        }.get(stage, set())

    def _validate_plan(self, state: PreparationRunState, plan: StagePlan) -> None:
        accepted = set(state.artifacts)
        for task in plan.tasks:
            if not set(task.dependency_artifact_ids) <= accepted:
                raise ValueError("Stage plan names an unavailable dependency.")
            if task.stage.ordinal > 1 and not task.dependency_artifact_ids:
                raise ValueError("Downstream tasks must bind accepted dependencies.")
        if plan.stage in (PreparationStage.P05, PreparationStage.P06, PreparationStage.P08):
            backbones = [a for a in state.artifacts.values()
                         if a.stage == PreparationStage.P03 and "scope_contract" in a.payload]
            if len(backbones) != 1 or not isinstance(backbones[0].payload["scope_contract"], dict):
                raise ValueError("P03 must provide one canonical scope contract before act work.")
            acts = backbones[0].payload["scope_contract"].get("act_ids")
            if (not isinstance(acts, list) or not acts
                    or not all(isinstance(act, str) and act for act in acts)
                    or len(set(acts)) != len(acts)):
                raise ValueError("P03 act scope is invalid.")
            if set(plan.required_scope_keys) != set(acts):
                raise ValueError(f"{plan.stage.value} plan must exactly cover every P03 act.")

    def _recover_expired_leases(
        self, slug: str, run_id: str, state: PreparationRunState
    ) -> PreparationRunState:
        now = _now()
        if not any(task.status in (PreparationTaskStatus.LEASED, PreparationTaskStatus.RUNNING)
                   and _expired(task.lease_expires_at, now) for task in state.tasks.values()):
            return state

        def recover(payload: dict[str, Any]) -> None:
            for task in payload["tasks"].values():
                if task["status"] not in ("leased", "running") or not _expired(task.get("lease_expires_at"), now):
                    continue
                calls, tokens, cost = (task["reserved_provider_calls"], task["reserved_tokens"],
                                       task["reserved_cost_microusd"])
                payload["provider_calls_reserved"] -= calls
                payload["tokens_reserved"] -= tokens
                payload["cost_microusd_reserved"] -= cost
                task_spec = task["task"]
                called = task_spec["requires_provider"]
                usage = ProviderUsage(
                    provider_called=called,
                    usage_known=False,
                    call_count=max(calls, 1) if called else 0,
                    task_version=(
                        f"{task_spec['stage']}.{task_spec['task_kind']}.attempt-{task['attempt_count']}"
                    ),
                    failure_code="lease_expired",
                )
                payload["usage_attempts"].append(_with_unresolved_estimates(
                    usage, calls, tokens, cost,
                    canonical_sha256({"run": run_id, "task": task_spec["task_id"],
                                      "attempt": task["attempt_count"]}),
                ).model_dump(mode="json"))
                task.update(status="pending", lease_owner=None, lease_expires_at=None,
                            reserved_provider_calls=0, reserved_tokens=0,
                            reserved_cost_microusd=0, last_safe_error_code="lease_expired",
                            lease_fence=task["lease_fence"] + 1)
            payload["status"] = "preparing"
            _refresh_unresolved_usage(payload)
        return self.store.mutate_run(slug, run_id, update=recover)

    async def _execute_task(
        self, slug: str, run_id: str, worker_id: str, source_guard: SourceGuard,
        state: PreparationRunState, task_state: PreparationTaskState,
    ) -> PreparationStep:
        task = task_state.task
        if task_state.attempt_count and task_state.last_safe_error_code in _AUTOMATIC_RETRY_CODES:
            # Backoff controls retry pacing, never the number of attempts.
            await asyncio.sleep(min(30.0, 2.0 ** min(task_state.attempt_count, 5)))
        self._check_live(state, source_guard)
        reserved_calls = task.max_provider_calls if task.requires_provider else 0
        reserved_tokens = task.max_input_tokens + task.max_output_tokens if task.requires_provider else 0
        reserved_cost = task.max_cost_microusd if task.requires_provider else 0
        available = state.binding.budget
        semaphore = self._semaphores.setdefault(run_id, asyncio.Semaphore(available.max_parallel_tasks))
        async with semaphore:
            try:
                state = self._claim_task(slug, run_id, worker_id, task, reserved_calls,
                                         reserved_tokens, reserved_cost)
            except PreparationError:
                # A duplicate worker may have claimed the same pending task, or the
                # run was cancelled while this worker waited for its semaphore.
                return PreparationStep(self.store.load_run(slug, run_id))
            task_state = state.tasks[_task_key(task)]
            task = task_state.task
            deps = tuple(state.artifacts[artifact_id] for artifact_id in task.dependency_artifact_ids)
            adapter = self.adapters[task.stage]
            idem_key = task_state.provider_idempotency_key or canonical_sha256(
                {"run_id": run_id, "task_id": task.task_id, "stage": task.stage.value,
                 "scope": task.scope_keys, "dependencies": _dependency_map(deps)}
            )
            try:
                self._check_live(state, source_guard)
                async def execute_and_validate():
                    result = await _call(adapter.execute, state.binding, task, deps,
                                         task_state.attempt_count, idem_key)
                    if not isinstance(result, StageExecutionResult):
                        raise PreparationTaskFailure("worker_result_invalid")
                    self._check_live(self.store.load_run(slug, run_id), source_guard)
                    receipts, findings = await self._validate_outputs(state.binding, task, result, deps)
                    return result, receipts, findings

                result, receipts, findings = await self._execute_with_lease(
                    slug, run_id, worker_id, task, task_state.lease_fence, source_guard,
                    execute_and_validate,
                )
                self._check_live(self.store.load_run(slug, run_id), source_guard)
                saved = self._accept_task(slug, run_id, worker_id, task, task_state.lease_fence,
                                          result, receipts, findings, reserved_calls, reserved_tokens, reserved_cost)
                return PreparationStep(saved, completed_task_id=task.task_id)
            except asyncio.CancelledError:
                self._record_failure(slug, run_id, worker_id, task, task_state.lease_fence,
                                     "worker_cancelled", None, reserved_calls, reserved_tokens, reserved_cost)
                raise
            except PreparationTaskFailure as exc:
                state = self._record_failure(slug, run_id, worker_id, task, task_state.lease_fence,
                                             exc.code, exc.usage, reserved_calls, reserved_tokens, reserved_cost,
                                             exc.findings)
                return PreparationStep(state)
            except Exception as exc:
                state = self._record_failure(slug, run_id, worker_id, task, task_state.lease_fence,
                                             _exception_code(exc), None, reserved_calls, reserved_tokens, reserved_cost)
                return PreparationStep(state)

    async def _execute_with_lease(
        self, slug, run_id, worker_id, task, fence, source_guard, executor, *args
    ):
        """Keep a long multi-call stage fenced and stop work after cancellation.

        The heartbeat holds a lifecycle lock only while extending the exact active
        lease. No lock or database transaction spans provider work.
        """
        def guard():
            latest = self.store.load_run(slug, run_id)
            self._check_live(latest, source_guard)
            saved = latest.tasks[_task_key(task)]
            if (saved.status != PreparationTaskStatus.RUNNING
                    or saved.lease_owner != worker_id or saved.lease_fence != fence):
                raise PreparationError("Task lease was superseded before an external call.")

        token = _WORKER_GUARD.set(guard)
        try:
            work = asyncio.create_task(_call(executor, *args))
        finally:
            _WORKER_GUARD.reset(token)
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=min(30.0, self.lease_seconds / 3))
                if done:
                    return await work
                self._check_live(self.store.load_run(slug, run_id), source_guard)

                def renew(payload):
                    if payload["cancel_requested"] or payload["status"] in ("cancelled", "ready"):
                        raise PreparationError("Preparation run stopped during work.")
                    saved = payload["tasks"][_task_key(task)]
                    _assert_fence(saved, worker_id, fence)
                    saved["lease_expires_at"] = _iso(_now() + timedelta(seconds=self.lease_seconds))

                self.store.mutate_run(slug, run_id, update=renew)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    def _claim_task(
        self, slug: str, run_id: str, worker_id: str, task: PreparationTask,
        calls: int, tokens: int, cost: int,
    ) -> PreparationRunState:
        def update(payload: dict[str, Any]) -> None:
            if payload["cancel_requested"] or payload["status"] in ("cancelled", "ready"):
                raise PreparationError("Run cannot dispatch new work.")
            key = _task_key(task)
            saved = payload["tasks"][key]
            if saved["status"] != "pending":
                raise PreparationError("Task is no longer pending.")
            if task.stage == PreparationStage.P04 and saved["attempt_count"] == 0:
                designs = [a for a in payload["artifacts"].values()
                           if a["artifact_kind"] == "campaign_design"]
                if designs:
                    acts = [a["act_id"] for a in designs[0]["payload"]["design"]["acts"]]
                    earlier = acts[:acts.index(task.scope_keys[0])]
                    earlier_tasks = [t for t in payload["tasks"].values()
                                     if t["task"]["stage"] == "P04"
                                     and set(t["task"]["scope_keys"]) & set(earlier)]
                    if any(t["status"] != "accepted" for t in earlier_tasks):
                        raise PreparationError("Earlier act definitions are still being authored.")
                    dependencies = list(saved["task"]["dependency_artifact_ids"])
                    for act_id in earlier:
                        for artifact in payload["artifacts"].values():
                            if (artifact["artifact_kind"] in ("canonical_act", "act_authoring")
                                    and artifact["scope_keys"] == [act_id]
                                    and artifact["artifact_id"] not in dependencies):
                                dependencies.append(artifact["artifact_id"])
                    saved["task"]["dependency_artifact_ids"] = dependencies
                    for planned in payload["stage_plans"]["P04"]["tasks"]:
                        if planned["task_id"] == task.task_id:
                            planned["dependency_artifact_ids"] = dependencies
            active = sum(1 for item in payload["tasks"].values()
                         if item["status"] in ("leased", "running"))
            if active >= payload["binding"]["budget"]["max_parallel_tasks"]:
                raise PreparationError("Run concurrency limit is already reserved.")
            if (payload["provider_calls_spent"] + payload["provider_calls_reserved"] + calls > payload["binding"]["budget"]["max_provider_calls"]
                    or payload["tokens_spent"] + payload["tokens_reserved"] + tokens > payload["binding"]["budget"]["max_total_tokens"]
                    or payload["cost_microusd_spent"] + payload["cost_microusd_reserved"] + cost > payload["binding"]["budget"]["max_cost_microusd"]):
                log.warning("preparation_run_budget_estimate_exceeded", run_id=run_id,
                            task_id=task.task_id, provider_calls=payload["provider_calls_spent"],
                            tokens=payload["tokens_spent"], cost_microusd=payload["cost_microusd_spent"])
            now = _now()  # Start the lease only after acquiring semaphore and lock.
            saved.update(status="running", attempt_count=saved["attempt_count"] + 1,
                         lease_owner=worker_id, lease_fence=saved["lease_fence"] + 1,
                         lease_expires_at=_iso(now + timedelta(seconds=self.lease_seconds)),
                         reserved_provider_calls=calls, reserved_tokens=tokens,
                         reserved_cost_microusd=cost, updated_at=_iso(now))
            if not saved.get("provider_idempotency_key"):
                saved["provider_idempotency_key"] = canonical_sha256(
                    {"run_id": run_id, "task_id": task.task_id, "stage": task.stage.value,
                     "scope": list(task.scope_keys), "dependencies": saved["task"]["dependency_artifact_ids"]}
                )
            payload["provider_calls_reserved"] += calls
            payload["tokens_reserved"] += tokens
            payload["cost_microusd_reserved"] += cost
            payload["attempts_total"] += 1
            payload["status"] = "preparing"
        return self.store.mutate_run(slug, run_id, update=update)

    async def _validate_outputs(
        self, binding: PreparationRunBinding, task: PreparationTask,
        result: StageExecutionResult, dependencies: tuple[PreparationArtifact, ...],
    ) -> tuple[tuple[ValidationReceipt, ...], tuple[PreparationFinding, ...]]:
        proposed = result.proposed_artifacts
        expected_deps = _dependency_map(dependencies)
        if task.requires_provider and not result.usage.provider_called:
            raise PreparationTaskFailure("provider_usage_missing", result.usage)
        if not task.requires_provider and result.usage.provider_called:
            raise PreparationTaskFailure("unexpected_provider_call", result.usage)
        if result.usage.provider_called and (
            result.usage.call_count > task.max_provider_calls
            or result.usage.input_tokens > task.max_input_tokens
            or result.usage.output_tokens > task.max_output_tokens
            or result.usage.cost_microusd > task.max_cost_microusd
        ):
            log.warning("preparation_task_estimate_exceeded", task_id=task.task_id,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_microusd=result.usage.cost_microusd)
        scopes: set[str] = set()
        for artifact in proposed:
            assert_artifact_integrity(binding, artifact)
            if (artifact.stage != task.stage or artifact.task_id != task.task_id
                    or artifact.dependency_digests != expected_deps):
                raise PreparationTaskFailure("artifact_binding_invalid", result.usage)
            if not set(artifact.scope_keys) <= set(task.scope_keys):
                raise PreparationTaskFailure("artifact_scope_invalid", result.usage)
            scopes.update(artifact.scope_keys)
        if scopes != set(task.scope_keys):
            raise PreparationTaskFailure("artifact_scope_incomplete", result.usage)
        validators = self.validators.get(task.stage, ())
        if not validators:
            raise PreparationTaskFailure("validator_missing", result.usage)
        receipts: list[ValidationReceipt] = []
        findings: list[PreparationFinding] = []
        for validator in validators:
            validator_id = getattr(validator, "validator_id", "")
            if not validator_id:
                raise PreparationTaskFailure("validator_contract_invalid", result.usage)
            if inspect.iscoroutinefunction(validator):
                outcome = await validator(binding, task, proposed, dependencies)
            else:
                outcome = await asyncio.to_thread(validator, binding, task, proposed, dependencies)
            if not isinstance(outcome, ValidationResult) or not outcome.passed:
                raise PreparationTaskFailure(
                    outcome.block_code if isinstance(outcome, ValidationResult) and outcome.block_code else "validation_failed",
                    result.usage,
                    outcome.findings if isinstance(outcome, ValidationResult) else (),
                )
            matches = [receipt for receipt in outcome.receipts if receipt.validator_id == validator_id]
            if len(matches) != 1:
                raise PreparationTaskFailure("validation_receipt_missing", result.usage)
            receipt = matches[0]
            if (receipt.artifact_digests != _dependency_map(proposed)
                    or receipt.dependency_digests != expected_deps or not receipt.passed):
                raise PreparationTaskFailure("validation_receipt_binding_invalid", result.usage)
            receipts.append(receipt)
            findings.extend(outcome.findings)
        return tuple(receipts), tuple(findings)

    def _accept_task(
        self, slug: str, run_id: str, worker_id: str, task: PreparationTask, fence: int,
        result: StageExecutionResult, receipts: tuple[ValidationReceipt, ...],
        findings: tuple[PreparationFinding, ...],
        calls: int, tokens: int, cost: int,
    ) -> PreparationRunState:
        usage = result.usage
        now = _iso(_now())
        def update(payload: dict[str, Any]) -> None:
            saved = payload["tasks"][_task_key(task)]
            _assert_fence(saved, worker_id, fence)
            spent_calls, spent_tokens, spent_cost = _charged(
                usage, calls, tokens, cost, payload["usage_attempts"])
            payload["provider_calls_reserved"] -= calls
            payload["tokens_reserved"] -= tokens
            payload["cost_microusd_reserved"] -= cost
            payload["provider_calls_spent"] += spent_calls
            payload["tokens_spent"] += spent_tokens
            payload["cost_microusd_spent"] += spent_cost
            payload["usage_attempts"].append(usage.model_dump(mode="json"))
            _refresh_unresolved_usage(payload)
            saved.update(status="accepted", lease_owner=None, lease_expires_at=None,
                         reserved_provider_calls=0, reserved_tokens=0,
                         reserved_cost_microusd=0, accepted_artifact_ids=[a.artifact_id for a in result.proposed_artifacts],
                         validation_receipt_ids=[r.validator_id + ":" + r.details_sha256 for r in receipts],
                         last_safe_error_code=None, updated_at=now)
            payload["last_safe_error_ref"] = None
            current_findings = {finding["finding_id"]: finding for finding in payload["findings"]}
            for finding in findings:
                current_findings[finding.finding_id] = finding.model_dump(mode="json")
            payload["findings"] = list(current_findings.values())
            for artifact in result.proposed_artifacts:
                if artifact.artifact_id in payload["artifacts"]:
                    raise PreparationError("Artifact identity collision.")
                payload["artifacts"][artifact.artifact_id] = artifact.model_dump(mode="json")
            for receipt in receipts:
                key = receipt.validator_id + ":" + receipt.details_sha256
                payload["validation_receipts"][key] = receipt.model_dump(mode="json")
            stage_tasks = (item for item in payload["tasks"].values()
                           if item["task"]["stage"] == task.stage.value)
            if all(item["status"] == "accepted" for item in stage_tasks):
                payload["stage_statuses"][task.stage.value] = "accepted"
            payload["status"] = "reviewing" if task.stage == PreparationStage.P09 else "preparing"
        return self.store.mutate_run(slug, run_id, update=update)

    def _record_failure(
        self, slug: str, run_id: str, worker_id: str, task: PreparationTask, fence: int,
        code: str, usage: ProviderUsage | None, calls: int, tokens: int, cost: int,
        findings: tuple[PreparationFinding, ...] = (),
    ) -> PreparationRunState:
        now = _iso(_now())
        unknown_provider_outcome = task.requires_provider and (
            usage is None or (usage.provider_called and not usage.usage_known)
        )
        retryable = (not unknown_provider_outcome and usage is not None
                     and usage.provider_called and code in _AUTOMATIC_RETRY_CODES)
        error_ref = _new_support_ref()
        log.warning(
            "preparation_task_failed",
            run_id=run_id,
            stage=task.stage.value,
            task_id=task.task_id,
            failure_code=code,
            support_ref=error_ref,
            provider_called=bool(usage and usage.provider_called),
            usage_known=bool(usage and usage.usage_known),
            retryable=retryable,
        )
        def update(payload: dict[str, Any]) -> None:
            saved = payload["tasks"][_task_key(task)]
            _assert_fence(saved, worker_id, fence)
            attempt_usage = usage or ProviderUsage(
                provider_called=task.requires_provider,
                usage_known=False,
                call_count=1 if task.requires_provider else 0,
                task_version=f"{task.stage.value}.{task.task_kind}.attempt-{saved['attempt_count']}",
                failure_code=code,
            )
            if usage is not None and usage.failure_code is None:
                attempt_usage = usage.model_copy(update={"failure_code": code})
            attempt_usage = _with_unresolved_estimates(
                attempt_usage, calls, tokens, cost,
                canonical_sha256({"run": run_id, "task": task.task_id,
                                  "attempt": saved["attempt_count"]}),
            )
            used_calls, used_tokens, used_cost = _charged(
                attempt_usage, calls, tokens, cost, payload["usage_attempts"])
            payload["provider_calls_reserved"] -= calls
            payload["tokens_reserved"] -= tokens
            payload["cost_microusd_reserved"] -= cost
            payload["provider_calls_spent"] += used_calls
            payload["tokens_spent"] += used_tokens
            payload["cost_microusd_spent"] += used_cost
            payload["usage_attempts"].append(attempt_usage.model_dump(mode="json"))
            _refresh_unresolved_usage(payload)
            current_findings = {finding["finding_id"]: finding for finding in payload["findings"]}
            for finding in findings:
                current_findings[finding.finding_id] = finding.model_dump(mode="json")
            payload["findings"] = list(current_findings.values())
            saved.update(status="failed" if retryable else "blocked",
                         lease_owner=None, lease_expires_at=None, reserved_provider_calls=0,
                         reserved_tokens=0, reserved_cost_microusd=0,
                         last_safe_error_code=code, updated_at=now)
            payload["status"] = "failed" if saved["status"] == "failed" else "blocked"
            payload["last_safe_error_code"] = code
            payload["last_safe_error_ref"] = error_ref
        try:
            return self.store.mutate_run(slug, run_id, update=update)
        except PreparationError:
            return self.store.load_run(slug, run_id)

    def _block_task(self, slug: str, run_id: str, task: PreparationTask, code: str) -> PreparationRunState:
        error_ref = _new_support_ref()
        def update(payload: dict[str, Any]) -> None:
            saved = payload["tasks"][_task_key(task)]
            saved.update(status="blocked", last_safe_error_code=code, updated_at=_iso(_now()))
            payload["status"] = "blocked"
            payload["last_safe_error_code"] = code
            payload["last_safe_error_ref"] = error_ref
        return self.store.mutate_run(slug, run_id, update=update)

    def _block_run(self, slug: str, run_id: str, code: str) -> PreparationRunState:
        error_ref = _new_support_ref()
        def update(payload: dict[str, Any]) -> None:
            if payload["status"] in ("ready", "cancelled"):
                return
            payload["status"] = "blocked"
            payload["stage_statuses"][payload["current_stage"] or "P01"] = "blocked"
            payload["last_safe_error_code"] = code
            payload["last_safe_error_ref"] = error_ref
        return self.store.mutate_run(slug, run_id, update=update)

    def _finish_stage(
        self, slug: str, run_id: str, state: PreparationRunState, stage: PreparationStage
    ) -> PreparationRunState:
        if stage == PreparationStage.P12 and state.status == PreparationJobStatus.PUBLISHING:
            return state
        def update(payload: dict[str, Any]) -> None:
            if payload["cancel_requested"]:
                raise PreparationError("Run was cancelled.")
            payload["stage_statuses"][stage.value] = "accepted"
            if stage == PreparationStage.P12:
                payload["status"] = "publishing"
            else:
                next_stage = PreparationStage(f"P{stage.ordinal + 1:02d}")
                payload["current_stage"] = next_stage.value
                payload["status"] = "preparing"
        return self.store.mutate_run(slug, run_id, update=update)

    async def _publish(
        self, slug: str, run_id: str, source_guard: SourceGuard,
        publisher: Publisher | None, state: PreparationRunState,
    ) -> PreparationStep:
        if publisher is None:
            return PreparationStep(self._block_run(slug, run_id, "publication_callback_missing"))
        try:
            self._check_ready_inventory(state)
            publication = build_publication(
                state.binding,
                artifacts=tuple(state.artifacts.values()),
                validation_receipts=tuple(state.validation_receipts.values()),
                findings=state.findings,
            )
            self._check_live(self.store.load_run(slug, run_id), source_guard)
            digest = await _call(publisher, publication)
            if digest != publication.bundle_sha256:
                raise PreparationError("Publisher did not confirm the exact bundle digest.")
            current = self.store.load_run(slug, run_id)
            self._check_live(current, source_guard)
            if current.revision != state.revision:
                raise PreparationError("Run changed while publication was in progress.")
            def ready(payload: dict[str, Any]) -> None:
                payload["publication_inventory"] = publication.artifact_digests
                payload["published_bundle_sha256"] = publication.bundle_sha256
                payload["status"] = "ready"
            ready_state = self.store.update_run(
                slug, run_id, expected_revision=state.revision, update=ready
            )
            return PreparationStep(ready_state, publication_digest=digest)
        except StalePreparationRevision:
            return PreparationStep(self.store.load_run(slug, run_id))
        except Exception as exc:
            from .preparation_proposals import save_diagnostic

            save_diagnostic(state.binding, "publication", exc,
                            dependencies=tuple(state.artifacts.values()))
            current = self.store.load_run(slug, run_id)
            if current.revision != state.revision or current.status in (
                PreparationJobStatus.READY, PreparationJobStatus.CANCELLED
            ):
                return PreparationStep(current)
            return PreparationStep(self._block_run(slug, run_id, "publication_failed"))

    def _check_ready_inventory(self, state: PreparationRunState) -> None:
        if set(state.stage_statuses) != {stage.value for stage in PreparationStage} or any(
            state.stage_statuses.get(stage.value) != "accepted" for stage in PreparationStage
        ):
            raise PreparationError("All twelve exact stages must be accepted before publication.")
        expected_artifacts: set[str] = set()
        for stage in PreparationStage:
            plan = state.stage_plans.get(stage.value)
            if plan is None:
                raise PreparationError("A stage plan is missing.")
            expected_artifacts.update(
                artifact_id for task in plan.tasks
                for artifact_id in state.tasks[_task_key(task)].accepted_artifact_ids
            )
        if expected_artifacts != set(state.artifacts):
            raise PreparationError("P12 artifact inventory does not exactly match accepted stage work.")
        for artifact in state.artifacts.values():
            assert_artifact_integrity(state.binding, artifact)
        if not state.validation_receipts:
            raise PreparationError("Validated receipts are required for publication.")
        if any(not receipt.passed for receipt in state.validation_receipts.values()):
            raise PreparationError("Failed receipts cannot be published.")
        if any(f.status == "open" and f.severity in ("medium", "high", "critical") for f in state.findings):
            raise PreparationError("Unresolved critical findings block publication.")

    @staticmethod
    def _check_live(state: PreparationRunState, source_guard: SourceGuard) -> None:
        state.binding.assert_integrity()
        if state.cancel_requested or state.status == PreparationJobStatus.CANCELLED:
            raise PreparationError("Preparation run was cancelled.")
        guard_result = source_guard(state.binding)
        if guard_result is False:
            raise PreparationError("Source binding is no longer current.")

    @staticmethod
    def _has_retryable_work(state: PreparationRunState) -> bool:
        return any(task.status in (PreparationTaskStatus.FAILED, PreparationTaskStatus.BLOCKED)
                   for task in state.tasks.values())


def _assert_fence(task_state: dict[str, Any], owner: str, fence: int) -> None:
    if task_state["status"] != "running" or task_state["lease_owner"] != owner or task_state["lease_fence"] != fence:
        raise PreparationError("Task lease was superseded before its result could be committed.")


def _charged(usage: ProviderUsage | None, calls: int, tokens: int, cost: int,
             previous=()) -> tuple[int, int, int]:
    """Count known usage once; reservations and unknown estimates never become spent."""
    if usage is None:
        return 0, 0, 0
    seen = {r["response_id"] for item in previous for r in item.get("responses", [])}
    repeated = [r for r in usage.responses if r.response_id in seen]
    if usage.usage_known:
        known_calls = usage.call_count
    elif usage.unresolved_calls:
        known_calls = usage.call_count - sum(call.call_count for call in usage.unresolved_calls)
    else:
        # Legacy aggregates retain known responses despite an unknown later request.
        known_calls = len(usage.responses)
    return (max(0, known_calls - len(repeated)),
            max(0, usage.input_tokens + usage.output_tokens
                - sum(r.input_tokens + r.output_tokens for r in repeated)),
            max(0, usage.cost_microusd - sum(r.cost_microusd for r in repeated)))


def _with_unresolved_estimates(usage, calls, tokens, cost, dispatch_id):
    if not usage.provider_called or usage.usage_known or usage.unresolved_calls:
        return usage
    return usage.model_copy(update={"unresolved_calls": (UnresolvedProviderCall(
        dispatch_id=dispatch_id, call_count=max(1, usage.call_count - len(usage.responses)),
        estimated_tokens=tokens, estimated_cost_microusd=cost,
    ),)})


def _refresh_unresolved_usage(payload):
    """Project unique pending calls; a later exact receipt resolves its old estimate."""
    responses = {r["response_id"]: r for item in payload["usage_attempts"]
                 for r in item.get("responses", [])}
    dispatches = {r.get("dispatch_id") for r in responses.values() if r.get("dispatch_id")}
    pending = {}
    for index, usage in enumerate(payload["usage_attempts"]):
        for call in usage.get("unresolved_calls", []):
            if call["dispatch_id"] not in dispatches and call.get("response_id") not in responses:
                pending[call["dispatch_id"]] = call
        if usage["provider_called"] and not usage["usage_known"] and not usage.get("unresolved_calls"):
            # Legacy unknown outcomes have no trustworthy per-call estimate. Keep
            # that uncertainty visible until receipt reconciliation supplies it.
            pending[f"legacy-{index}"] = {"call_count": max(1, usage["call_count"] - len(usage.get("responses", [])))}
    payload["provider_calls_unresolved"] = sum(call["call_count"] for call in pending.values())
    payload["tokens_unresolved_estimate"] = sum(call.get("estimated_tokens", 0) for call in pending.values())
    payload["cost_microusd_unresolved_estimate"] = sum(call.get("estimated_cost_microusd", 0) for call in pending.values())
    payload["unresolved_estimates_complete"] = all(
        "estimated_tokens" in call and "estimated_cost_microusd" in call for call in pending.values()
    )


async def _call(function: Callable[..., Any], *args: Any) -> Any:
    result = function(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _exception_code(exc: Exception) -> str:
    """Keep a safe class-only failure identifier in the private run record."""
    code = re.sub(r"[^a-z0-9_-]", "_", type(exc).__name__.lower())[:96]
    return code if code and code[0].isalpha() else "task_execution_failed"


def _new_support_ref() -> str:
    return f"err-{secrets.token_hex(8)}"


def _safe_preparation_failure(
    code: str | None, support_ref: str | None, run_id: str
) -> SafePreparationFailure | None:
    if not isinstance(code, str):
        return None
    if not isinstance(support_ref, str) or not re.fullmatch(r"err-[0-9a-f]{16}", support_ref):
        # Stable compatibility projection for runs created before references were persisted.
        support_ref = "err-" + hashlib.sha256(f"{run_id}:{code}".encode()).hexdigest()[:16]
    failure = classify_failure(code, outcome=OutcomeState.UNKNOWN, support_ref=support_ref)
    return SafePreparationFailure(
        code=failure.code,
        category=failure.category.value,
        message=failure.message,
        recovery_action=failure.recovery_action.value,
        retryable=failure.retryable,
        support_ref=failure.support_ref,
        outcome=failure.outcome.value,
    )
