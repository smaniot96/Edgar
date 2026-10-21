"""Luna/medium semantic tasks with durable background response recovery."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from openai.lib._pydantic import to_strict_json_schema

from edgar_core.campaign_semantics import SCHEMA_VERSION
from edgar_core.config import OPENAI_API_KEY

from .store import Cancelled, NeedsAttention, Store, digest

PROMPTS = Path(__file__).parent / "prompts"
MODEL, EFFORT = "gpt-6-luna", "medium"


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
        instruction = (
            (PROMPTS / "principles-v2.md").read_text()
            + "\n\n"
            + (PROMPTS / (prompt + "-v2.md")).read_text()
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
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": to_strict_json_schema(schema),
                    "strict": True,
                }
            },
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
