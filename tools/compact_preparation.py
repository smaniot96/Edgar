"""Recover a retained PDF design through the campaign pipeline, without paid study redo.

Run inside Edgar's configured environment. The original checkpoint, proposals and
source remain immutable; imports receive fresh bindings and local validation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from apps.api.services import adventures_meta as meta_store
from apps.api.services.preparation_sources import load_source, source_directory
from apps.api.services.preparation_stages import _validator
from apps.api.services.preparation_store import PreparationStore, _atomic_write, preparation_path
from edgar_core.preparation import (
    PreparationRunBinding,
    PreparationStage,
    PreparationTaskState,
    PreparationTaskStatus,
    StagePlan,
    build_artifact,
    canonical_sha256,
)


def import_saved_design(slug, original_run_id):
    store = PreparationStore()
    original = store.load_run(slug, original_run_id)
    if not original.cancel_requested or original.status.value != "cancelled":
        raise ValueError("Stop the source run before importing its retained design into a recovery run.")
    if original.binding.source_mode != "pdf" or any(
        original.stage_statuses.get(stage.value).value != "accepted"
        for stage in (PreparationStage.P01, PreparationStage.P02, PreparationStage.P03)
    ):
        raise ValueError("A retained PDF with accepted source studies and design is required.")
    now = datetime.now(UTC).isoformat()
    frozen = original.binding.model_dump(mode="json")
    frozen["run_id"] = "prep-" + uuid.uuid4().hex
    frozen["commission"].pop("preparation_pipeline", None)
    frozen["commission_sha256"] = canonical_sha256(frozen["commission"])
    binding = PreparationRunBinding.model_validate_json(json.dumps(frozen))
    shutil.copytree(source_directory(slug, original_run_id), source_directory(slug, binding.run_id))
    load_source(binding)
    originals = preparation_path(slug, original_run_id).read_bytes()
    lineage = {"original_run_id": original_run_id,
        "original_checkpoint_sha256": hashlib.sha256(originals).hexdigest(),
        "original_binding_sha256": original.binding.binding_sha256,
        "original_artifact_digests": {a.artifact_id: a.content_sha256 for a in original.artifacts.values()},
        "reused_stages": ["P01", "P02", "P03"], "fresh_provider_calls": 0,
        "source_run_recorded_cost_microusd": original.cost_microusd_spent,
        "measurement": "Recovery measurement excludes retained source studies and accepted design."}
    rebound, plans, tasks, receipts = {}, {}, {}, {}
    for stage in (PreparationStage.P01, PreparationStage.P02, PreparationStage.P03):
        planned = []
        for old_task in original.stage_plans[stage.value].tasks:
            old_state = original.tasks[f"{stage.value}:{old_task.task_id}"]
            deps = tuple(rebound[key] for key in old_task.dependency_artifact_ids)
            task = old_task.model_copy(update={"dependency_artifact_ids": tuple(a.artifact_id for a in deps)})
            proposed = []
            for artifact_id in old_state.accepted_artifact_ids:
                old = original.artifacts[artifact_id]
                payload = deepcopy(old.payload)
                if old.artifact_kind == "commission_binding":
                    payload = {"binding_sha256": binding.binding_sha256,
                        "commission": binding.commission, "party": binding.hero_party_snapshot}
                new = build_artifact(binding, stage=stage, task_id=task.task_id,
                    artifact_kind=old.artifact_kind, payload=payload, dependency_artifacts=deps,
                    created_at=now, scope_keys=old.scope_keys, version=old.version)
                proposed.append(new)
                rebound[artifact_id] = new
            if stage == PreparationStage.P03:
                proposed.append(build_artifact(binding, stage=stage, task_id=task.task_id,
                    artifact_kind="checkpoint_reuse", payload=lineage, dependency_artifacts=deps,
                    created_at=now, scope_keys=task.scope_keys))
            # Run the production source/book/design checks over freshly bound payloads.
            validation = _validator(stage, binding, task, tuple(proposed), deps, book_client=None)
            if not validation.passed:
                raise ValueError(f"Retained {stage.value}/{task.task_id} did not pass local revalidation.")
            receipt_keys = []
            for receipt in validation.receipts:
                key = receipt.validator_id + ":" + receipt.details_sha256
                receipts[key] = receipt.model_dump(mode="json")
                receipt_keys.append(key)
            planned.append(task)
            tasks[f"{stage.value}:{task.task_id}"] = PreparationTaskState(task=task,
                status=PreparationTaskStatus.ACCEPTED, accepted_artifact_ids=tuple(a.artifact_id for a in proposed),
                validation_receipt_ids=tuple(receipt_keys), updated_at=now).model_dump(mode="json")
            # Lineage is an additional output of the accepted design task.
            if stage == PreparationStage.P03:
                rebound["lineage"] = proposed[-1]
        plans[stage.value] = StagePlan(stage=stage, tasks=tuple(planned),
            required_scope_keys=original.stage_plans[stage.value].required_scope_keys).model_dump(mode="json")
    store.create_run(binding)
    def imported(payload):
        payload.update(artifacts={a.artifact_id: a.model_dump(mode="json") for a in rebound.values()},
            tasks=tasks, validation_receipts=receipts, stage_plans=plans,
            stage_statuses={s: "accepted" for s in plans}, current_stage="P04", status="preparing")
    store.mutate_run(slug, binding.run_id, update=imported)
    store.load_run(slug, binding.run_id)
    if preparation_path(slug, original_run_id).read_bytes() != originals:
        raise ValueError("The source checkpoint changed while importing; stop before generation.")
    token = meta_store.begin_attempt(slug, job_attempt=True, job_kind="generation", reset_job=True)
    with meta_store.attempt_guard(slug, token) as meta:
        meta.pop("preparation_pipeline", None)
        meta.update(preparation_run_id=binding.run_id,
            preparation_binding={"binding_sha256": binding.binding_sha256,
                "source_binding_sha256": binding.source_binding_sha256,
                "hero_party_sha256": binding.hero_party_sha256,
                "profile_snapshot_sha256": binding.profile_snapshot_sha256})
        meta["job"].update(job_id=binding.run_id.removeprefix("prep-"),
            state="queued", stage="Preparing the campaign world", checkpoint_available=True,
            retained_work="source_pdf")
        meta_store.save_meta(slug, meta)
    _atomic_write(preparation_path(slug, binding.run_id).parent / "benchmark.json", {
        "started_at": now, "run_id": binding.run_id, "lineage": lineage,
        "target_minutes": 20, "target_usd": 5, "targets_are_caps": False})
    return binding.run_id, token


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug")
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--from-run")
    sources.add_argument("--resume-run")
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    if args.resume_run:
        meta = meta_store.load_meta(args.slug)
        run_id, token = meta["preparation_run_id"], meta["attempt"]["id"]
        if run_id != args.resume_run:
            raise ValueError("Resume must match the current saved recovery run identity.")
        state = PreparationStore().load_run(args.slug, run_id)
        if state.cancel_requested or state.status.value == "cancelled":
            raise ValueError("A cancelled source run cannot be resumed by this command.")
        if state.status.value in {"blocked", "failed"}:
            from apps.api.services.preparation_runtime import retry_preparation

            resumed_run, token = retry_preparation(args.slug)
            if resumed_run != run_id:
                raise ValueError("Retry changed the saved run identity.")
    else:
        run_id, token = import_saved_design(args.slug, args.from_run)
    print(json.dumps({"run_id": run_id, "state": "resumed" if args.resume_run else "imported",
                      "new_provider_calls": 0}), flush=True)
    if args.import_only:
        return
    from apps.api.services.preparation_runtime import prepare_adventure
    path = preparation_path(args.slug, run_id).parent / "benchmark.json"
    report = json.loads(path.read_text())
    report.setdefault("imported_at", report["started_at"])
    state = PreparationStore().load_run(args.slug, run_id)
    if not state.attempts_total:
        report["started_at"] = datetime.now(UTC).isoformat()
    # Worker handoffs resume the exact stored provider response. Preserve the
    # original wall-clock measurement rather than reporting only the last worker.
    report.setdefault("execution_started_at", report["started_at"])
    _atomic_write(path, report)
    result = await prepare_adventure(args.slug, attempt_token=token)
    report = json.loads(path.read_text())
    report.update(elapsed_seconds=(datetime.now(UTC) - datetime.fromisoformat(report["execution_started_at"])).total_seconds(),
        status=result.status.value if result else "setup_failed",
        fresh_input_and_output_tokens=result.tokens_spent if result else None,
        fresh_estimated_cost_microusd=result.cost_microusd_spent if result else None,
        fresh_provider_calls=result.provider_calls_spent if result else None)
    _atomic_write(path, report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
