"""Provider-call measurement for turn diagnostics (EDG-053). Fake chat models only.

Covers: unknown stays None (never zero), usage read through LangChain's own callback path, cost only
for a configured price, safe error categories that never store the exception text, streamed chunks
passed through unchanged, and cancellation recorded as a category.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from agent.provider_trace import (
    CONFIGURED_PRICES,
    cost_microusd,
    error_category,
    traced_call,
    traced_stream,
)
from agent.turn_diagnostics import (
    ERROR_CATEGORIES,
    TurnDiagnostics,
    bind_collector,
    normalize_usage,
)

SECRET = "sk-live-SECRET-token connection refused at 10.0.0.7"


class _FakeChat(BaseChatModel):
    """A deterministic chat model. `reply` is returned as-is, usage and model included."""

    reply: Any = None
    chunks: Any = None

    @property
    def _llm_type(self) -> str:
        return "fake-edg-053"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.reply)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self.chunks:
            yield ChatGenerationChunk(message=chunk)


def _collector() -> TurnDiagnostics:
    return TurnDiagnostics(turn_id="turn-1", path="normal", delivery="sync")


def _calls(collector: TurnDiagnostics) -> list[dict]:
    return collector.to_event()["payload"]["provider_calls"]


def _usage(input_tokens, output_tokens) -> dict:
    total = None if input_tokens is None or output_tokens is None else input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}


async def test_without_a_collector_the_call_is_a_pass_through() -> None:
    reply = AIMessage(content="ok")
    llm = _FakeChat(reply=reply)

    response = await traced_call(lambda: llm.ainvoke("hi"), kind="parse", profile="play.parser")

    assert response.content == "ok"


async def test_unreported_usage_and_cost_are_unknown_not_zero() -> None:
    collector = _collector()
    llm = _FakeChat(reply=AIMessage(content="no usage here"))

    with bind_collector(collector):
        await traced_call(lambda: llm.ainvoke("hi"), kind="narrate", profile="play.narrator")

    (call,) = _calls(collector)
    assert call["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert call["cost_microusd"] is None
    totals = collector.to_event()["payload"]["totals"]
    assert totals["input_tokens"] is None
    assert totals["cost_microusd"] is None
    assert totals["calls_unknown_usage"] == 1
    assert totals["complete"] is False


async def test_reported_zero_usage_is_recorded_as_zero() -> None:
    collector = _collector()
    reply = AIMessage(
        content="",
        usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        response_metadata={"model_name": "gpt-test-zero"},
    )
    with bind_collector(collector):
        await traced_call(
            lambda: _FakeChat(reply=reply).ainvoke("hi"), kind="parse", profile="play.parser"
        )

    (call,) = _calls(collector)
    assert call["usage"] == _usage(0, 0)
    assert collector.to_event()["payload"]["totals"]["input_tokens"] == 0


async def test_usage_is_read_through_langchain_and_priced_only_when_configured() -> None:
    collector = _collector()
    reply = AIMessage(
        content="{}",
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        response_metadata={"model_name": "gpt-6.1-sol-2026-10-01"},
    )
    with bind_collector(collector):
        await traced_call(
            lambda: _FakeChat(reply=reply).ainvoke("hi"), kind="adjudicate", profile="play.x"
        )

    (call,) = _calls(collector)
    assert call["resolved_model"] == "gpt-6.1-sol-2026-10-01"
    assert call["usage"] == _usage(100, 10)
    assert call["cost_microusd"] == 100 * 4 + 10 * 20


async def test_unpriced_model_keeps_usage_but_has_unknown_cost() -> None:
    collector = _collector()
    reply = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        response_metadata={"model_name": "gpt-unlisted-9"},
    )
    with bind_collector(collector):
        await traced_call(lambda: _FakeChat(reply=reply).ainvoke("hi"), kind="parse", profile="p")

    (call,) = _calls(collector)
    assert call["usage"] == _usage(5, 2)
    assert call["cost_microusd"] is None


def test_cost_needs_both_token_counts_and_a_configured_model() -> None:
    price_in, price_out = CONFIGURED_PRICES["gpt-6.1-sol"]
    assert cost_microusd("gpt-6.1-sol", _usage(3, 1)) == 3 * price_in + price_out
    assert cost_microusd("gpt-6.1-sol", _usage(3, None)) is None
    assert cost_microusd("gpt-other", _usage(3, 1)) is None
    assert cost_microusd("gpt-6.1-sol", None) is None


def test_price_table_mirrors_the_frozen_preparation_ceiling() -> None:
    from apps.api.services.preparation_provider import (
        INPUT_MICROUSD_PER_TOKEN,
        OUTPUT_MICROUSD_PER_TOKEN,
    )

    assert CONFIGURED_PRICES["gpt-6.1-sol"] == (INPUT_MICROUSD_PER_TOKEN, OUTPUT_MICROUSD_PER_TOKEN)


def _status_error(cls, status: int):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return cls(SECRET, response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "category"),
    [
        (_status_error(RateLimitError, 429), "rate_limited"),
        (APITimeoutError(request=httpx.Request("POST", "https://example.invalid")), "timeout"),
        (APIConnectionError(message=SECRET, request=httpx.Request("POST", "https://x.invalid")),
         "unavailable"),
        (_status_error(BadRequestError, 400), "invalid_request"),
        (_status_error(AuthenticationError, 401), "auth"),
        (asyncio.CancelledError(), "cancelled"),
        (RuntimeError(SECRET), "internal"),
    ],
)
def test_error_category_comes_from_the_closed_vocabulary(exc, category) -> None:
    assert error_category(exc) == category
    assert category in ERROR_CATEGORIES


async def test_failed_call_is_recorded_with_category_and_still_raises() -> None:
    collector = _collector()

    async def provider_down():
        raise APIConnectionError(message=SECRET, request=httpx.Request("POST", "https://x.invalid"))

    with bind_collector(collector), pytest.raises(APIConnectionError):
        await traced_call(provider_down, kind="narrate", profile="play.narrator", attempts=2)

    (call,) = _calls(collector)
    assert call["error_category"] == "unavailable"
    assert call["attempts"] == 2
    assert call["usage"] == _usage(None, None)
    assert call["cost_microusd"] is None


async def test_exception_text_is_never_stored() -> None:
    collector = _collector()

    async def leaky():
        raise RuntimeError(f"upstream said {SECRET} and echoed the prompt")

    with bind_collector(collector), pytest.raises(RuntimeError):
        await traced_call(leaky, kind="parse", profile="play.parser")

    stored = json.dumps(collector.to_event())
    assert SECRET not in stored
    assert "echoed the prompt" not in stored
    assert _calls(collector)[0]["error_category"] == "internal"


async def test_stream_yields_chunks_unchanged_and_reads_final_usage() -> None:
    collector = _collector()
    chunks = [
        AIMessageChunk(content=""),
        AIMessageChunk(content="The door "),
        AIMessageChunk(content="creaks."),
        AIMessageChunk(
            content="",
            usage_metadata={"input_tokens": 40, "output_tokens": 9, "total_tokens": 49},
            response_metadata={"model_name": "gpt-6.1-sol"},
        ),
    ]
    llm = _FakeChat(chunks=chunks)

    seen = []
    async for chunk in traced_stream(
        llm.astream("hi"), kind="narrate", profile="play.narrator", collector=collector
    ):
        seen.append(chunk.content)

    assert [text for text in seen if text] == ["The door ", "creaks."]
    (call,) = _calls(collector)
    assert call["streamed"] is True
    assert call["first_text_ms"] is not None
    assert call["usage"] == _usage(40, 9)
    assert call["cost_microusd"] == 40 * 4 + 9 * 20


async def test_stream_without_usage_reports_unknown_and_no_text_means_no_first_text() -> None:
    collector = _collector()

    async def silent():
        yield AIMessageChunk(content="")

    async for _ in traced_stream(silent(), kind="narrate", profile="play.narrator", collector=collector):
        pass

    (call,) = _calls(collector)
    assert call["first_text_ms"] is None
    assert call["usage"] == _usage(None, None)
    assert call["cost_microusd"] is None


async def test_abandoned_stream_is_recorded_as_cancelled() -> None:
    collector = _collector()

    async def long_stream():
        yield AIMessageChunk(content="one ")
        yield AIMessageChunk(content="two ")

    stream = traced_stream(long_stream(), kind="narrate", profile="play.narrator", collector=collector)
    first = await stream.__anext__()
    assert first.content == "one "
    await stream.aclose()

    (call,) = _calls(collector)
    assert call["error_category"] == "cancelled"
    assert call["first_text_ms"] is not None


def test_usage_normalisation_rejects_anything_that_is_not_a_count() -> None:
    assert normalize_usage({"input_tokens": "5", "output_tokens": None, "total_tokens": 3}) == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": 3,
    }
