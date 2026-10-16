from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.services import adventures_meta
from apps.api.services.preparation import (
    PreparationSupervisor,
    PreparationTaskFailure,
    StageAdapter,
)
from apps.api.services.preparation_store import PreparationStore
from edgar_core.preparation import (
    PreparationBudget,
    PreparationRunBinding,
    PreparationStage,
    PreparationTask,
    ProviderUsage,
    StageExecutionResult,
    StagePlan,
    ValidationReceipt,
    ValidationResult,
    build_artifact,
    canonical_sha256,
)


@pytest.fixture
def private_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PreparationStore:
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    return PreparationStore()


def binding() -> PreparationRunBinding:
    commission = {"tone": "mystery"}
    heroes = {"members": ["hero-1"]}
    profile = {"profiles": {"preparation.p01": {"model": "gpt-6.1-sol"}}}
    return PreparationRunBinding(
        run_id="prep-" + "1" * 32,
        source_mode="ai",
        source_slug="mystery",
        source_version="v1",
        rules_edition="2014",
        commission=commission,
        commission_sha256=canonical_sha256(commission),
        hero_party_snapshot=heroes,
        hero_party_sha256=canonical_sha256(heroes),
        profile_snapshot=profile,
        profile_snapshot_sha256=canonical_sha256(profile),
        budget=PreparationBudget(
            max_provider_calls=12,
            max_total_tokens=1000,
            max_cost_microusd=100,
            max_attempts_per_task=2,
        ),
    )


@pytest.mark.asyncio
async def test_supervisor_reserves_usage_and_accepts_bound_receipt(private_store: PreparationStore) -> None:
    run = binding()
    task = PreparationTask(
        task_id="brief",
        stage=PreparationStage.P01,
        task_kind="commission",
        scope_keys=("brief",),
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_microusd=10,
    )
    plan = StagePlan(stage=PreparationStage.P01, tasks=(task,), required_scope_keys=("brief",))
    supervisor: PreparationSupervisor

    def execute(bound, planned, dependencies, attempt, idempotency_key):
        assert bound.run_id == run.run_id
        assert planned == task
        assert dependencies == ()
        assert attempt == 1
        assert idempotency_key
        artifact = build_artifact(
            bound,
            stage=planned.stage,
            task_id=planned.task_id,
            artifact_kind="commission_record",
            payload={"ready": True},
            dependency_artifacts=(),
            created_at="2026-10-16T00:00:00+00:00",
            scope_keys=planned.scope_keys,
        )
        usage = ProviderUsage(
            provider_called=True,
            call_count=1,
            input_tokens=17,
            output_tokens=23,
            cost_microusd=2,
            requested_model="gpt-6.1-sol",
            reasoning_effort="high",
            resolved_model="gpt-6.1-sol",
            task_version="1",
            latency_ms=10,
        )
        return StageExecutionResult(proposed_artifacts=(artifact,), usage=usage)

    def validate(bound, planned, artifacts, dependencies):
        receipt = ValidationReceipt(
            validator_id="p01_code_check",
            passed=True,
            artifact_digests={a.artifact_id: a.content_sha256 for a in artifacts},
            dependency_digests={},
            details_sha256=canonical_sha256({"valid": True}),
            created_at="2026-10-16T00:00:01+00:00",
        )
        return ValidationResult(passed=True, receipts=(receipt,))

    validate.validator_id = "p01_code_check"
    adapter = StageAdapter(plan=lambda _binding, _artifacts: plan, execute=execute)
    supervisor = PreparationSupervisor(
        adapters={PreparationStage.P01: adapter},
        validators={PreparationStage.P01: (validate,)},
        store=private_store,
    )
    supervisor.create_run(run)

    result = await supervisor.step("mystery", run.run_id, "worker-1", lambda _binding: True)

    assert result.state.tasks["P01:brief"].status == "accepted", result.state.last_safe_error_code
    assert result.state.provider_calls_spent == 1
    assert result.state.tokens_spent == 40
    assert result.state.cost_microusd_spent == 2
    assert result.state.provider_calls_reserved == 0
    assert len(result.state.validation_receipts) == 1


@pytest.mark.asyncio
async def test_missing_stage_validator_blocks_before_worker_dispatch(private_store: PreparationStore) -> None:
    run = binding()
    task = PreparationTask(
        task_id="brief",
        stage=PreparationStage.P01,
        task_kind="commission",
        max_input_tokens=1,
        max_output_tokens=1,
        max_cost_microusd=0,
    )
    dispatched = False

    def execute(*_args):
        nonlocal dispatched
        dispatched = True
        raise AssertionError("must not dispatch without a validator")

    supervisor = PreparationSupervisor(
        adapters={PreparationStage.P01: StageAdapter(
            plan=lambda _binding, _artifacts: StagePlan(stage=PreparationStage.P01, tasks=(task,)),
            execute=execute,
        )},
        validators={},
        store=private_store,
    )
    supervisor.create_run(run)

    result = await supervisor.step("mystery", run.run_id, "worker-1", lambda _binding: True)

    assert result.state.status.value == "blocked"
    assert result.state.last_safe_error_code == "stage_contract_missing"
    assert not dispatched


@pytest.mark.asyncio
async def test_unknown_failed_provider_usage_charges_the_full_reservation(
    private_store: PreparationStore,
) -> None:
    run = binding()
    task = PreparationTask(
        task_id="brief",
        stage=PreparationStage.P01,
        task_kind="commission",
        max_input_tokens=100,
        max_output_tokens=100,
        max_cost_microusd=10,
    )

    def execute(*_args):
        raise PreparationTaskFailure("network_timeout")

    def validate(*_args):
        raise AssertionError("validation cannot run after execution failure")

    validate.validator_id = "p01_code_check"
    supervisor = PreparationSupervisor(
        adapters={PreparationStage.P01: StageAdapter(
            plan=lambda _binding, _artifacts: StagePlan(stage=PreparationStage.P01, tasks=(task,)),
            execute=execute,
        )},
        validators={PreparationStage.P01: (validate,)},
        store=private_store,
    )
    supervisor.create_run(run)

    result = await supervisor.step("mystery", run.run_id, "worker-1", lambda _binding: True)

    assert result.state.tasks["P01:brief"].status.value == "failed"
    assert result.state.provider_calls_spent == 1
    assert result.state.tokens_spent == 200
    assert result.state.cost_microusd_spent == 10
    assert result.state.usage_attempts[0].usage_known is False
