"""Per-turn diagnostics collector (EDG-053 contract, shared with EDG-079).

One `TurnDiagnostics` is created per play turn (normal or combat, sync or streamed), bound to the
running task with `bind_collector`, and filled by whoever measures something: the SSE/sync turn
runner (stages, lock wait, time to first text) and the provider call sites (`record_provider_call`).
`to_event()` produces the whitelisted `turn_diagnostics` telemetry row, which the caller stages in
`telemetry_events` so it commits or rolls back with the turn (same path as `combat_round`).

Rules the collector enforces, so every call site gets them for free:
  * A value that was not measured is `None` (or absent from `stages_ms` and listed in
    `stages_unavailable`). It is never zero. Totals are sums over KNOWN values only, and the
    payload says how many calls had unknown usage, so a partial total cannot pass as complete.
  * Nothing here accepts prompts, responses, headers or free text. Error categories come from a
    closed vocabulary; anything else is stored as `"unknown"`.
  * The collector is in-memory and holds no DB connection or lock; persistence is the caller's.
  * The number of recorded provider calls is bounded.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

TURN_DIAGNOSTICS_EVENT = "turn_diagnostics"
PAYLOAD_VERSION = 1
MAX_PROVIDER_CALLS = 16

PATHS = ("normal", "combat")
DELIVERIES = ("sync", "stream")
# Stage buckets shared by normal and combat turns so their rows are comparable.
STAGES: tuple[str, ...] = (
    "lock_wait_ms",
    "parsing_ms",
    "retrieving_ms",
    "adjudicating_ms",
    "narrating_ms",
    "saving_ms",
)
# What a provider call was for. `narrate` covers normal and combat narration alike.
CALL_KINDS = ("parse", "adjudicate", "narrate", "combat_setup", "summarize", "other")
ERROR_CATEGORIES = (
    "timeout",
    "rate_limited",
    "unavailable",
    "auth",
    "invalid_request",
    "cancelled",
    "internal",
    "unknown",
)


def _nonneg_int(value: Any) -> int | None:
    """An int >= 0, or None. Booleans and floats are not token counts."""
    return value if type(value) is int and value >= 0 else None


def normalize_usage(usage: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Whitelist provider `usage_metadata` into three counts; anything unusable is None."""
    usage = usage or {}
    return {
        "input_tokens": _nonneg_int(usage.get("input_tokens")),
        "output_tokens": _nonneg_int(usage.get("output_tokens")),
        "total_tokens": _nonneg_int(usage.get("total_tokens")),
    }


class TurnDiagnostics:
    def __init__(
        self,
        *,
        turn_id: str | None,
        path: str,
        delivery: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if path not in PATHS or delivery not in DELIVERIES:
            raise ValueError("unknown diagnostics path or delivery")
        self.turn_id = turn_id
        self.path = path
        self.delivery = delivery
        self._clock = clock
        self._started = clock()
        self._stages: dict[str, int] = {}
        self._calls: list[dict[str, Any]] = []
        self._calls_dropped = 0
        self._lock: dict[str, Any] | None = None
        self._retrieval: dict[str, Any] | None = None
        self._first_text_ms: int | None = None

    def set_path(self, path: str) -> None:
        """Routing is known only after parsing; the runner sets the final path then."""
        if path not in PATHS:
            raise ValueError("unknown diagnostics path")
        self.path = path

    def elapsed_ms(self) -> int:
        return int((self._clock() - self._started) * 1000)

    def record_stage_ms(self, stage: str, ms: int) -> None:
        """Add measured time to a stage. Repeated records accumulate."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        self._stages[stage] = self._stages.get(stage, 0) + max(0, int(ms))

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        start = self._clock()
        try:
            yield
        finally:
            self.record_stage_ms(stage, int((self._clock() - start) * 1000))

    def record_first_text(self) -> None:
        """Mark the first narration text sent to the client; only the first call counts."""
        if self._first_text_ms is None:
            self._first_text_ms = self.elapsed_ms()

    def record_lock(self, *, wait_ms: int, contended: bool, attempts: int | None = None) -> None:
        self._lock = {
            "wait_ms": max(0, int(wait_ms)),
            "contended": bool(contended),
            "attempts": _nonneg_int(attempts),
        }

    def record_retrieval(
        self,
        *,
        latency_ms: int,
        rules_chunks: int | None,
        adventure_chunks: int | None,
        degraded: bool,
    ) -> None:
        self._retrieval = {
            "latency_ms": max(0, int(latency_ms)),
            "rules_chunks": _nonneg_int(rules_chunks),
            "adventure_chunks": _nonneg_int(adventure_chunks),
            "degraded": bool(degraded),
        }

    def record_provider_call(
        self,
        *,
        kind: str,
        profile: str,
        requested_model: str | None,
        latency_ms: int,
        streamed: bool = False,
        first_text_ms: int | None = None,
        resolved_model: str | None = None,
        usage: Mapping[str, Any] | None = None,
        cost_microusd: int | None = None,
        attempts: int | None = None,
        error_category: str | None = None,
    ) -> None:
        """Record one provider call. `usage=None` means the provider reported none (unknown)."""
        if len(self._calls) >= MAX_PROVIDER_CALLS:
            self._calls_dropped += 1
            return
        if error_category is not None and error_category not in ERROR_CATEGORIES:
            error_category = "unknown"
        self._calls.append(
            {
                "kind": kind if kind in CALL_KINDS else "other",
                "profile": str(profile),
                "requested_model": requested_model,
                "resolved_model": resolved_model,
                "streamed": bool(streamed),
                "latency_ms": max(0, int(latency_ms)),
                "first_text_ms": None if first_text_ms is None else max(0, int(first_text_ms)),
                "attempts": _nonneg_int(attempts),
                "error_category": error_category,
                "usage": normalize_usage(usage),
                "cost_microusd": _nonneg_int(cost_microusd),
            }
        )

    def _totals(self) -> dict[str, Any]:
        def known_sum(key: str) -> int | None:
            values = [c["usage"][key] for c in self._calls if c["usage"][key] is not None]
            return sum(values) if values else None

        unknown_usage = sum(
            1
            for c in self._calls
            if c["usage"]["input_tokens"] is None or c["usage"]["output_tokens"] is None
        )
        costs = [c["cost_microusd"] for c in self._calls if c["cost_microusd"] is not None]
        unknown_cost = sum(1 for c in self._calls if c["cost_microusd"] is None)
        return {
            "calls": len(self._calls),
            "calls_dropped": self._calls_dropped,
            "input_tokens": known_sum("input_tokens"),
            "output_tokens": known_sum("output_tokens"),
            "total_tokens": known_sum("total_tokens"),
            "cost_microusd": sum(costs) if costs else None,
            "calls_unknown_usage": unknown_usage,
            "calls_unknown_cost": unknown_cost,
            "complete": bool(self._calls)
            and not unknown_usage
            and not unknown_cost
            and not self._calls_dropped,
        }

    def to_event(self) -> dict[str, Any]:
        """The telemetry row (`{event_type, payload}`) for this turn."""
        measured = {k: v for k, v in self._stages.items() if k in STAGES}
        return {
            "event_type": TURN_DIAGNOSTICS_EVENT,
            "payload": {
                "version": PAYLOAD_VERSION,
                "turn_id": self.turn_id,
                "path": self.path,
                "delivery": self.delivery,
                "total_ms": self.elapsed_ms(),
                "time_to_first_text_ms": self._first_text_ms,
                "stages_ms": measured,
                "stages_unavailable": [k for k in STAGES if k not in measured],
                "lock": self._lock,
                "retrieval": self._retrieval,
                "provider_calls": [dict(c, usage=dict(c["usage"])) for c in self._calls],
                "totals": self._totals(),
            },
        }


_current: contextvars.ContextVar[TurnDiagnostics | None] = contextvars.ContextVar(
    "turn_diagnostics", default=None
)


def current_collector() -> TurnDiagnostics | None:
    """The collector bound to this task, or None (call sites must tolerate None)."""
    return _current.get()


@contextmanager
def bind_collector(collector: TurnDiagnostics | None) -> Iterator[TurnDiagnostics | None]:
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
