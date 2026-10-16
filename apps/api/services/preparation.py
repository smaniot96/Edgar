"""Fail-closed, durable supervisor for private campaign preparation runs.

Stage workers propose artifacts only. This module owns leases, budget reservations, exact
binding checks, validator receipts, retries, cancellation and the final publication gate.
Provider execution and publication callbacks always run outside the lifecycle file lock.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

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
    StageExecutionResult,
    StagePlan,
    ValidationReceipt,
    ValidationResult,
    assert_artifact_integrity,
    build_publication,
    canonical_sha256,
    safe_public_stage,
)

from .preparation_store import PreparationStore, StalePreparationRevision


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
                        payload["usage_attempts"].append(ProviderUsage(
                            provider_called=called,
                            usage_known=False,
                            call_count=max(item["reserved_provider_calls"], 1) if called else 0,
                            task_version=(
                                f"{task['stage']}.{task['task_kind']}.attempt-{item['attempt_count']}"
                            ),
                            failure_code="cancelled",
                        ).model_dump(mode="json"))
                    item["status"] = "cancelled"
                    item["lease_owner"] = None
                    item["lease_expires_at"] = None
                    item["lease_fence"] += 1
            # Any outstanding reservation is uncertain and is conservatively charged.
            payload["provider_calls_spent"] += payload["provider_calls_reserved"]
            payload["tokens_spent"] += payload["tokens_reserved"]
            payload["cost_microusd_spent"] += payload["cost_microusd_reserved"]
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
                if item["status"] in ("failed", "blocked") and item["attempt_count"] < payload["binding"]["budget"]["max_attempts_per_task"]:
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

        return self.store.mutate_run(slug, run_id, update=update)

    def _retry_failed_tasks(
        self, slug: str, run_id: str, state: PreparationRunState
    ) -> PreparationRunState:
        """Resume retryable provider failures without losing their durable attempt record."""
        retryable = [task for task in state.tasks.values()
                     if task.status == PreparationTaskStatus.FAILED
                     and task.attempt_count < state.binding.budget.max_attempts_per_task]
        if not retryable:
            return state

        def update(payload: dict[str, Any]) -> None:
            for task in payload["tasks"].values():
                if (task["status"] == "failed"
                        and task["attempt_count"] < payload["binding"]["budget"]["max_attempts_per_task"]):
                    task["status"] = "pending"
            payload["status"] = "preparing"
            payload["last_safe_error_code"] = None
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
            payload["publication_inventory"] = None
            payload["published_bundle_sha256"] = None

        return self.store.mutate_run(slug, run_id, update=update)

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
        except Exception:
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
                payload["provider_calls_spent"] += calls
                payload["tokens_spent"] += tokens
                payload["cost_microusd_spent"] += cost
                task_spec = task["task"]
                called = task_spec["requires_provider"]
                payload["usage_attempts"].append(ProviderUsage(
                    provider_called=called,
                    usage_known=False,
                    call_count=max(calls, 1) if called else 0,
                    task_version=(
                        f"{task_spec['stage']}.{task_spec['task_kind']}.attempt-{task['attempt_count']}"
                    ),
                    failure_code="lease_expired",
                ).model_dump(mode="json"))
                task.update(status="pending", lease_owner=None, lease_expires_at=None,
                            reserved_provider_calls=0, reserved_tokens=0,
                            reserved_cost_microusd=0, last_safe_error_code="lease_expired",
                            lease_fence=task["lease_fence"] + 1)
            payload["status"] = "preparing"
        return self.store.mutate_run(slug, run_id, update=recover)

    async def _execute_task(
        self, slug: str, run_id: str, worker_id: str, source_guard: SourceGuard,
        state: PreparationRunState, task_state: PreparationTaskState,
    ) -> PreparationStep:
        task = task_state.task
        if task_state.attempt_count >= state.binding.budget.max_attempts_per_task:
            return PreparationStep(self._block_task(slug, run_id, task, "attempt_budget_exhausted"))
        self._check_live(state, source_guard)
        reserved_calls = task.max_provider_calls if task.requires_provider else 0
        reserved_tokens = task.max_input_tokens + task.max_output_tokens if task.requires_provider else 0
        reserved_cost = task.max_cost_microusd if task.requires_provider else 0
        available = state.binding.budget
        if (state.provider_calls_spent + state.provider_calls_reserved + reserved_calls > available.max_provider_calls
                or state.tokens_spent + state.tokens_reserved + reserved_tokens > available.max_total_tokens
                or state.cost_microusd_spent + state.cost_microusd_reserved + reserved_cost > available.max_cost_microusd):
            return PreparationStep(self._block_task(slug, run_id, task, "run_budget_exhausted"))
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
            deps = tuple(state.artifacts[artifact_id] for artifact_id in task.dependency_artifact_ids)
            adapter = self.adapters[task.stage]
            idem_key = task_state.provider_idempotency_key or canonical_sha256(
                {"run_id": run_id, "task_id": task.task_id, "stage": task.stage.value,
                 "scope": task.scope_keys, "dependencies": _dependency_map(deps)}
            )
            try:
                self._check_live(state, source_guard)
                result = await _call(adapter.execute, state.binding, task, deps,
                                     task_state.attempt_count, idem_key)
                if not isinstance(result, StageExecutionResult):
                    raise PreparationTaskFailure("worker_result_invalid")
                self._check_live(self.store.load_run(slug, run_id), source_guard)
                receipts, findings = await self._validate_outputs(state.binding, task, result, deps)
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
            active = sum(1 for item in payload["tasks"].values()
                         if item["status"] in ("leased", "running"))
            if active >= payload["binding"]["budget"]["max_parallel_tasks"]:
                raise PreparationError("Run concurrency limit is already reserved.")
            if (payload["provider_calls_spent"] + payload["provider_calls_reserved"] + calls > payload["binding"]["budget"]["max_provider_calls"]
                    or payload["tokens_spent"] + payload["tokens_reserved"] + tokens > payload["binding"]["budget"]["max_total_tokens"]
                    or payload["cost_microusd_spent"] + payload["cost_microusd_reserved"] + cost > payload["binding"]["budget"]["max_cost_microusd"]):
                raise PreparationError("Run budget was reserved by another worker.")
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
            raise PreparationTaskFailure("task_reservation_exceeded", result.usage)
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
            outcome = await _call(validator, binding, task, proposed, dependencies)
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
        spent_calls, spent_tokens, spent_cost = _charged(usage, calls, tokens, cost)
        now = _iso(_now())
        def update(payload: dict[str, Any]) -> None:
            saved = payload["tasks"][_task_key(task)]
            _assert_fence(saved, worker_id, fence)
            payload["provider_calls_reserved"] -= calls
            payload["tokens_reserved"] -= tokens
            payload["cost_microusd_reserved"] -= cost
            payload["provider_calls_spent"] += spent_calls
            payload["tokens_spent"] += spent_tokens
            payload["cost_microusd_spent"] += spent_cost
            payload["usage_attempts"].append(usage.model_dump(mode="json"))
            saved.update(status="accepted", lease_owner=None, lease_expires_at=None,
                         reserved_provider_calls=0, reserved_tokens=0,
                         reserved_cost_microusd=0, accepted_artifact_ids=[a.artifact_id for a in result.proposed_artifacts],
                         validation_receipt_ids=[r.validator_id + ":" + r.details_sha256 for r in receipts],
                         last_safe_error_code=None, updated_at=now)
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
        retryable = usage is None or usage.failure_code is not None
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
            used_calls, used_tokens, used_cost = _charged(attempt_usage, calls, tokens, cost)
            payload["provider_calls_reserved"] -= calls
            payload["tokens_reserved"] -= tokens
            payload["cost_microusd_reserved"] -= cost
            payload["provider_calls_spent"] += used_calls
            payload["tokens_spent"] += used_tokens
            payload["cost_microusd_spent"] += used_cost
            payload["usage_attempts"].append(attempt_usage.model_dump(mode="json"))
            current_findings = {finding["finding_id"]: finding for finding in payload["findings"]}
            for finding in findings:
                current_findings[finding.finding_id] = finding.model_dump(mode="json")
            payload["findings"] = list(current_findings.values())
            exhausted = saved["attempt_count"] >= payload["binding"]["budget"]["max_attempts_per_task"]
            saved.update(status="failed" if retryable and not exhausted else "blocked",
                         lease_owner=None, lease_expires_at=None, reserved_provider_calls=0,
                         reserved_tokens=0, reserved_cost_microusd=0,
                         last_safe_error_code=code, updated_at=now)
            payload["status"] = "failed" if saved["status"] == "failed" else "blocked"
            payload["last_safe_error_code"] = code
        try:
            return self.store.mutate_run(slug, run_id, update=update)
        except PreparationError:
            return self.store.load_run(slug, run_id)

    def _block_task(self, slug: str, run_id: str, task: PreparationTask, code: str) -> PreparationRunState:
        def update(payload: dict[str, Any]) -> None:
            saved = payload["tasks"][_task_key(task)]
            saved.update(status="blocked", last_safe_error_code=code, updated_at=_iso(_now()))
            payload["status"] = "blocked"
            payload["last_safe_error_code"] = code
        return self.store.mutate_run(slug, run_id, update=update)

    def _block_run(self, slug: str, run_id: str, code: str) -> PreparationRunState:
        def update(payload: dict[str, Any]) -> None:
            if payload["status"] in ("ready", "cancelled"):
                return
            payload["status"] = "blocked"
            payload["stage_statuses"][payload["current_stage"] or "P01"] = "blocked"
            payload["last_safe_error_code"] = code
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
        except Exception:
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
                   and task.attempt_count < state.binding.budget.max_attempts_per_task
                   for task in state.tasks.values())


def _assert_fence(task_state: dict[str, Any], owner: str, fence: int) -> None:
    if task_state["status"] != "running" or task_state["lease_owner"] != owner or task_state["lease_fence"] != fence:
        raise PreparationError("Task lease was superseded before its result could be committed.")


def _charged(usage: ProviderUsage | None, calls: int, tokens: int, cost: int) -> tuple[int, int, int]:
    if usage is None or (usage.provider_called and not usage.usage_known):
        return calls, tokens, cost
    return usage.call_count, usage.input_tokens + usage.output_tokens, usage.cost_microusd


async def _call(function: Callable[..., Any], *args: Any) -> Any:
    result = function(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _exception_code(exc: Exception) -> str:
    """Keep a safe class-only failure identifier in the private run record."""
    code = re.sub(r"[^a-z0-9_-]", "_", type(exc).__name__.lower())[:96]
    return code if code and code[0].isalpha() else "task_execution_failed"
