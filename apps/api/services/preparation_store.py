"""Durable private preparation checkpoints on the existing adventure metadata store."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from edgar_core.preparation import (
    PreparationRunBinding,
    PreparationRunState,
    assert_artifact_integrity,
)

from . import adventures_meta

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")


class PreparationStoreError(RuntimeError):
    """The checkpoint cannot be safely read or changed."""


class StalePreparationRevision(PreparationStoreError):
    """Another worker changed the checkpoint after a caller read it."""


def preparation_path(slug: str, run_id: str) -> Path:
    """Return the private run checkpoint beneath its existing adventure job directory."""

    if not _SLUG.fullmatch(slug):
        raise ValueError("Invalid adventure source binding.")
    run_uuid = run_id.removeprefix("prep-")
    if not re.fullmatch(r"[0-9a-f]{32}", run_uuid):
        raise ValueError("Invalid preparation run identity.")
    return adventures_meta.job_dir(slug, run_uuid) / "preparation.json"


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(value, file, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class PreparationStore:
    """Atomic run checkpoints; lifecycle lock is held only around file operations."""

    def create_run(self, binding: PreparationRunBinding) -> PreparationRunState:
        path = preparation_path(binding.source_slug, binding.run_id)
        now = datetime.now(UTC).isoformat()
        state = PreparationRunState(
            binding=binding,
            created_at=now,
            updated_at=now,
        )
        with adventures_meta.lifecycle_lock(binding.source_slug):
            if path.exists():
                raise PreparationStoreError("Preparation run already exists.")
            _atomic_write(path, state.model_dump(mode="json"))
        return state

    def load_run(self, slug: str, run_id: str) -> PreparationRunState:
        path = preparation_path(slug, run_id)
        try:
            state = PreparationRunState.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PreparationStoreError("Preparation run was not found.") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise PreparationStoreError("Saved preparation work cannot be safely resumed.") from exc
        if state.binding.run_id != run_id or state.binding.source_slug != slug:
            raise PreparationStoreError("Saved preparation binding does not match this run.")
        state.binding.assert_integrity()
        digests: dict[str, str] = {}
        for artifact_id, artifact in state.artifacts.items():
            assert_artifact_integrity(state.binding, artifact)
            if artifact_id != artifact.artifact_id:
                raise PreparationStoreError("Saved preparation artifact key does not match its identity.")
            digests[artifact_id] = artifact.content_sha256
        for artifact in state.artifacts.values():
            if artifact.dependency_digests != {
                dependency_id: digests[dependency_id]
                for dependency_id in artifact.dependency_digests
                if dependency_id in digests
            }:
                raise PreparationStoreError("Saved preparation dependency inventory is incomplete or stale.")
        for receipt_key, receipt in state.validation_receipts.items():
            if (receipt_key != receipt.validator_id + ":" + receipt.details_sha256
                    or not receipt.artifact_digests
                    or any(digests.get(key) != digest for key, digest in receipt.artifact_digests.items())
                    or any(digests.get(key) != digest for key, digest in receipt.dependency_digests.items())):
                raise PreparationStoreError("Saved validation receipt is stale or unbound.")
        expected_artifacts: set[str] = set()
        for task_state in state.tasks.values():
            expected_artifacts.update(task_state.accepted_artifact_ids)
            if any(key not in state.validation_receipts for key in task_state.validation_receipt_ids):
                raise PreparationStoreError("Accepted task is missing its exact validation receipt.")
        if expected_artifacts != set(state.artifacts):
            raise PreparationStoreError("Saved artifact inventory does not match accepted tasks.")
        budget = state.binding.budget
        over_budget = (
            state.provider_calls_spent + state.provider_calls_reserved > budget.max_provider_calls
            or state.tokens_spent + state.tokens_reserved > budget.max_total_tokens
            or state.cost_microusd_spent + state.cost_microusd_reserved > budget.max_cost_microusd
        )
        if over_budget and state.status.value not in ("blocked", "failed", "cancelled"):
            raise PreparationStoreError("Saved preparation work exceeds its frozen budget.")
        return state

    def update_run(
        self,
        slug: str,
        run_id: str,
        *,
        expected_revision: int,
        update: Callable[[dict], None],
    ) -> PreparationRunState:
        """Apply a small checkpoint update with compare-and-swap under the source lifecycle lock."""

        path = preparation_path(slug, run_id)
        with adventures_meta.lifecycle_lock(slug):
            current = self.load_run(slug, run_id)
            if current.revision != expected_revision:
                raise StalePreparationRevision("A newer worker checkpoint superseded this update.")
            payload = current.model_dump(mode="json")
            update(payload)
            payload["revision"] = current.revision + 1
            payload["updated_at"] = datetime.now(UTC).isoformat()
            try:
                updated = PreparationRunState.model_validate_json(json.dumps(payload))
            except (ValueError, TypeError) as exc:
                raise PreparationStoreError("Checkpoint update failed validation.") from exc
            _atomic_write(path, updated.model_dump(mode="json"))
            return updated

    def mutate_run(
        self,
        slug: str,
        run_id: str,
        *,
        update: Callable[[dict], None],
    ) -> PreparationRunState:
        """Serialize a state transition with other workers under the existing source lock."""

        path = preparation_path(slug, run_id)
        with adventures_meta.lifecycle_lock(slug):
            current = self.load_run(slug, run_id)
            payload = current.model_dump(mode="json")
            update(payload)
            payload["revision"] = current.revision + 1
            payload["updated_at"] = datetime.now(UTC).isoformat()
            try:
                updated = PreparationRunState.model_validate_json(json.dumps(payload))
            except (ValueError, TypeError) as exc:
                raise PreparationStoreError("Checkpoint update failed validation.") from exc
            _atomic_write(path, updated.model_dump(mode="json"))
            return updated

    def replace_run(
        self,
        state: PreparationRunState,
        *,
        expected_revision: int,
    ) -> PreparationRunState:
        """Replace state from a prepared snapshot; check revision and binding under the lock."""

        payload = state.model_dump(mode="json")

        def apply(current: dict) -> None:
            if current["binding"]["run_id"] != payload["binding"]["run_id"]:
                raise PreparationStoreError("Run identity cannot change during preparation.")
            if current["binding"] != payload["binding"]:
                raise PreparationStoreError("Frozen run bindings cannot be changed in place.")
            current.clear()
            current.update(payload)

        return self.update_run(
            state.binding.source_slug,
            state.binding.run_id,
            expected_revision=expected_revision,
            update=apply,
        )
