"""Fast, offline checks of the EDG-071 capacity harness invariants (no Docker, no provider, no DB).

The harness runs at small scale with short fixture delays. The assertions are structural: they
must hold for any delay, and they do not compare wall-clock timings. A negative control proves the
held-across-provider detector can fire, so the zero-violation assertions are not vacuous.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.capacity_harness import (
    _CURRENT,
    ALL_SCENARIOS,
    GENERATION_CONCURRENCY,
    SCHEMA,
    Run,
    Timing,
    Unit,
    format_table,
    run_harness,
)

LEVELS = (1, 2, 4)
FAST = Timing(provider_ms=2.0, chunk_ms=0.5, statement_ms=0.2, probe_external_ms=2.0, probe_interval_ms=2.0)
SLOW = Timing(provider_ms=60.0, chunk_ms=4.0, statement_ms=0.2, probe_external_ms=2.0, probe_interval_ms=2.0)
ROW_KEYS = {
    "concurrency",
    "units",
    "completed",
    "failed",
    "wall_s",
    "latency_ms",
    "ttft_ms",
    "provider_calls",
    "max_inflight_provider_calls",
    "max_inflight_provider_calls_per_unit",
    "peak_connection_demand",
    "peak_connections_held",
    "max_connections_held_per_unit",
    "connection_wait_ms",
    "connection_acquires",
    "connection_acquires_queued",
    "pool_cap",
    "held_across_provider_violations",
    "peak_open_sessions",
    "statements",
    "probes_completed",
    "event_loop_lag_ms",
}


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("capacity") / "result.json"
    return run_harness(levels=LEVELS, timing=FAST, out=out)


def test_every_unit_completes_in_every_scenario(result):
    for scenario, rows in result["scenarios"].items():
        for row in rows:
            assert row["failed"] == 0, (scenario, row)
            assert row["completed"] == row["units"] == row["concurrency"], (scenario, row)


def test_no_connection_is_held_while_a_provider_call_is_pending(result):
    for scenario, rows in result["scenarios"].items():
        for row in rows:
            assert row["held_across_provider_violations"] == 0, (scenario, row)


def test_each_unit_holds_at_most_one_connection_at_a_time(result):
    for scenario, rows in result["scenarios"].items():
        for row in rows:
            assert row["max_connections_held_per_unit"] <= 1, (scenario, row)


def test_the_connection_detector_fires_on_a_session_held_across_a_provider_wait():
    """Negative control: a deliberately bad unit must be counted, or the zero assertions prove nothing."""

    async def held_across_provider() -> int:
        run = Run(FAST, pool_cap=None, statement_s=0.0)
        unit = Unit(name="negative-control")
        token = _CURRENT.set(unit)
        try:
            async with run.factory() as db:
                await db.execute(None)  # connection taken
                await run.chat.ainvoke([])  # provider call while the connection is still held
                await db.commit()
        finally:
            _CURRENT.reset(token)
        return unit.violations

    assert asyncio.run(held_across_provider()) == 1


def test_peak_connection_demand_does_not_grow_with_provider_delay():
    """Provider delay changes the length of the provider wait, not the number of connections demanded."""
    fast = run_harness(levels=(4,), scenarios=("runner", "route"), timing=FAST)
    slow = run_harness(levels=(4,), scenarios=("runner", "route"), timing=SLOW)
    for scenario in ("runner", "route"):
        fast_row, slow_row = fast["scenarios"][scenario][0], slow["scenarios"][scenario][0]
        assert slow_row["held_across_provider_violations"] == 0
        assert slow_row["peak_connection_demand"] <= 4
        assert abs(slow_row["peak_connection_demand"] - fast_row["peak_connection_demand"]) <= 1, (
            scenario,
            fast_row["peak_connection_demand"],
            slow_row["peak_connection_demand"],
        )
    # The route loader finishes before any provider call, so its demand cannot depend on provider delay.
    assert fast["scenarios"]["route"][0]["peak_connection_demand"] <= 4
    assert slow["scenarios"]["route"][0]["peak_connection_demand"] <= 4


def test_summary_jobs_never_hold_a_connection_during_generation(result):
    row = result["scenarios"]["summary"][-1]
    assert row["concurrency"] == LEVELS[-1]
    assert row["held_across_provider_violations"] == 0
    assert row["provider_calls"] == row["concurrency"]


def test_generation_is_bounded_per_generation_but_not_globally(result):
    rows = result["scenarios"]["generation"]
    for row in rows:
        assert row["max_inflight_provider_calls_per_unit"] == GENERATION_CONCURRENCY
        assert row["max_inflight_provider_calls"] == GENERATION_CONCURRENCY * row["concurrency"]
        assert row["peak_connection_demand"] == 0  # generation has no database work in this path


def test_a_small_pool_queues_sessions_without_exceeding_the_cap():
    capped = run_harness(levels=(4,), scenarios=("route",), timing=FAST, pool_cap=2)
    row = capped["scenarios"]["route"][0]
    assert row["peak_connections_held"] <= 2
    assert row["peak_connection_demand"] > 2
    assert row["connection_acquires_queued"] > 0
    assert row["completed"] == row["concurrency"]


def test_result_schema_is_stable_and_round_trips(result, tmp_path):
    assert result["schema"] == SCHEMA == "edgar.capacity_harness/1"
    assert set(result) == {"schema", "measures", "config", "environment", "scenarios", "sizing_inputs"}
    assert result["measures"]["production_benchmark"] is False
    assert result["measures"]["container_resources"] is False
    assert set(result["scenarios"]) == set(ALL_SCENARIOS)
    for rows in result["scenarios"].values():
        assert [row["concurrency"] for row in rows] == list(LEVELS)
        for row in rows:
            assert set(row) == ROW_KEYS
    out = tmp_path / "result.json"
    run_harness(levels=(1,), scenarios=("summary",), timing=FAST, out=out)
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == SCHEMA


def test_table_has_one_row_per_scenario_and_level(result):
    table = format_table(result)
    body = table.splitlines()[2:]
    assert len(body) == len(ALL_SCENARIOS) * len(LEVELS)


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError):
        run_harness(levels=(1,), scenarios=("nonsense",), timing=FAST)
