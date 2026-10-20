"""Durable background Responses jobs: polling has no overall preparation deadline."""

from __future__ import annotations

import asyncio
import json
import uuid

import structlog
from openai import APIConnectionError, BadRequestError, InternalServerError, RateLimitError
from openai.types.responses import Response

from edgar_core.preparation import canonical_sha256

from .preparation_proposals import proposal_directory
from .preparation_store import _atomic_write

log = structlog.get_logger(__name__)
POLL_SECONDS = 3


async def await_response(client, binding, task, request, *, idempotency_key):
    try:
        return await _await_response(client, binding, task, request,
                                     idempotency_key=idempotency_key)
    finally:
        # Fencing the local worker does not stop an OpenAI background response.
        # Cancel only on the user's durable cancellation, never on a restart,
        # temporary connection failure or expired worker lease.
        from .preparation_store import preparation_path

        path = preparation_path(binding.source_slug, binding.run_id)
        state = json.loads(path.read_text()) if path.exists() else {}
        if state.get("cancel_requested"):
            await cancel_background_responses(client, binding, task)


async def cancel_background_responses(client, binding, task):
    """Retain every receipt while stopping work explicitly cancelled by the user."""
    for path in (proposal_directory(binding, task) / "responses").glob("*.json"):
        record = json.loads(path.read_text())
        response = record.get("response", {})
        if (record.get("binding_sha256") != binding.binding_sha256
                or response.get("status") not in {"queued", "in_progress"}):
            continue
        try:
            try:
                cancelled = await client.responses.cancel(response["id"])
            except BadRequestError:
                # Completion can win the race with cancellation; keep the paid answer.
                cancelled = await client.responses.retrieve(response["id"])
        except (APIConnectionError, InternalServerError, RateLimitError) as exc:
            log.warning("preparation_response_cancel_pending", response_id=response["id"],
                        error_type=type(exc).__name__)
            continue
        record["response"] = cancelled.model_dump(mode="json", by_alias=True)
        record["cancellation_requested"] = True
        _atomic_write(path, record)
        log.info("preparation_response_cancelled", response_id=cancelled.id,
                 status=cancelled.status)


async def _await_response(client, binding, task, request, *, idempotency_key):
    """Resume the exact stored job after a worker restart, without another generation.

    Individual HTTP requests still detect broken connections. A temporary polling
    failure never discards the job or imposes a deadline on model execution.
    """
    from .preparation import assert_current_preparation_worker

    request_digest = canonical_sha256(request)
    logical_call = idempotency_key.split(":attempt:", 1)[0]
    fingerprint = canonical_sha256({"request": request_digest, "logical_call": logical_call})
    path = proposal_directory(binding, task) / "responses" / (fingerprint + ".json")
    legacy = path.with_name(request_digest + ".json")
    if not path.exists() and legacy.exists():
        # Older checkpoints used request content alone. Preserve their exact job.
        path = legacy
    dispatch_path = path.with_suffix(".dispatch.json")
    assert_current_preparation_worker()
    record = None
    if path.exists():
        record = json.loads(path.read_text())
        if record["request_sha256"] != request_digest or record["binding_sha256"] != binding.binding_sha256:
            raise ValueError("Stored response does not match the exact preparation request.")
        if dispatch_path.exists():
            dispatch = json.loads(dispatch_path.read_text())
            if not dispatch.get("finished"):
                if record.get("dispatch_key") == dispatch["key"]:
                    # The receipt confirms this exact dispatch survived a crash.
                    dispatch["finished"] = True
                    _atomic_write(dispatch_path, dispatch)
                else:
                    # An earlier rejected receipt must not complete a newer
                    # dispatch whose HTTP result has not yet been persisted.
                    record = None
        if record is not None:
            # Older SDK dumps used the Python field name schema_, while response
            # parsing expects the provider's schema alias. Preserve those jobs.
            response_data = record["response"]
            output_format = (response_data.get("text") or {}).get("format") or {}
            if "schema_" in output_format and "schema" not in output_format:
                output_format["schema"] = output_format.pop("schema_")
            response = Response.model_validate(response_data)
        else:
            response = None
        if response is not None and (record.get("rejected") or response.status in {
            "failed", "cancelled", "incomplete"
        }):
            # Keep the exact earlier output and its accounting. A later logical
            # attempt may generate again; a restart of an in-flight call may not.
            archived = path.parent / "history" / (response.id + ".json")
            _atomic_write(archived, record)
            record = None
    if record is None:
        # Save dispatch identity before HTTP submission. Restarting between create
        # and receipt persistence must resend the same idempotency key.
        dispatch = json.loads(dispatch_path.read_text()) if dispatch_path.exists() else None
        if dispatch is None or dispatch.get("finished"):
            dispatch = {"key": canonical_sha256({"request": fingerprint,
                        "logical_call": idempotency_key, "generation": uuid.uuid4().hex}),
                        "finished": False}
            _atomic_write(dispatch_path, dispatch)
        response = await client.responses.create(
            **request,
            background=True,
            extra_headers={"Idempotency-Key": dispatch["key"]},
        )
        # Save the ID before checking the lease again: even an interrupted worker
        # must leave enough information to recover work already submitted.
        record = {"request_sha256": request_digest, "binding_sha256": binding.binding_sha256,
                  "logical_call": idempotency_key,
                  "dispatch_key": dispatch["key"],
                  "response": response.model_dump(mode="json", by_alias=True)}
        _atomic_write(path, record)
        dispatch["finished"] = True
        _atomic_write(dispatch_path, dispatch)
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
        record["response"] = response.model_dump(mode="json", by_alias=True)
        _atomic_write(path, record)
        assert_current_preparation_worker()
    return response


def reject_responses(directory, response_ids):
    """Mark only this proposal's generations unusable for a later correction."""
    directory = directory / "responses"
    for path in directory.glob("*.json"):
        record = json.loads(path.read_text())
        if record.get("response", {}).get("id") in response_ids:
            record["rejected"] = True
            _atomic_write(path, record)
