"""Durable background Responses jobs: polling has no overall preparation deadline."""

from __future__ import annotations

import asyncio
import json

import structlog
from openai import APIConnectionError, InternalServerError, RateLimitError
from openai.types.responses import Response

from edgar_core.preparation import canonical_sha256

from .preparation_proposals import proposal_directory
from .preparation_store import _atomic_write

log = structlog.get_logger(__name__)
POLL_SECONDS = 3


async def await_response(client, binding, task, request):
    """Resume the exact stored job after a worker restart, without another generation.

    Individual HTTP requests still detect broken connections. A temporary polling
    failure never discards the job or imposes a deadline on model execution.
    """
    from .preparation import assert_current_preparation_worker

    fingerprint = canonical_sha256(request)
    path = proposal_directory(binding, task) / "responses" / (fingerprint + ".json")
    assert_current_preparation_worker()
    if path.exists():
        record = json.loads(path.read_text())
        if record["request_sha256"] != fingerprint or record["binding_sha256"] != binding.binding_sha256:
            raise ValueError("Stored response does not match the exact preparation request.")
        response = Response.model_validate(record["response"])
    else:
        response = await client.responses.create(
            **request,
            background=True,
            extra_headers={"Idempotency-Key": fingerprint},
        )
        # Save the ID before checking the lease again: even an interrupted worker
        # must leave enough information to recover work already submitted.
        record = {"request_sha256": fingerprint, "binding_sha256": binding.binding_sha256,
                  "response": response.model_dump(mode="json")}
        _atomic_write(path, record)
    assert_current_preparation_worker()
    while response.status in {"queued", "in_progress"}:
        await asyncio.sleep(POLL_SECONDS)
        assert_current_preparation_worker()
        try:
            response = await client.responses.retrieve(response.id)
        except (APIConnectionError, InternalServerError, RateLimitError) as exc:
            log.warning("preparation_response_poll_retry", stage=task.stage.value,
                        task_id=task.task_id, error_type=type(exc).__name__)
            continue
        record["response"] = response.model_dump(mode="json")
        _atomic_write(path, record)
        assert_current_preparation_worker()
    return response
