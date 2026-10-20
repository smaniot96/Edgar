"""Private rejected proposals and correction feedback, bound to exact saved dependencies.

These files are diagnostic work, never accepted evidence or a publication receipt.
They live beside the private preparation checkpoint and are not projected by the API.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from edgar_core.preparation import canonical_sha256

from .preparation_store import _atomic_write, preparation_path


def proposal_directory(binding, task):
    identity = canonical_sha256({"binding": binding.binding_sha256,
                                 "task": task.model_dump(mode="json")})
    return preparation_path(binding.source_slug, binding.run_id).parent / "proposals" / identity


def latest_rejection(binding, task, schema, logical_call):
    directory = proposal_directory(binding, task)
    if not directory.exists():
        return None
    records = []
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text())
        if (record.get("schema") == schema.__name__ and record.get("diagnostics")
                and (record.get("logical_call") == logical_call or (
                    record.get("logical_call") is None and task.stage.value != "P09"
                ))
                and record.get("binding_sha256") == binding.binding_sha256):
            records.append(record)
    return max(records, key=lambda record: record["created_at"], default=None)


def save_proposal(binding, task, schema, value, usage, key):
    binding.assert_integrity()
    path = proposal_directory(binding, task) / (canonical_sha256(key) + ".json")
    record = {
        "binding_sha256": binding.binding_sha256,
        "task_id": task.task_id,
        "stage": task.stage.value,
        "schema": schema.__name__,
        "logical_call": key.split(":attempt:", 1)[0],
        "created_at": datetime.now(UTC).isoformat(),
        "proposal": value.model_dump(mode="json") if hasattr(value, "model_dump") else value,
        "usage": usage.model_dump(mode="json"),
        "diagnostics": [],
    }
    _atomic_write(path, record)
    return path, record


def reject_proposal(path, record, error):
    # This detail can contain private model/source text, so retain it only here.
    record["diagnostics"] = [{"type": type(error).__name__, "message": str(error)[:12000]}]
    _atomic_write(path, record)
    from .preparation_responses import reject_responses

    reject_responses(path.parent, {r["response_id"] for r in record["usage"].get("responses", [])})


def save_diagnostic(binding, boundary, error, *, task=None, dependencies=()):
    """Keep exact code errors private at boundaries without a provider proposal."""
    directory = preparation_path(binding.source_slug, binding.run_id).parent / "diagnostics"
    record = {"binding_sha256": binding.binding_sha256, "boundary": boundary,
              "created_at": datetime.now(UTC).isoformat(),
              "task_id": task.task_id if task is not None else None,
              "error_type": type(error).__name__, "message": str(error),
              "dependencies": {a.artifact_id: a.content_sha256 for a in dependencies}}
    _atomic_write(directory / (canonical_sha256(record) + ".json"), record)
