"""Durable background Responses jobs: polling has no overall preparation deadline."""

from __future__ import annotations

import asyncio
import json
import uuid

import structlog
from openai import APIConnectionError, BadRequestError, InternalServerError, RateLimitError
from openai.types.responses import Response

from edgar_core.model_profiles import LOCKED_EFFORT, LOCKED_MODEL, ModelProfileSnapshot
from edgar_core.preparation import canonical_sha256

from .preparation_proposals import proposal_directory
from .preparation_store import _atomic_write

log = structlog.get_logger(__name__)
POLL_SECONDS = 3


async def await_response(client, binding, task, request, *, idempotency_key, receipt=None):
    try:
        return await _await_response(client, binding, task, request,
                                     idempotency_key=idempotency_key, receipt=receipt)
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


def _retained_request_variants(binding, task, request):
    """Reconstruct old request hashes without relaxing source/schema identity.

    The historical model/effort are the only differences allowed; every instruction,
    source character, image and schema remains exact.
    """
    original = ModelProfileSnapshot.from_dict(binding.profile_snapshot).profiles[
        f"preparation.{task.stage.value.lower()}"
    ]
    policies = ((request["model"], request["reasoning"]),
                (original.model, {"effort": original.reasoning_effort}))
    seen = {canonical_sha256(request)}
    for model, reasoning in policies:
        variant = {**request, "model": model, "reasoning": reasoning}
        digest = canonical_sha256(variant)
        if digest not in seen:
            seen.add(digest)
            yield variant, digest


async def _await_response(client, binding, task, request, *, idempotency_key, receipt=None):
    """Resume the exact stored job after a worker restart, without another generation.

    Individual HTTP requests still detect broken connections. A temporary polling
    failure never discards the job or imposes a deadline on model execution.
    """
    from .preparation import assert_current_preparation_worker

    if (request.get("model"), request.get("reasoning", {}).get("effort")) != (
        LOCKED_MODEL, LOCKED_EFFORT
    ):
        raise ValueError("Fresh preparation requests must use the current campaign model policy.")
    request_digest = canonical_sha256(request)
    logical_call = idempotency_key.split(":attempt:", 1)[0]
    fingerprint = canonical_sha256({"request": request_digest, "logical_call": logical_call})
    path = proposal_directory(binding, task) / "responses" / (fingerprint + ".json")
    legacy = path.with_name(request_digest + ".json")
    if not path.exists() and legacy.exists():
        # Older checkpoints used request content alone. Preserve their exact job.
        path = legacy
    current_path = path
    current_digest = request_digest
    actual_request = request
    if not path.exists():
        # Recover paid background work from an older policy. Matching hashes are
        # reconstructed from the exact current semantic request, never model name alone.
        for historical, historical_digest in _retained_request_variants(binding, task, request):
            historical_fingerprint = canonical_sha256(
                {"request": historical_digest, "logical_call": logical_call})
            for candidate in (path.with_name(historical_fingerprint + ".json"),
                              path.with_name(historical_digest + ".json")):
                if not candidate.exists():
                    historical_dispatch = candidate.with_suffix(".dispatch.json")
                    if (historical_dispatch.exists()
                            and not json.loads(historical_dispatch.read_text()).get("finished")):
                        raise ValueError("Historical provider dispatch needs its missing receipt reconciled.")
                    continue
                saved = json.loads(candidate.read_text())
                if (saved.get("request_sha256") != historical_digest
                        or saved.get("binding_sha256") != binding.binding_sha256
                        or saved.get("logical_call", "").split(":attempt:", 1)[0] != logical_call
                        or saved.get("rejected")
                        or saved.get("response", {}).get("status") not in {
                            "queued", "in_progress", "completed"}):
                    continue
                path, request_digest, actual_request = candidate, historical_digest, historical
                break
            if path != current_path:
                break
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
        # Historical dispatch receipts are immutable. In particular, never resend
        # an old model's unfinished HTTP submission when there is no response ID.
        if path != current_path:
            raise ValueError("Historical provider dispatch needs its missing receipt reconciled.")
        path, request_digest, actual_request = current_path, current_digest, request
        dispatch_path = path.with_suffix(".dispatch.json")
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
                  "requested_model": request["model"],
                  "reasoning_effort": request["reasoning"]["effort"],
                  "dispatch_key": dispatch["key"],
                  "response": response.model_dump(mode="json", by_alias=True)}
        _atomic_write(path, record)
        dispatch["finished"] = True
        _atomic_write(dispatch_path, dispatch)
    if receipt is not None:
        receipt.update(requested_model=actual_request["model"],
                       reasoning_effort=actual_request["reasoning"]["effort"],
                       response_id=response.id)
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
