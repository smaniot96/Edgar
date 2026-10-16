"""Offline concurrency/restart/publication checks over real atomic checkpoints."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from test_preparation_supervisor import binding

from apps.api.services import adventures_meta
from apps.api.services.preparation import PreparationError, PreparationSupervisor, StageAdapter
from apps.api.services.preparation_store import PreparationStore, PreparationStoreError
from edgar_core.preparation import (
    PreparationFinding,
    PreparationStage,
    PreparationTask,
    ProviderUsage,
    StageExecutionResult,
    StagePlan,
    ValidationResult,
    build_artifact,
    build_validation_receipt,
)


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    return PreparationStore()


def harness(store, *, execute_hook=None, findings=()):
    def plan(bound, accepted):
        stage = PreparationStage(f"P{len({a.stage for a in accepted}) + 1:02d}")
        scope = ("act1",) if stage in (PreparationStage.P05, PreparationStage.P06, PreparationStage.P08) else ()
        task = PreparationTask(
            task_id="unit", stage=stage, task_kind="offline",
            scope_keys=scope, dependency_artifact_ids=tuple(a.artifact_id for a in accepted),
            max_input_tokens=1, max_output_tokens=1, max_cost_microusd=0,
            requires_provider=False,
        )
        return StagePlan(stage=stage, tasks=(task,), required_scope_keys=scope)

    async def execute(bound, task, deps, attempt, key):
        if execute_hook:
            await execute_hook(bound, task, deps, attempt, key)
        artifact = build_artifact(
            bound, stage=task.stage, task_id=task.task_id, artifact_kind="offline",
            payload={"scope_contract": {"act_ids": ["act1"]}} if task.stage == PreparationStage.P03 else {"value": task.stage.value},
            dependency_artifacts=deps, created_at=datetime.now(UTC).isoformat(), scope_keys=task.scope_keys,
        )
        return StageExecutionResult(proposed_artifacts=(artifact,), usage=ProviderUsage(
            provider_called=False, task_version="offline"))

    def validator(name):
        def validate(bound, task, artifacts, dependencies):
            return ValidationResult(passed=True, receipts=(build_validation_receipt(
                name, artifacts=artifacts, dependencies=dependencies, passed=True,
                details={"stage": task.stage.value}, created_at=datetime.now(UTC).isoformat(),
            ),), findings=findings if task.stage == PreparationStage.P09 else ())
        validate.validator_id = name
        return validate

    validators = {stage: (validator(f"{stage.value.lower()}_code"),) for stage in PreparationStage}
    validators[PreparationStage.P04] = (validator("p04_prepared_campaign"),)
    validators[PreparationStage.P09] = (validator("p09_deterministic_review"), validator("p09_independent_critic"))
    validators[PreparationStage.P12] = (validator("p12_publication_inventory"),)
    return PreparationSupervisor(adapters={stage: StageAdapter(plan=plan, execute=execute) for stage in PreparationStage}, validators=validators, store=store)


async def finish(supervisor, *, publisher=None):
    run = binding()
    for _ in range(40):
        result = await supervisor.step("mystery", run.run_id, "worker", lambda _: True,
                                       publisher or (lambda p: p.bundle_sha256))
        if result.state.status.value in ("ready", "blocked", "cancelled"):
            return result.state
    raise AssertionError("bounded offline run did not terminate")


@pytest.mark.asyncio
async def test_restart_retains_accepted_artifacts_and_publishes_exact_inventory(private_store):
    first = harness(private_store)
    first.create_run(binding())
    await first.step("mystery", binding().run_id, "worker", lambda _: True)
    before = private_store.load_run("mystery", binding().run_id)
    restarted = harness(private_store)
    final = await finish(restarted)
    assert final.status.value == "ready", final.last_safe_error_code
    assert set(before.artifacts) <= set(final.artifacts)
    assert final.publication_inventory == {k: a.content_sha256 for k, a in final.artifacts.items()}
    assert len(final.artifacts) == 12
    assert final.provider_calls_spent == 0


@pytest.mark.asyncio
async def test_ready_checkpoint_and_bound_saves_cannot_be_invalidated(private_store):
    supervisor = harness(private_store)
    supervisor.create_run(binding())
    ready = await finish(supervisor)
    with pytest.raises(PreparationError, match="immutable"):
        supervisor.invalidate_dependents("mystery", binding().run_id, (next(iter(ready.artifacts)),))
    with pytest.raises(PreparationStoreError, match="immutable"):
        private_store.mutate_run("mystery", binding().run_id, update=lambda p: p.update(status="queued"))
    assert private_store.load_run("mystery", binding().run_id) == ready


@pytest.mark.asyncio
async def test_medium_contradiction_blocks_publication_after_review(private_store):
    finding = PreparationFinding(finding_id="contradiction", code="contradiction", severity="medium", message="Private contradiction")
    supervisor = harness(private_store, findings=(finding,))
    supervisor.create_run(binding())
    called = []
    state = await finish(supervisor, publisher=lambda p: called.append(p) or p.bundle_sha256)
    assert state.status.value == "blocked"
    assert not called
    assert state.stage_statuses["P09"] == "accepted"


@pytest.mark.asyncio
async def test_cancel_during_provider_work_fences_late_result(private_store):
    started, release = asyncio.Event(), asyncio.Event()
    async def wait(*_):
        started.set()
        await release.wait()
    supervisor = harness(private_store, execute_hook=wait)
    supervisor.create_run(binding())
    work = asyncio.create_task(supervisor.step("mystery", binding().run_id, "old", lambda _: True))
    await started.wait()
    cancelled = supervisor.cancel("mystery", binding().run_id)
    release.set()
    late = await work
    assert late.state.status.value == "cancelled"
    assert not late.state.artifacts
    assert private_store.load_run("mystery", binding().run_id).revision == cancelled.revision


@pytest.mark.asyncio
async def test_duplicate_dispatch_commits_one_task_and_one_usage(private_store):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def wait(*_):
        calls.append(1)
        started.set()
        await release.wait()
    supervisor = harness(private_store, execute_hook=wait)
    supervisor.create_run(binding())
    first = asyncio.create_task(supervisor.step("mystery", binding().run_id, "first", lambda _: True))
    await started.wait()
    duplicate = await supervisor.step("mystery", binding().run_id, "second", lambda _: True)
    assert duplicate.state.tasks["P01:unit"].status.value == "running"
    release.set()
    await first
    state = private_store.load_run("mystery", binding().run_id)
    assert len(state.artifacts) == len(state.usage_attempts) == len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_during_publication_cannot_be_overwritten_ready(private_store):
    supervisor = harness(private_store)
    supervisor.create_run(binding())
    async def publish(publication):
        supervisor.cancel("mystery", binding().run_id)
        return publication.bundle_sha256
    final = await finish(supervisor, publisher=publish)
    assert final.status.value == "cancelled"
    assert final.published_bundle_sha256 is None


@pytest.mark.asyncio
async def test_dependency_invalidation_removes_reviews_and_retains_spend(private_store):
    supervisor = harness(private_store)
    supervisor.create_run(binding())
    for _ in range(8):
        await supervisor.step("mystery", binding().run_id, "worker", lambda _: True)
    before = private_store.load_run("mystery", binding().run_id)
    root = next(a for a in before.artifacts.values() if a.stage == PreparationStage.P03)
    invalidated = supervisor.invalidate_dependents("mystery", binding().run_id, (root.artifact_id,))
    assert invalidated.current_stage == PreparationStage.P03
    assert {a.stage for a in invalidated.artifacts.values()} == {PreparationStage.P01, PreparationStage.P02}
    assert invalidated.cost_microusd_spent == before.cost_microusd_spent
    final = await finish(harness(private_store))
    assert final.status.value == "ready"


@pytest.mark.asyncio
async def test_expired_worker_cannot_overwrite_replacement_lease(private_store):
    started, release = asyncio.Event(), asyncio.Event()
    async def wait(bound, task, deps, attempt, key):
        if attempt == 1:
            started.set()
            await release.wait()
    supervisor = harness(private_store, execute_hook=wait)
    supervisor.create_run(binding())
    old = asyncio.create_task(supervisor.step("mystery", binding().run_id, "old", lambda _: True))
    await started.wait()
    private_store.mutate_run("mystery", binding().run_id, update=lambda p: p["tasks"]["P01:unit"].update(lease_expires_at=(datetime.now(UTC) - timedelta(seconds=10)).isoformat()))
    replacement = await supervisor.step("mystery", binding().run_id, "new", lambda _: True)
    release.set()
    await old
    final = private_store.load_run("mystery", binding().run_id)
    assert final == replacement.state
    assert final.tasks["P01:unit"].attempt_count == 2
    assert len(final.artifacts) == 1
