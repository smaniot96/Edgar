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
    transport = to_strict_json_schema(schema)
    if schema.__name__ == "Bundles":
        # Declarations precede their events within each chapter. Avoid trailing
        # root declarations that could be lost after a fully closed chapter when
        # the provider exhausts its output window. The local fold owns the root.
        transport["properties"].pop("mechanical_profiles", None)
        transport["required"] = list(transport["properties"])
    encounter = transport.get("$defs", {}).get("Encounter")
    if encounter:
        # Inline statistics remain readable only for retained paid responses.
        # New responses must reference the shared profile registry structurally.
        for field in ("creature", "source_stat_block", "source_pages"):
            encounter["properties"].pop(field, None)
        encounter["properties"]["profile_id"] = {
            "type": "string",
            "pattern": "^[a-z][a-z0-9_]{0,63}$",
            "description": "ID of a shared mechanical_profiles declaration; never repeat statistics here.",
        }
        encounter["required"] = list(encounter["properties"])
    return {
        "type": "json_schema",
        "name": schema.__name__,
        "schema": transport,
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


class OutputExhausted(NeedsAttention):
    """A terminal output-limit receipt retained for explicit, scoped recovery.

    This exception never accepts its output or retries the original request. Callers
    can inspect completed JSON values and plan distinct missing work, preserving the
    exact original response, output whitespace and provider usage in the task store.
    """

    def __init__(self, task_id: str, response: dict):
        self.task_id = task_id
        self.response_id = response.get("id")
        self.response = response
        self.output_text = response_text(response)
        super().__init__(
            "Retained provider output exhausted the model's output window: "
            + str(self.response_id or task_id)
            + " (task "
            + task_id
            + "). Original output and usage are preserved; no request was resubmitted."
        )


def completed_json_values(text: str) -> list[dict]:
    """Extract closed JSON values with their exact paths from a possibly partial root.

    An incomplete container is never returned as a complete record. Its completed
    fields/list items are returned with explicit paths instead; an empty path means
    the entire root is syntactically closed. No braces, quotes, scalar suffixes or
    missing fields are invented. At EOF an unclosed numeric/literal token is omitted
    conservatively, because its delimiter has not arrived. This is source recovery,
    not schema acceptance or proof that the retained response completed successfully.
    """

    def reject_constant(value):
        raise ValueError("Non-JSON constant " + value)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)
    recovered = []
    length = len(text)

    def whitespace(index):
        while index < length and text[index] in " \t\r\n":
            index += 1
        return index

    def value(index, path):
        index = whitespace(index)
        if index == length:
            return None
        try:
            parsed, end = decoder.raw_decode(text, index)
        except (ValueError, RecursionError):
            pass
        else:
            following = whitespace(end)
            closed = text[index] in '{["'
            delimited = following < length and text[following] in ",]}"
            valid_end = following == length if not path else following == length or delimited
            if valid_end and (closed or delimited):
                recovered.append({"path": path, "value": parsed})
                return end
            # A decoder can read the '1' prefix of '1e+'; never retain that scalar.
            if text[index] not in "{[":
                return None
        opening = text[index]
        if opening not in "{[":
            return None
        cursor, item, seen = index + 1, 0, set()
        while True:
            cursor = whitespace(cursor)
            if cursor >= length:
                return None
            if opening == "{":
                try:
                    key, end = decoder.raw_decode(text, cursor)
                except (ValueError, RecursionError):
                    return None
                if not isinstance(key, str) or key in seen:
                    return None
                seen.add(key)
                cursor = whitespace(end)
                if cursor >= length or text[cursor] != ":":
                    return None
                cursor += 1
            else:
                key = item
            end = value(cursor, [*path, key])
            if end is None:
                return None
            cursor = whitespace(end)
            if cursor >= length or text[cursor] != ",":
                return None
            cursor += 1
            item += 1

    try:
        value(0, [])
    except RecursionError:
        # Already closed siblings remain usable; a pathological open nest is not.
        pass
    return recovered


class SemanticClient:
    def __init__(self, store: Store, client=None):
        self.store = store
        self.client = client or AsyncOpenAI(
            api_key=OPENAI_API_KEY, max_retries=0, timeout=httpx.Timeout(90, connect=15)
        )
        self.semaphore = asyncio.Semaphore(3)

    async def call(
        self,
        node: str,
        prompt: str,
        schema,
        evidence: dict,
        images: list[Path] | None = None,
        *,
        reuse_saved_request: bool = False,
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
            if row is None and reuse_saved_request:
                # Chapter recovery may outlive a prompt edit. Adopt only a unique,
                # intact original dispatch with identical evidence, page bytes and
                # schema. Never rewrite that paid request or give it a new identity.
                matches = []
                with self.store.db() as db:
                    saved_rows = [
                        dict(saved)
                        for saved in db.execute("SELECT * FROM task WHERE node=?", (node,))
                    ]
                for saved in saved_rows:
                    saved_request = json.loads(saved["request"])
                    if (
                        saved_request.get("text", {}).get("format", {}).get("name")
                        != schema.__name__
                    ):
                        continue
                    saved_input = saved_request.get("input")
                    if (
                        not isinstance(saved_input, list)
                        or not saved_input
                        or not isinstance(saved_input[0], dict)
                        or saved_input[0].get("role") != "system"
                    ):
                        continue
                    comparable = {
                        **saved_request,
                        "input": [
                            {**saved_input[0], "content": instruction},
                            *saved_input[1:],
                        ],
                    }
                    # Typed readers retain old Encounter fields; only the NEW
                    # output transport changed. Reuse the saved schema verbatim
                    # when all original evidence and image bytes still match.
                    comparable["text"] = request["text"]
                    if isinstance(comparable.get("input"), list):
                        candidate = json.loads(json.dumps(request))
                        saved_data = json.loads(comparable["input"][1]["content"][0]["text"])
                        for payload in (candidate, comparable):
                            content = payload["input"][1]["content"]
                            if content and content[0].get("type") == "input_text":
                                data = json.loads(content[0]["text"])
                                if "known_mechanical_profiles" not in saved_data:
                                    data.pop("known_mechanical_profiles", None)
                                content[0]["text"] = json.dumps(
                                    data, ensure_ascii=False, sort_keys=True
                                )
                    else:
                        candidate = request
                    if comparable != candidate:
                        continue
                    if saved["id"] != digest(
                        {"node": node, "request": saved_request, "schema_version": SCHEMA_VERSION}
                    ):
                        raise NeedsAttention(
                            "Retained provider request integrity check failed: " + node
                        )
                    matches.append((saved, saved_request))
                if len(matches) > 1:
                    raise NeedsAttention(
                        "Multiple retained provider requests match the chapter evidence: " + node
                    )
                if matches:
                    row, request = matches[0]
                    task_id = row["id"]
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
            output_exhausted = (
                response.get("status") == "incomplete"
                and (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens"
            )
            if response.get("status") == "completed" or output_exhausted:
                resolved = response.get("model") or ""
                if resolved != MODEL and not resolved.startswith(MODEL + "-"):
                    raise NeedsAttention(
                        "Provider resolved a different model; retained output was not accepted."
                    )
            if response.get("status") != "completed":
                if output_exhausted:
                    raise OutputExhausted(task_id, response)
                raise NeedsAttention(
                    "Retained provider response is "
                    + str(response.get("status"))
                    + ": "
                    + response.get("id", task_id)
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
