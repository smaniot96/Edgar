"""Measured provider calls for turn diagnostics (EDG-053).

`traced_call` wraps one awaited provider call and `traced_stream` wraps one streamed call. Both
record a single `record_provider_call` into the collector (explicit, or the bound one) and change
nothing the caller sees: the same return value, the same chunks, the same exceptions.

What is measured, and where it comes from:
  * latency: wall time around the call (or to the end of the stream);
  * first_text_ms: streams only, the first chunk with text;
  * usage: LangChain's `AIMessage.usage_metadata`, captured by a callback handler held in a
    context variable for the duration of each awaited step. This works for structured-output
    calls too, whose return value is the parsed object and carries no usage. Unreported usage is
    `None`, never zero;
  * resolved model: the `model_name` the provider echoed in response metadata;
  * cost: an ESTIMATE at the configured conservative per-token rates (not a billed amount), only
    when `CONFIGURED_PRICES` has a rate for the model. Otherwise `None`;
  * error category: a member of `ERROR_CATEGORIES`, derived from the exception class and the
    existing safe provider code. The exception message and provider body are never read or kept.

Not measured: SDK-internal retries (`attempts` is the caller's own attempt count, or None).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook
from openai import APITimeoutError, BadRequestError

from agent.llm import message_text
from agent.turn_diagnostics import TurnDiagnostics, current_collector, normalize_usage
from edgar_core.model_profiles import effective_profile
from edgar_core.safe_errors import ErrorCategory, classify_failure

# USD micro-units per token (input, output) for models with a configured price. Mirrors the frozen
# preparation ceilings in apps/api/services/preparation_provider.py (asserted equal in tests). A
# model absent here has no configured price, so its cost stays unknown. Do not add guessed prices.
CONFIGURED_PRICES: dict[str, tuple[int, int]] = {"gpt-6.1-sol": (4, 20)}

# The safe-failure taxonomy (edgar_core.safe_errors) mapped onto the closed diagnostics vocabulary.
_TAXONOMY_CATEGORY = {
    ErrorCategory.RATE_LIMITED: "rate_limited",
    ErrorCategory.QUOTA_EXCEEDED: "rate_limited",
    ErrorCategory.PROVIDER_UNAVAILABLE: "unavailable",
    ErrorCategory.PROVIDER_CONFIGURATION: "auth",
    ErrorCategory.CANCELLED: "cancelled",
    ErrorCategory.INTERNAL: "internal",
}


class _UsageSink(BaseCallbackHandler):
    """Collects one provider call's reported usage, keyed by the model that answered."""

    run_inline = True  # Handle synchronously so the entry exists when the awaited call returns.

    def __init__(self) -> None:
        self.entries: list[tuple[str | None, dict[str, int | None]]] = []

    def on_llm_end(self, response: Any, **_kwargs: Any) -> None:
        try:
            message = response.generations[0][0].message
        except (AttributeError, IndexError, TypeError):
            return
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            return
        model = (getattr(message, "response_metadata", None) or {}).get("model_name")
        self.entries.append((model if isinstance(model, str) else None, normalize_usage(usage)))

    def result(self) -> tuple[dict[str, int | None] | None, str | None]:
        if not self.entries:
            return None, None
        models = {m for m, _ in self.entries}
        resolved = models.pop() if len(models) == 1 else None
        usage = {
            key: _sum([entry[key] for _, entry in self.entries])
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        return usage, resolved


# Registered once: each new sink is set in the context around an awaited step, and LangChain then
# attaches it to the model runs made inside that step.
_SINK: ContextVar[_UsageSink | None] = ContextVar("provider_usage_sink", default=None)
register_configure_hook(_SINK, inheritable=True)


def _sum(values: list[int | None]) -> int | None:
    """Sum only when every part is known; one unknown part makes the total unknown."""
    if any(v is None for v in values):
        return None
    return sum(values)  # type: ignore[arg-type]


def _add_usage(
    total: dict[str, int | None] | None, usage: Any
) -> dict[str, int | None] | None:
    if not usage:
        return total
    normalized = normalize_usage(usage)
    if total is None:
        return normalized
    return {k: _sum([total[k], normalized[k]]) for k in normalized}


def error_category(exc: BaseException) -> str:
    """Closed-vocabulary category for a failed call. Never reads the exception message."""
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return "timeout"
    if isinstance(exc, BadRequestError):
        return "invalid_request"
    return _TAXONOMY_CATEGORY.get(classify_failure(exc).category, "unknown")


def _requested_model(profile: str) -> str | None:
    try:
        return effective_profile(profile).model
    except Exception:  # An unknown or unavailable profile is recorded as an unknown model.
        return None


def _price_for(model: str | None) -> tuple[int, int] | None:
    if model is None:
        return None
    for name, price in CONFIGURED_PRICES.items():
        if model == name or model.startswith(name + "-"):
            return price
    return None


def cost_microusd(model: str | None, usage: Mapping[str, int | None] | None) -> int | None:
    """Priced only for a configured model with known input and output tokens."""
    if usage is None:
        return None
    price = _price_for(model)
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if price is None or tokens_in is None or tokens_out is None:
        return None
    return tokens_in * price[0] + tokens_out * price[1]


class _CallTrace:
    """One provider call's measurements, written exactly once."""

    def __init__(
        self,
        collector: TurnDiagnostics,
        *,
        kind: str,
        profile: str,
        streamed: bool,
        attempts: int | None,
    ) -> None:
        self.collector = collector
        self.kind = kind
        self.profile = profile
        self.streamed = streamed
        self.attempts = attempts
        self.requested_model = _requested_model(profile)
        self.started = time.monotonic()
        self.first_text_ms: int | None = None
        self.sink = _UsageSink()
        self._finished = False

    def mark_text(self) -> None:
        if self.first_text_ms is None:
            self.first_text_ms = int((time.monotonic() - self.started) * 1000)

    def finish(
        self,
        *,
        error: BaseException | None = None,
        response: Any = None,
        fallback_usage: Any = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        usage, resolved = self.sink.result()
        if usage is None:
            # Usage not seen by the callback: use the message the call itself returned, if any.
            usage = _add_usage(None, getattr(response, "usage_metadata", None) or fallback_usage)
        # The answering model prices the call when the provider reported one; a reported model
        # without a configured price stays unpriced rather than falling back to the requested rate.
        model = resolved if resolved is not None else self.requested_model
        self.collector.record_provider_call(
            kind=self.kind,
            profile=self.profile,
            requested_model=self.requested_model,
            resolved_model=resolved,
            latency_ms=int((time.monotonic() - self.started) * 1000),
            streamed=self.streamed,
            first_text_ms=self.first_text_ms,
            usage=usage,
            cost_microusd=cost_microusd(model, usage),
            attempts=self.attempts,
            error_category=None if error is None else error_category(error),
        )


async def traced_call[T](
    call: Callable[[], Awaitable[T]],
    *,
    kind: str,
    profile: str,
    attempts: int | None = None,
) -> T:
    """Await one provider call, recording it when a collector is bound. A no-op otherwise."""
    collector = current_collector()
    if collector is None:
        return await call()
    trace = _CallTrace(collector, kind=kind, profile=profile, streamed=False, attempts=attempts)
    token = _SINK.set(trace.sink)
    try:
        response = await call()
    except BaseException as exc:
        trace.finish(error=exc)
        raise
    finally:
        _SINK.reset(token)
    trace.finish(response=response)
    return response


async def traced_stream(
    stream: AsyncIterator[Any],
    *,
    kind: str,
    profile: str,
    collector: TurnDiagnostics | None = None,
) -> AsyncIterator[Any]:
    """Yield a provider stream's chunks unchanged, recording the call when it ends.

    The collector is passed explicitly: this generator yields across the caller's frames, so it
    never relies on a bound contextvar for its own records.
    """
    collector = collector or current_collector()
    if collector is None:
        async for chunk in stream:
            yield chunk
        return
    trace = _CallTrace(collector, kind=kind, profile=profile, streamed=True, attempts=None)
    iterator = stream.__aiter__()
    chunk_usage: dict[str, int | None] | None = None
    try:
        while True:
            token = _SINK.set(trace.sink)
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            finally:
                _SINK.reset(token)
            if message_text(chunk):
                trace.mark_text()
            chunk_usage = _add_usage(chunk_usage, getattr(chunk, "usage_metadata", None))
            yield chunk
    except BaseException as exc:
        trace.finish(error=exc, fallback_usage=chunk_usage)
        raise
    trace.finish(fallback_usage=chunk_usage)
