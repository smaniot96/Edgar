"""Operational metrics (EDG-139): registry, exposition, bounded labels, recording, gating. No Docker.

Turn recording is exercised through the streamed turn runner with the same fakes as
tests/api/test_turn_diagnostics_runner.py. Postgres-backed router paths are not run here.
"""

from __future__ import annotations

import re
import threading

import pytest
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient

from agent import provider_trace
from agent.models.parsed_input import ParsedInput
from agent.turn_diagnostics import (
    CALL_KINDS,
    ERROR_CATEGORIES,
    TURN_DIAGNOSTICS_EVENT,
    TurnDiagnostics,
)
from apps.api.services import operational_metrics as om, turn_runner
from tests.api.test_turn_diagnostics_runner import (
    _DelayedStreamingNarrator,
    _done,
    _prepare_app,
    _SessionCounter,
    _stream,
)

ALLOWED_LABELS = {
    "path",
    "delivery",
    "outcome",
    "stage",
    "kind",
    "result",
    "category",
    "type",
    "component",
    "le",
}
SAMPLE = re.compile(
    r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"'
    r'(,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*")*\})? (-?[0-9.]+(e[-+]?[0-9]+)?|\+Inf|-Inf)$'
)


@pytest.fixture(autouse=True)
def _fresh_metrics():
    om.reset_operational_metrics()
    yield
    om.reset_operational_metrics()


def _row(collector: TurnDiagnostics) -> list[dict]:
    return [{"event_type": TURN_DIAGNOSTICS_EVENT, "payload": collector.to_event()["payload"]}]


def _collector(*, delivery: str = "sync", turn_id: str = "turn-secret-42") -> TurnDiagnostics:
    return TurnDiagnostics(turn_id=turn_id, path="normal", delivery=delivery)


def _label_names(text: str) -> set[str]:
    return set(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="', text))


# --- Registry -----------------------------------------------------------------------------------


def test_counter_increments_per_label_set_and_rejects_negative() -> None:
    registry = om.Registry()
    c = registry.counter("x_things_total", "Things.", {"kind": ("a", "b")})
    c.inc(kind="a")
    c.inc(2, kind="a")
    c.inc(kind="b")
    assert c.value(kind="a") == 3
    assert c.value(kind="b") == 1
    with pytest.raises(ValueError):
        c.inc(-1, kind="a")


def test_unknown_label_value_is_bucketed_as_other_never_a_new_series() -> None:
    registry = om.Registry()
    c = registry.counter("x_things_total", "Things.", {"kind": ("a",)})
    c.inc(kind="session-42")
    c.inc(kind="prompt: tell me a secret")
    text = registry.render()
    assert 'x_things_total{kind="other"} 2' in text
    assert "session-42" not in text
    assert "secret" not in text


def test_label_names_must_match_the_declared_set() -> None:
    registry = om.Registry()
    c = registry.counter("x_things_total", "Things.", {"kind": ("a",)})
    with pytest.raises(ValueError):
        c.inc(session_id="7")
    with pytest.raises(ValueError):
        c.inc()


def test_counter_names_must_end_in_total_and_le_is_reserved() -> None:
    registry = om.Registry()
    with pytest.raises(ValueError):
        registry.counter("x_things", "No suffix.", {})
    with pytest.raises(ValueError):
        registry.histogram("x_seconds", "Reserved.", {"le": ("a",)})


def test_histogram_buckets_are_cumulative_and_end_at_count() -> None:
    registry = om.Registry()
    h = registry.histogram("x_seconds", "Latency.", {}, buckets=(0.1, 1.0))
    for value in (0.05, 0.5, 5.0):
        h.observe(value)
    text = registry.render()
    assert 'x_seconds_bucket{le="0.1"} 1' in text
    assert 'x_seconds_bucket{le="1.0"} 2' in text
    assert 'x_seconds_bucket{le="+Inf"} 3' in text
    assert "x_seconds_count 3" in text
    assert h.count() == 3


def test_histogram_rejects_non_finite_and_clamps_negative() -> None:
    registry = om.Registry()
    h = registry.histogram("x_seconds", "Latency.", {}, buckets=(1.0,))
    with pytest.raises(ValueError):
        h.observe(float("nan"))
    h.observe(-3.0)
    assert h.count() == 1


def test_reset_clears_values_and_keeps_families() -> None:
    registry = om.Registry()
    c = registry.counter("x_things_total", "Things.", {"kind": ("a",)})
    c.inc(kind="a")
    registry.reset()
    assert c.value(kind="a") == 0
    assert "# TYPE x_things_total counter" in registry.render()
    assert "x_things_total{" not in registry.render()


def test_concurrent_increments_from_threads_are_not_lost() -> None:
    registry = om.Registry()
    c = registry.counter("x_things_total", "Things.", {"kind": ("a",)})
    h = registry.histogram("x_seconds", "Latency.", {}, buckets=(1.0,))

    def work() -> None:
        for _ in range(2000):
            c.inc(kind="a")
            h.observe(0.5)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value(kind="a") == 16000
    assert h.count() == 16000


# --- Exposition format --------------------------------------------------------------------------


def _assert_valid_exposition(text: str) -> None:
    assert text.endswith("\n")
    helped, typed = set(), set()
    for line in text.splitlines():
        if line.startswith("# HELP "):
            helped.add(line.split()[2])
        elif line.startswith("# TYPE "):
            typed.add(line.split()[2])
        else:
            assert SAMPLE.match(line), f"invalid sample line: {line!r}"
    assert helped == typed
    assert all(name.startswith("edgar_") for name in typed)


def test_full_registry_renders_valid_prometheus_text_even_when_empty() -> None:
    _assert_valid_exposition(om.render_exposition())


def test_rendered_exposition_after_recording_is_valid_and_label_bounded() -> None:
    collector = _collector(delivery="stream", turn_id="turn-secret-42")
    collector.record_provider_call(
        kind="narrate",
        profile="play.narrator",
        requested_model="gpt-6.1-sol",
        latency_ms=120,
        usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        cost_microusd=96,
        attempts=2,
    )
    collector.record_provider_call(
        kind="parse", profile="x", requested_model=None, latency_ms=30, error_category="timeout"
    )
    collector.record_retrieval(latency_ms=40, rules_chunks=3, adventure_chunks=1, degraded=True)
    om.observe_committed_turn(_row(collector), collector)

    text = om.render_exposition()
    _assert_valid_exposition(text)
    assert _label_names(text) <= ALLOWED_LABELS
    assert "turn-secret-42" not in text
    assert "play.narrator" not in text
    assert "gpt-6.1-sol" not in text
    assert 'edgar_provider_failures_total{kind="parse",category="timeout"} 1' in text


# --- Turn recording -----------------------------------------------------------------------------


def test_committed_turn_feeds_turn_provider_and_retrieval_series() -> None:
    collector = _collector(delivery="sync")
    collector.record_stage_ms("narrating_ms", 250)
    collector.record_provider_call(
        kind="narrate",
        profile="play.narrator",
        requested_model="gpt-6.1-sol",
        latency_ms=250,
        usage={"input_tokens": 40, "output_tokens": 9, "total_tokens": 49},
        cost_microusd=340,
        attempts=1,
    )
    collector.record_retrieval(latency_ms=12, rules_chunks=2, adventure_chunks=0, degraded=False)

    om.observe_committed_turn(_row(collector), collector)

    assert om.TURNS_TOTAL.value(path="normal", delivery="sync", outcome="committed") == 1
    assert om.TURN_DURATION.count(path="normal", delivery="sync") == 1
    assert om.TURN_STAGE_DURATION.count(stage="narrating_ms") == 1
    # Stages that were not measured are absent, not observed as zero.
    assert om.TURN_STAGE_DURATION.count(stage="retrieving_ms") == 0
    assert om.TIME_TO_FIRST_TEXT.count(delivery="sync") == 0
    assert om.PROVIDER_CALLS_TOTAL.value(kind="narrate", result="ok") == 1
    assert om.PROVIDER_TOKENS_TOTAL.value(kind="narrate", type="input") == 40
    assert om.PROVIDER_COST_USD_TOTAL.value(kind="narrate") == pytest.approx(0.00034)
    assert om.RETRIEVAL_TOTAL.value(result="ok") == 1
    assert om.RETRIEVAL_DURATION.count() == 1


def test_unknown_usage_is_counted_separately_and_never_zero_filled() -> None:
    collector = _collector()
    collector.record_provider_call(
        kind="narrate", profile="p", requested_model=None, latency_ms=10, usage=None
    )
    om.observe_committed_turn(_row(collector), collector)

    assert om.PROVIDER_USAGE_UNKNOWN_TOTAL.value(kind="narrate") == 1
    assert om.PROVIDER_COST_UNKNOWN_TOTAL.value(kind="narrate") == 1
    text = om.render_exposition()
    # No token or cost sample at all: an absent measurement must not read as zero tokens or zero cost.
    assert "edgar_provider_tokens_total{" not in text
    assert "edgar_provider_estimated_cost_usd_total{" not in text


def test_retries_count_attempts_beyond_the_first() -> None:
    collector = _collector()
    collector.record_provider_call(
        kind="adjudicate", profile="p", requested_model=None, latency_ms=5, attempts=3
    )
    om.observe_committed_turn(_row(collector), collector)
    assert om.PROVIDER_RETRIES_TOTAL.value(kind="adjudicate") == 2


def test_uncommitted_turn_still_counts_provider_failures_but_no_latency() -> None:
    collector = _collector(delivery="stream")
    collector.record_provider_call(
        kind="narrate",
        profile="p",
        requested_model=None,
        latency_ms=900,
        error_category="unavailable",
    )
    om.observe_uncommitted_turn(collector)

    assert om.TURNS_TOTAL.value(path="normal", delivery="stream", outcome="failed") == 1
    assert om.TURNS_TOTAL.value(path="normal", delivery="stream", outcome="committed") == 0
    assert om.PROVIDER_FAILURES_TOTAL.value(kind="narrate", category="unavailable") == 1
    assert om.TURN_DURATION.count(path="normal", delivery="stream") == 0
    assert om.PROVIDER_CALL_DURATION.count(kind="narrate") == 1


def test_a_turn_is_counted_once_even_if_both_paths_see_it() -> None:
    collector = _collector()
    collector.record_provider_call(kind="narrate", profile="p", requested_model=None, latency_ms=5)
    om.observe_committed_turn(_row(collector), collector)
    om.observe_uncommitted_turn(collector)
    om.observe_committed_turn(_row(collector), collector)

    assert om.TURNS_TOTAL.value(path="normal", delivery="sync", outcome="committed") == 1
    assert om.TURNS_TOTAL.value(path="normal", delivery="sync", outcome="failed") == 0
    assert om.PROVIDER_CALLS_TOTAL.value(kind="narrate", result="ok") == 1


def test_provider_kind_and_error_vocabularies_are_closed() -> None:
    collector = _collector()
    collector.record_provider_call(
        kind="session-99",
        profile="p",
        requested_model=None,
        latency_ms=5,
        error_category="free text",
    )
    om.observe_uncommitted_turn(collector)
    assert om.PROVIDER_CALLS_TOTAL.value(kind="other", result="error") == 1
    assert om.PROVIDER_FAILURES_TOTAL.value(kind="other", category="unknown") == 1
    assert set(CALL_KINDS) <= set(om.PROVIDER_CALLS_TOTAL._allowed["kind"])
    assert set(ERROR_CATEGORIES) <= set(om.PROVIDER_FAILURES_TOTAL._allowed["category"])


def test_metrics_fault_never_propagates_to_the_caller(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("secret detail that must not be logged 10.0.0.7")

    monkeypatch.setattr(om, "_record_turn_payload", boom)
    collector = _collector()
    assert om.observe_committed_turn(_row(collector), collector) is None
    assert om.observe_uncommitted_turn(collector) is None


@pytest.mark.asyncio
async def test_metrics_fault_never_fails_a_streamed_turn(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("provider said: connection refused at 10.0.0.7")

    monkeypatch.setattr(om, "_record_turn_payload", boom)
    frames = await _streamed_turn_frames(monkeypatch)
    assert any(f.startswith("event: done\n") for f in frames)
    assert om.TURNS_TOTAL.value(path="normal", delivery="stream", outcome="committed") == 0


# --- Turn through the streamed runner (fakes only) ----------------------------------------------


async def _streamed_turn_frames(monkeypatch) -> list[str]:
    import db.postgres.session as db_session_module

    counter = _SessionCounter()
    monkeypatch.setattr(db_session_module, "async_session_factory", counter)
    monkeypatch.setattr(turn_runner, "deterministic_narration", lambda _state: None)
    monkeypatch.setattr(turn_runner, "actions_for_state", lambda _state: [])
    monkeypatch.setattr(turn_runner, "build_narrator_prompt", lambda _state: [])
    observed: list[int] = []
    monkeypatch.setattr(
        turn_runner, "make_narrator_model", lambda: _DelayedStreamingNarrator(counter, observed)
    )
    # Pin the requested model so the estimated cost is deterministic (the configured price applies).
    monkeypatch.setattr(provider_trace, "_requested_model", lambda _profile: "gpt-6.1-sol")
    values = {"player_input": "I look.", "adjudication_result": None, "turn_id": "turn-7"}
    monkeypatch.setattr(
        turn_runner,
        "prepare_app",
        _prepare_app(
            ("updates", {"input_parser": {"parsed_input": ParsedInput(intent="exploration")}}),
            ("values", values),
        ),
    )
    monkeypatch.setattr(turn_runner, "persist_turn", _done({}))
    return await _stream()


@pytest.mark.asyncio
async def test_streamed_turn_updates_the_expected_series_without_high_cardinality_labels(
    monkeypatch,
) -> None:
    frames = await _streamed_turn_frames(monkeypatch)

    assert any(f.startswith("event: done\n") for f in frames)
    assert om.TURNS_TOTAL.value(path="normal", delivery="stream", outcome="committed") == 1
    assert om.TIME_TO_FIRST_TEXT.count(delivery="stream") == 1
    assert om.PROVIDER_CALLS_TOTAL.value(kind="narrate", result="ok") == 1
    assert om.PROVIDER_TOKENS_TOTAL.value(kind="narrate", type="input") == 40
    assert om.PROVIDER_TOKENS_TOTAL.value(kind="narrate", type="output") == 9
    # 40 input tokens at 4 microUSD plus 9 output tokens at 20 microUSD (configured rates).
    assert om.PROVIDER_COST_USD_TOTAL.value(kind="narrate") == pytest.approx(0.00034)

    text = om.render_exposition()
    _assert_valid_exposition(text)
    assert _label_names(text) <= ALLOWED_LABELS
    assert "turn-7" not in text
    assert "I look." not in text
    assert "The door creaks." not in text


# --- Turn lock, readiness and exposure gate -----------------------------------------------------


@pytest.mark.asyncio
async def test_lock_contention_increments_the_rejection_series_and_wait_histogram() -> None:
    from apps.api.dependencies import acquire_turn_lock

    redis = FakeAsyncRedis(decode_responses=True)
    first = await acquire_turn_lock(11, redis)
    assert first is not None
    second = await acquire_turn_lock(11, redis)  # same session while the first turn holds the lock
    assert second is None

    assert om.TURN_LOCK_ACQUISITIONS_TOTAL.value(result="acquired") == 1
    assert om.TURN_LOCK_ACQUISITIONS_TOTAL.value(result="contended") == 1
    assert om.TURN_LOCK_WAIT.count() == 2
    await first.release()
    await redis.aclose()


@pytest.mark.asyncio
async def test_readiness_probe_observations_separate_components_and_outcomes() -> None:
    from apps.api.routers import health

    async def ok() -> None:
        return None

    async def down() -> None:
        raise OSError("connection refused at 10.0.0.7")

    assert await health._probe("redis", ok()) == ("redis", "ok")
    assert await health._probe("qdrant", down()) == ("qdrant", "unavailable")

    assert om.READINESS_CHECKS_TOTAL.value(component="redis", result="ok") == 1
    assert om.READINESS_CHECKS_TOTAL.value(component="qdrant", result="unavailable") == 1
    assert om.READINESS_CHECK_DURATION.count(component="qdrant") == 1
    om.observe_readiness(ready=False)
    assert om.READINESS_PROBES_TOTAL.value(result="unavailable") == 1
    # Probe latency lives in its own family: no DM-turn duration sample was created.
    assert om.TURN_DURATION.count(path="normal", delivery="sync") == 0


async def _client() -> AsyncClient:
    from apps.api.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_metrics_endpoint_is_404_unless_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv(om.METRICS_ENABLED_ENV, raising=False)
    monkeypatch.delenv(om.METRICS_TOKEN_ENV, raising=False)
    async with await _client() as client:
        response = await client.get("/metrics")
    assert response.status_code == 404
    assert "edgar_" not in response.text


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_exposition_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv(om.METRICS_ENABLED_ENV, "1")
    monkeypatch.delenv(om.METRICS_TOKEN_ENV, raising=False)
    om.observe_readiness(ready=True)
    async with await _client() as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    _assert_valid_exposition(response.text)
    assert 'edgar_readiness_probes_total{result="ready"} 1' in response.text


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_the_configured_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv(om.METRICS_ENABLED_ENV, "true")
    monkeypatch.setenv(om.METRICS_TOKEN_ENV, "scrape-token-for-tests")
    async with await _client() as client:
        missing = await client.get("/metrics")
        wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
        scheme = await client.get(
            "/metrics", headers={"Authorization": "Basic scrape-token-for-tests"}
        )
        right = await client.get(
            "/metrics", headers={"Authorization": "Bearer scrape-token-for-tests"}
        )
    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert scheme.status_code == 403
    assert right.status_code == 200
    assert "scrape-token-for-tests" not in right.text


def test_gate_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv(om.METRICS_ENABLED_ENV, raising=False)
    assert om.metrics_enabled() is False
    monkeypatch.setenv(om.METRICS_ENABLED_ENV, "0")
    assert om.metrics_enabled() is False
    monkeypatch.setenv(om.METRICS_ENABLED_ENV, " YES ")
    assert om.metrics_enabled() is True


@pytest.mark.asyncio
async def test_sync_turn_row_feeds_the_committed_series(monkeypatch) -> None:
    """The sync runner's persisted row is recorded by the same committed path as the stream runner."""

    class FakeApp:
        async def ainvoke(self, _initial_state):
            return {"narration": "A quiet room."}

    monkeypatch.setattr(turn_runner, "app", FakeApp())
    result = await turn_runner.run_turn({"player_input": "I look.", "turn_id": "t-sync"})
    collector_row = next(
        e for e in result["telemetry_events"] if e.get("event_type") == TURN_DIAGNOSTICS_EVENT
    )
    assert collector_row["payload"]["delivery"] == "sync"

    om.observe_committed_turn(result["telemetry_events"], None)

    assert om.TURNS_TOTAL.value(path="normal", delivery="sync", outcome="committed") == 1
    assert om.TURN_DURATION.count(path="normal", delivery="sync") == 1
    assert "t-sync" not in om.render_exposition()
