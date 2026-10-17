"""In-process operational metrics with Prometheus text exposition (EDG-139).

A small hand-written registry: counters and histograms with fixed, closed label vocabularies, rendered
in the Prometheus text format on demand. No dependency and no sidecar; see docs/operational-metrics.md.

Rules enforced here, so every call site gets them for free:
  * Labels are closed vocabularies. A value outside its vocabulary is recorded as ``other``, so a
    stray value can never create a new series. Sessions, users, campaigns, turn ids, prompts, model
    output and free-text errors are never labels; they are not even accepted as arguments.
  * Recording is best-effort: every ``observe_*`` function swallows its own errors and logs a fixed
    event name, so a metrics fault can never fail a turn, a readiness probe or a lock acquisition.
  * Recording holds no database connection and performs no I/O.
  * Unknown values stay unknown. A missing token count, cost or latency is never recorded as zero;
    token and cost counters only grow by values that were measured, and calls with unknown usage or
    cost are counted in their own series.
  * Each turn is recorded once: a committed turn from its persisted row, an uncommitted turn from its
    collector, and never both (``_RECORDED`` is consulted before recording).

Process-local: each API worker keeps its own counters, so run one worker per exposed API process or
aggregate in the scraper (see the operations doc).
"""

from __future__ import annotations

import bisect
import math
import os
import re
import threading
import weakref
from collections.abc import Callable, Iterable, Mapping
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import structlog

from agent.turn_diagnostics import (
    CALL_KINDS,
    DELIVERIES,
    ERROR_CATEGORIES,
    PATHS,
    STAGES,
    TURN_DIAGNOSTICS_EVENT,
    TurnDiagnostics,
)

log = structlog.get_logger()

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
METRICS_ENABLED_ENV = "EDGAR_METRICS_ENABLED"
METRICS_TOKEN_ENV = "EDGAR_METRICS_TOKEN"

OTHER = "other"
TURN_OUTCOMES = ("committed", "failed")
PROVIDER_RESULTS = ("ok", "error")
RETRIEVAL_RESULTS = ("ok", "degraded")
LOCK_RESULTS = ("acquired", "contended")
READY_RESULTS = ("ready", "unavailable")
CHECK_RESULTS = ("ok", "unavailable")
TOKEN_TYPES = ("input", "output", "total")
READINESS_COMPONENTS = ("postgres", "schema", "redis", "qdrant")
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)

_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(float(value))


class _Family:
    """A named metric with fixed label names; each label has a closed set of allowed values."""

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labels: Mapping[str, Iterable[str]]) -> None:
        if not _NAME.match(name):
            raise ValueError(f"invalid metric name {name!r}")
        for label in labels:
            if not _LABEL.match(label) or label == "le":
                raise ValueError(f"invalid label name {label!r}")
        self.name = name
        self.help = help_text
        self._label_names = tuple(labels)
        self._allowed = {
            label: frozenset(str(v) for v in values) | {OTHER} for label, values in labels.items()
        }
        self._lock = threading.RLock()

    def _key(self, labels: Mapping[str, object]) -> tuple[str, ...]:
        if set(labels) != set(self._label_names):
            raise ValueError(f"{self.name} expects labels {self._label_names}")
        key = []
        for label in self._label_names:
            value = str(labels[label])
            key.append(value if value in self._allowed[label] else OTHER)
        return tuple(key)

    def _label_text(self, key: tuple[str, ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
        pairs = [(n, v) for n, v in zip(self._label_names, key, strict=True)] + list(extra)
        if not pairs:
            return ""
        return "{" + ",".join(f'{n}="{_escape(v)}"' for n, v in pairs) + "}"

    def clear(self) -> None:
        raise NotImplementedError

    def render(self) -> list[str]:
        raise NotImplementedError


class Counter(_Family):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: Mapping[str, Iterable[str]]) -> None:
        if not name.endswith("_total"):
            raise ValueError("counter names end with _total")
        super().__init__(name, help_text, labels)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1, **labels: object) -> None:
        if amount < 0 or not math.isfinite(amount):
            raise ValueError("counter increments must be finite and non-negative")
        if amount == 0:
            return
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0) + amount

    def value(self, **labels: object) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        lines += [f"{self.name}{self._label_text(key)} {_fmt(value)}" for key, value in items]
        return lines


class Histogram(_Family):
    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Mapping[str, Iterable[str]],
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labels)
        bounds = tuple(float(b) for b in buckets)
        if (
            not bounds
            or list(bounds) != sorted(set(bounds))
            or not all(math.isfinite(b) for b in bounds)
        ):
            raise ValueError("histogram buckets must be finite, unique and ascending")
        self._bounds = bounds
        # Per label set: [per-bucket counts (non-cumulative, last is +Inf), sum, count]
        self._series: dict[tuple[str, ...], list[Any]] = {}

    def observe(self, value: float, **labels: object) -> None:
        if not math.isfinite(value):
            raise ValueError("histogram observations must be finite")
        value = max(0.0, float(value))
        key = self._key(labels)
        index = bisect.bisect_left(self._bounds, value)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = [[0] * (len(self._bounds) + 1), 0.0, 0]
                self._series[key] = series
            series[0][index] += 1
            series[1] += value
            series[2] += 1

    def count(self, **labels: object) -> int:
        with self._lock:
            series = self._series.get(self._key(labels))
            return series[2] if series else 0

    def clear(self) -> None:
        with self._lock:
            self._series.clear()

    def render(self) -> list[str]:
        with self._lock:
            items = sorted((k, [list(s[0]), s[1], s[2]]) for k, s in self._series.items())
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} histogram"]
        for key, (counts, total, n) in items:
            running = 0
            for bound, count in zip((*self._bounds, math.inf), counts, strict=True):
                running += count
                le = _fmt(bound)
                lines.append(
                    f"{self.name}_bucket{self._label_text(key, (('le', le),))} {_fmt(running)}"
                )
            lines.append(f"{self.name}_sum{self._label_text(key)} {_fmt(total)}")
            lines.append(f"{self.name}_count{self._label_text(key)} {_fmt(n)}")
        return lines


class Registry:
    def __init__(self) -> None:
        self._families: list[_Family] = []
        self._names: set[str] = set()

    def _add(self, family: _Family) -> _Family:
        if family.name in self._names:
            raise ValueError(f"duplicate metric {family.name}")
        self._names.add(family.name)
        self._families.append(family)
        return family

    def counter(self, name: str, help_text: str, labels: Mapping[str, Iterable[str]]) -> Counter:
        family = Counter(name, help_text, labels)
        self._add(family)
        return family

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: Mapping[str, Iterable[str]],
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> Histogram:
        family = Histogram(name, help_text, labels, buckets)
        self._add(family)
        return family

    def reset(self) -> None:
        """Drop every recorded value; families stay registered. For tests and re-initialisation."""
        for family in self._families:
            family.clear()

    def render(self) -> str:
        """Prometheus text exposition (version 0.0.4). Families with no samples emit HELP/TYPE only."""
        lines: list[str] = []
        for family in self._families:
            lines.extend(family.render())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

TURNS_TOTAL = REGISTRY.counter(
    "edgar_turns_total",
    "DM turns that reached a terminal state, by path, delivery and outcome.",
    {"path": PATHS, "delivery": DELIVERIES, "outcome": TURN_OUTCOMES},
)
TURN_DURATION = REGISTRY.histogram(
    "edgar_turn_duration_seconds",
    "Wall time of committed DM turns (health probes are not included).",
    {"path": PATHS, "delivery": DELIVERIES},
)
TURN_STAGE_DURATION = REGISTRY.histogram(
    "edgar_turn_stage_duration_seconds",
    "Time spent in each turn stage, for committed turns that measured the stage.",
    {"stage": STAGES},
)
TIME_TO_FIRST_TEXT = REGISTRY.histogram(
    "edgar_turn_time_to_first_text_seconds",
    "Time from turn start to the first narration text, for committed turns that streamed text.",
    {"delivery": DELIVERIES},
)
PROVIDER_CALLS_TOTAL = REGISTRY.counter(
    "edgar_provider_calls_total",
    "Provider calls made by turns, by call kind and result.",
    {"kind": CALL_KINDS, "result": PROVIDER_RESULTS},
)
PROVIDER_CALL_DURATION = REGISTRY.histogram(
    "edgar_provider_call_duration_seconds",
    "Wall time of one provider call, by call kind.",
    {"kind": CALL_KINDS},
)
PROVIDER_FAILURES_TOTAL = REGISTRY.counter(
    "edgar_provider_failures_total",
    "Failed provider calls by call kind and closed error category.",
    {"kind": CALL_KINDS, "category": ERROR_CATEGORIES},
)
PROVIDER_RETRIES_TOTAL = REGISTRY.counter(
    "edgar_provider_retries_total",
    "Caller-level retries (attempts beyond the first) by call kind.",
    {"kind": CALL_KINDS},
)
PROVIDER_TOKENS_TOTAL = REGISTRY.counter(
    "edgar_provider_tokens_total",
    "Tokens reported by the provider. Only measured values are added; unknown usage is not zero.",
    {"kind": CALL_KINDS, "type": TOKEN_TYPES},
)
PROVIDER_USAGE_UNKNOWN_TOTAL = REGISTRY.counter(
    "edgar_provider_usage_unknown_calls_total",
    "Provider calls whose input or output token count was not reported.",
    {"kind": CALL_KINDS},
)
PROVIDER_COST_USD_TOTAL = REGISTRY.counter(
    "edgar_provider_estimated_cost_usd_total",
    "Estimated provider cost in USD at configured conservative rates (not a billed amount).",
    {"kind": CALL_KINDS},
)
PROVIDER_COST_UNKNOWN_TOTAL = REGISTRY.counter(
    "edgar_provider_cost_unknown_calls_total",
    "Provider calls with no configured price, so no cost estimate exists.",
    {"kind": CALL_KINDS},
)
RETRIEVAL_TOTAL = REGISTRY.counter(
    "edgar_retrieval_total",
    "Turn retrievals by result; degraded means sources were unavailable for that turn.",
    {"result": RETRIEVAL_RESULTS},
)
RETRIEVAL_DURATION = REGISTRY.histogram(
    "edgar_retrieval_duration_seconds",
    "Wall time of turn retrieval.",
    {},
)
TURN_LOCK_ACQUISITIONS_TOTAL = REGISTRY.counter(
    "edgar_turn_lock_acquisitions_total",
    "Per-session turn lock attempts. A contended attempt is a 409 rejection.",
    {"result": LOCK_RESULTS},
)
TURN_LOCK_WAIT = REGISTRY.histogram(
    "edgar_turn_lock_wait_seconds",
    "Time to acquire the per-session turn lock (one Redis round trip per attempt).",
    {},
)
READINESS_PROBES_TOTAL = REGISTRY.counter(
    "edgar_readiness_probes_total",
    "Readiness probe results for the whole /ready endpoint.",
    {"result": READY_RESULTS},
)
READINESS_CHECKS_TOTAL = REGISTRY.counter(
    "edgar_readiness_checks_total",
    "Readiness results per backing component.",
    {"component": READINESS_COMPONENTS, "result": CHECK_RESULTS},
)
READINESS_CHECK_DURATION = REGISTRY.histogram(
    "edgar_readiness_check_duration_seconds",
    "Latency of readiness checks per component. Health-probe latency, not DM-turn latency.",
    {"component": READINESS_COMPONENTS},
)

# Collectors already recorded (committed or failed). A turn is counted once, whichever path ends it.
_RECORDED: weakref.WeakSet[TurnDiagnostics] = weakref.WeakSet()
_RECORDED_LOCK = threading.Lock()


def _best_effort(operation: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R | None]]:
    """Swallow and log (event name only, never the exception text) so recording cannot fail a turn."""

    def decorator(fn: Callable[_P, _R]) -> Callable[_P, _R | None]:
        @wraps(fn)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R | None:
            try:
                return fn(*args, **kwargs)
            except Exception:  # noqa: BLE001 - metrics must never affect the caller
                log.warning("operational_metrics_record_failed", operation=operation)
                return None

        return wrapper

    return decorator


def _nonneg_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _record_call(call: Mapping[str, Any]) -> None:
    kind = call.get("kind")
    category = call.get("error_category")
    PROVIDER_CALLS_TOTAL.inc(kind=kind, result="error" if category else "ok")
    latency = _nonneg_int(call.get("latency_ms"))
    if latency is not None:
        PROVIDER_CALL_DURATION.observe(latency / 1000, kind=kind)
    if category:
        PROVIDER_FAILURES_TOTAL.inc(kind=kind, category=category)
    attempts = _nonneg_int(call.get("attempts"))
    if attempts is not None and attempts > 1:
        PROVIDER_RETRIES_TOTAL.inc(attempts - 1, kind=kind)
    usage = call.get("usage") if isinstance(call.get("usage"), Mapping) else {}
    for token_type, key in (
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("total", "total_tokens"),
    ):
        tokens = _nonneg_int(usage.get(key))
        if tokens:
            PROVIDER_TOKENS_TOTAL.inc(tokens, kind=kind, type=token_type)
    if (
        _nonneg_int(usage.get("input_tokens")) is None
        or _nonneg_int(usage.get("output_tokens")) is None
    ):
        PROVIDER_USAGE_UNKNOWN_TOTAL.inc(kind=kind)
    cost = _nonneg_int(call.get("cost_microusd"))
    if cost is None:
        PROVIDER_COST_UNKNOWN_TOTAL.inc(kind=kind)
    elif cost:
        PROVIDER_COST_USD_TOTAL.inc(cost / 1_000_000, kind=kind)


def _record_turn_payload(payload: Mapping[str, Any], *, committed: bool) -> None:
    path = payload.get("path")
    delivery = payload.get("delivery")
    TURNS_TOTAL.inc(path=path, delivery=delivery, outcome="committed" if committed else "failed")
    if committed:
        total = _nonneg_int(payload.get("total_ms"))
        if total is not None:
            TURN_DURATION.observe(total / 1000, path=path, delivery=delivery)
        stages = payload.get("stages_ms") if isinstance(payload.get("stages_ms"), Mapping) else {}
        for stage in STAGES:
            ms = _nonneg_int(stages.get(stage))
            if ms is not None:
                TURN_STAGE_DURATION.observe(ms / 1000, stage=stage)
        first_text = _nonneg_int(payload.get("time_to_first_text_ms"))
        if first_text is not None:
            TIME_TO_FIRST_TEXT.observe(first_text / 1000, delivery=delivery)
    retrieval = payload.get("retrieval")
    if isinstance(retrieval, Mapping):
        RETRIEVAL_TOTAL.inc(result="degraded" if retrieval.get("degraded") is True else "ok")
        latency = _nonneg_int(retrieval.get("latency_ms"))
        if latency is not None:
            RETRIEVAL_DURATION.observe(latency / 1000)
    for call in payload.get("provider_calls") or []:
        if isinstance(call, Mapping):
            _record_call(call)


@_best_effort("committed_turn")
def observe_committed_turn(
    telemetry_events: list[dict[str, Any]] | None, collector: TurnDiagnostics | None = None
) -> None:
    """Record a committed turn from the `turn_diagnostics` row that was persisted with it."""
    row = next(
        (
            event.get("payload")
            for event in reversed(telemetry_events or [])
            if event.get("event_type") == TURN_DIAGNOSTICS_EVENT
        ),
        None,
    )
    if not isinstance(row, Mapping):
        return
    with _RECORDED_LOCK:
        if collector is not None:
            if collector in _RECORDED:
                return
            _RECORDED.add(collector)
    _record_turn_payload(row, committed=True)


@_best_effort("uncommitted_turn")
def observe_uncommitted_turn(collector: TurnDiagnostics | None) -> None:
    """Record a turn that ended without a commit, from its partial collector.

    Provider calls and retrieval already made are still counted, because they happened and are the
    evidence for provider failures during an outage. No stage or latency histogram is fed, so the
    latency distributions describe committed turns only.
    """
    if collector is None:
        return
    with _RECORDED_LOCK:
        if collector in _RECORDED:
            return
        _RECORDED.add(collector)
    _record_turn_payload(collector.to_event()["payload"], committed=False)


@_best_effort("turn_lock")
def observe_turn_lock(*, acquired: bool, wait_ms: int | None) -> None:
    TURN_LOCK_ACQUISITIONS_TOTAL.inc(result="acquired" if acquired else "contended")
    wait = _nonneg_int(wait_ms)
    if wait is not None:
        TURN_LOCK_WAIT.observe(wait / 1000)


@_best_effort("readiness_check")
def observe_readiness_check(component: str, *, ok: bool, seconds: float) -> None:
    READINESS_CHECKS_TOTAL.inc(component=component, result="ok" if ok else "unavailable")
    if math.isfinite(seconds):
        READINESS_CHECK_DURATION.observe(seconds, component=component)


@_best_effort("readiness")
def observe_readiness(*, ready: bool) -> None:
    READINESS_PROBES_TOTAL.inc(result="ready" if ready else "unavailable")


def reset_operational_metrics() -> None:
    """Clear every recorded value and the recorded-turn set. Intended for tests."""
    REGISTRY.reset()
    with _RECORDED_LOCK:
        _RECORDED.clear()


def metrics_enabled() -> bool:
    """Exposure gate for GET /metrics, read per request. Off unless explicitly enabled."""
    return os.environ.get(METRICS_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def metrics_token() -> str | None:
    """Optional bearer token that the scraper must present. Never logged or echoed."""
    token = os.environ.get(METRICS_TOKEN_ENV, "").strip()
    return token or None


def render_exposition() -> str:
    return REGISTRY.render()


__all__ = [
    "CONTENT_TYPE",
    "REGISTRY",
    "metrics_enabled",
    "metrics_token",
    "observe_committed_turn",
    "observe_readiness",
    "observe_readiness_check",
    "observe_turn_lock",
    "observe_uncommitted_turn",
    "render_exposition",
    "reset_operational_metrics",
]
