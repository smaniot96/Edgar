"""Luna/medium semantic tasks with durable background response recovery."""

from __future__ import annotations

import asyncio
import base64
import json
import math
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from edgar_core.campaign_semantics import SCHEMA_VERSION
from edgar_core.config import OPENAI_API_KEY

from .source import token_count
from .store import Cancelled, NeedsAttention, Store, digest

PROMPTS = Path(__file__).parent / "prompts"
MODEL, EFFORT = "gpt-6-luna", "medium"
# Published model limits, not operational spending/time/output caps. Reserve the full
# supported output window without imposing max_output_tokens on a semantic request.
# https://developers.openai.com/api/docs/models/gpt-6-luna
MODEL_CONTEXT_TOKENS = 1_050_000
MODEL_OUTPUT_TOKENS = 128_000
MODEL_INPUT_TOKENS = MODEL_CONTEXT_TOKENS - MODEL_OUTPUT_TOKENS


def instructions(prompt):
    return (
        (PROMPTS / "principles-v2.md").read_text()
        + "\n\n"
        + (PROMPTS / (prompt + "-v2.md")).read_text()
    )


def response_format(schema):
    return {
        "type": "json_schema",
        "name": schema.__name__,
        "schema": to_strict_json_schema(schema),
        "strict": True,
    }


def image_token_estimate(path):
    """Conservative unscaled 32px patches; no assumed provider resize discount.

    Three tokens per patch deliberately exceeds the image multipliers in the public
    image guide. This is a local context estimate, never a billed-token measurement.
    Small synthetic/unknown image fixtures receive a conservative page allowance.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as image:
            width, height = image.size
    except UnidentifiedImageError:
        return 12_000
    return math.ceil(width / 32) * math.ceil(height / 32) * 3 + 256


def request_context(prompt, schema, evidence, images=None):
    """Measure the complete semantic input, including schema and image estimates."""
    parts = {
        "instructions": instructions(prompt),
        "input": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        "text": {"format": response_format(schema)},
        "model": MODEL,
        "reasoning": {"effort": EFFORT},
        "background": True,
        "store": True,
        "service_tier": "default",
    }
    text_tokens = token_count(parts)
    image_tokens = sum(image_token_estimate(path) for path in images or [])
    # o200k is a local approximation; include five percent for tokenizer/envelope
    # uncertainty and message delimiters. This margin never truncates source text.
    estimated = math.ceil((text_tokens + image_tokens) * 1.05) + 256
    return {
        "model": MODEL,
        "text_tokens": text_tokens,
        "image_tokens_estimate": image_tokens,
        "image_count": len(images or []),
        "estimated_input_tokens": estimated,
        "max_input_tokens": MODEL_INPUT_TOKENS,
        "context_window": MODEL_CONTEXT_TOKENS,
        "output_reserve": MODEL_OUTPUT_TOKENS,
        "fits": estimated <= MODEL_INPUT_TOKENS,
    }


def response_text(response):
    return "".join(
        part.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )


class SemanticClient:
    def __init__(self, store: Store, client=None):
        self.store = store
        self.client = client or AsyncOpenAI(
            api_key=OPENAI_API_KEY, max_retries=0, timeout=httpx.Timeout(90, connect=15)
        )
        self.semaphore = asyncio.Semaphore(3)

    async def call(
        self, node: str, prompt: str, schema, evidence: dict, images: list[Path] | None = None
    ):
        self.store.check()
        instruction = instructions(prompt)
        context = request_context(prompt, schema, evidence, images)
        if not context["fits"]:
            raise NeedsAttention(
                "Semantic request exceeds the model's input context; retain and split its "
                "source evidence before submission: " + str(context["estimated_input_tokens"])
            )
        parts = [
            {"type": "input_text", "text": json.dumps(evidence, ensure_ascii=False, sort_keys=True)}
        ]
        for path in images or []:
            parts.append(
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,"
                    + base64.b64encode(path.read_bytes()).decode(),
                    "detail": "high",
                }
            )
        request = {
            "model": MODEL,
            "reasoning": {"effort": EFFORT},
            "input": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": parts},
            ],
            "text": {"format": response_format(schema)},
            "background": True,
            "store": True,
            "service_tier": "default",
        }
        task_id = digest({"node": node, "request": request, "schema_version": SCHEMA_VERSION})
        async with self.semaphore:
            self.store.check()
            row = self.store.task(task_id)
            if row and row["accepted"]:
                return schema.model_validate(json.loads(row["accepted"]))
            if row and not row["response"]:
                raise NeedsAttention(
                    "Ambiguous provider submission "
                    + task_id
                    + "; reconcile its response ID before retry. No duplicate request submitted."
                )
            if row:
                response = json.loads(row["response"])
            else:
                # Commit dispatch identity before touching the network. SDK retries disabled.
                self.store.dispatch(task_id, node, request)
                try:
                    result = await self.client.responses.create(
                        **request,
                        metadata={"edgar_dispatch_id": task_id, "edgar_workflow": "campaign-v2"},
                        extra_headers={"Idempotency-Key": task_id},
                    )
                except (APIConnectionError, APIStatusError) as exc:
                    # Even an HTTP error is retained for diagnosis. No automatic create retry.
                    raise NeedsAttention(
                        "Provider submission needs reconciliation: "
                        + task_id
                        + " ("
                        + type(exc).__name__
                        + ")"
                    ) from exc
                response = result.model_dump(mode="json")
                self.store.response(task_id, response)
            delay = 2
            while response.get("status") in ("queued", "in_progress"):
                try:
                    self.store.check()
                except Cancelled:
                    try:
                        result = await self.client.responses.cancel(response["id"])
                        self.store.response(task_id, result.model_dump(mode="json"))
                    except (APIConnectionError, APIStatusError):
                        pass  # Receipt retained; cancellation is retried by recovery.
                    raise
                await asyncio.sleep(delay)
                try:
                    result = await self.client.responses.retrieve(response["id"])
                except APIConnectionError:
                    delay = min(delay * 2, 30)
                    continue
                except APIStatusError as exc:
                    if exc.status_code in (408, 429) or exc.status_code >= 500:
                        delay = min(delay * 2, 30)
                        continue
                    raise NeedsAttention(
                        "Saved response retrieval needs attention: " + response["id"]
                    ) from exc
                response = result.model_dump(mode="json")
                self.store.response(task_id, response)  # Full output before parsing/acceptance.
                delay = 2
            self.store.check()
            if response.get("status") != "completed":
                raise NeedsAttention(
                    "Retained provider response is "
                    + str(response.get("status"))
                    + ": "
                    + response.get("id", task_id)
                )
            resolved = response.get("model", "")
            if resolved != MODEL and not resolved.startswith(MODEL + "-"):
                raise NeedsAttention(
                    "Provider resolved a different model; retained output was not accepted."
                )
            try:
                value = schema.model_validate_json(response_text(response))
            except ValueError as exc:
                # Representation errors must be fixed in code/schema; preserve paid output.
                raise NeedsAttention(
                    "Retained output requires a schema/code fix for " + task_id + ": " + str(exc)
                ) from exc
            self.store.accept(task_id, value.model_dump(mode="json"))
            return value

    async def reconcile(self, dispatch_id: str, response_id: str):
        """Explicit recovery for a lost POST receipt; metadata must prove exact dispatch."""
        row = self.store.task(dispatch_id)
        if not row or row["response"]:
            raise NeedsAttention("Dispatch is absent or already has a response receipt.")
        response = (await self.client.responses.retrieve(response_id)).model_dump(mode="json")
        if (response.get("metadata") or {}).get("edgar_dispatch_id") != dispatch_id:
            raise NeedsAttention("Response does not belong to this dispatch.")
        self.store.response(dispatch_id, response)

    async def reconcile_cancellation(self) -> bool:
        """Reconcile cancelled runs' known provider jobs without submitting new work.

        Cancellation deliberately bypasses ``store.check``: saved provider jobs still need
        attention after the run is cancelled. The caller owns recoverable retry scheduling;
        a completed response is retained as paid work, never parsed or accepted here.
        """
        with self.store.db() as db:
            rows = [
                dict(row)
                for row in db.execute("SELECT id,response FROM task WHERE response IS NOT NULL")
            ]
        pending = False
        for row in rows:
            saved = json.loads(row["response"])
            if saved.get("status") not in ("queued", "in_progress"):
                continue
            response_id = saved.get("id")
            if not response_id:
                raise NeedsAttention("A running provider receipt has no response identity.")
            try:
                result = await self.client.responses.retrieve(response_id)
                current = result.model_dump(mode="json")
                self.store.response(row["id"], current)
                if current.get("status") in ("queued", "in_progress"):
                    result = await self.client.responses.cancel(response_id)
                    current = result.model_dump(mode="json")
                    self.store.response(row["id"], current)
                pending |= current.get("status") in ("queued", "in_progress")
            except APIConnectionError:
                pending = True
            except APIStatusError as exc:
                if exc.status_code in (408, 429) or exc.status_code >= 500:
                    pending = True
                else:
                    raise NeedsAttention(
                        "Saved response cancellation needs attention: " + response_id
                    ) from exc
        return pending
