"""Stored turn rows: whitelisted reads, unknown-vs-zero summaries, comparable normal/combat rows (EDG-053)."""

from __future__ import annotations

import pytest

from agent.combat_telemetry import COMBAT_ROUND_EVENT
from agent.turn_diagnostics import (
    STAGES,
    TurnDiagnostics,
    bind_collector,
    bound_steps,
    current_collector,
    finalize_turn_diagnostics,
    new_turn_diagnostics,
    timed_node,
    turn_diagnostics_public,
    turn_diagnostics_summary,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def _row(*, path="normal", delivery="sync", total=100, ttft=None, calls=None, **kw):
    collector = TurnDiagnostics(
        turn_id="t", path=path, delivery=delivery, clock=_Clock()
    )
    for call in calls or []:
        collector.record_provider_call(**call)
    if ttft is not None:
        collector._first_text_ms = ttft  # deterministic: the clock is fake, value is the point
    # Exact integer milliseconds: a fake clock that reads `total` ms (plus a float guard).
    collector._started = 0.0
    collector._clock = lambda: total / 1000 + 1e-6
    return collector.to_event()["payload"]


def _call(**overrides):
    base = dict(kind="parse", profile="play.parser", requested_model="m", latency_ms=5)
    base.update(overrides)
    return base


def test_public_projection_drops_everything_outside_the_whitelist() -> None:
    payload = _row(
        calls=[_call(usage={"input_tokens": 3, "output_tokens": 1})],
    )
    payload["provider_calls"][0]["prompt"] = "secret prompt text"
    payload["provider_calls"][0]["headers"] = {"authorization": "x"}
    payload["stages_ms"]["source_reference"] = 1
    payload["combatants"] = [{"creature_id": "goblin-1", "source_reference": "MM p. 1"}]
    payload["raw_response"] = "provider payload"

    public = turn_diagnostics_public(payload)
    dumped = repr(public)

    for leaked in ("secret prompt text", "authorization", "creature_id", "source_reference",
                   "provider payload", "raw_response", "combatants"):
        assert leaked not in dumped
    assert set(public["stages_ms"]) <= set(STAGES)
    assert public["scope"] == "dm_turn"
    assert public["provider_calls"][0]["usage"] == {
        "input_tokens": 3,
        "output_tokens": 1,
        "total_tokens": None,
    }


def test_unmeasured_public_fields_stay_null_and_listed_unavailable() -> None:
    public = turn_diagnostics_public(_row(calls=[_call()]))

    assert public["time_to_first_text_ms"] is None
    assert public["retrieval"] is None
    assert public["lock"] is None
    assert public["provider_calls"][0]["cost_microusd"] is None
    assert public["provider_calls"][0]["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    assert set(public["stages_unavailable"]) == set(STAGES)


def test_summary_separates_unknown_from_zero_and_labels_partial_totals() -> None:
    complete_zero = _row(
        total=40,
        calls=[_call(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                     cost_microusd=0)],
    )
    partial = _row(
        total=200,
        path="combat",
        delivery="stream",
        calls=[_call(usage={"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}),
               _call(kind="narrate", latency_ms=9, error_category="timeout")],
    )
    unknown = _row(total=120, calls=[_call()])

    summary = turn_diagnostics_summary(
        [turn_diagnostics_public(p) for p in (complete_zero, partial, unknown)]
    )

    assert summary["turns"] == 3
    assert summary["complete_turns"] == 1
    assert summary["by_path"] == {"normal": 2, "combat": 1}
    assert summary["by_delivery"] == {"sync": 2, "stream": 1}
    # Zero is a real measured count; the unknown turn contributes nothing to the sum.
    assert summary["provider"]["input_tokens"] == 7
    assert summary["provider"]["calls_unknown_usage"] == 2
    assert summary["provider"]["cost_microusd"] == 0
    assert summary["provider"]["calls_unknown_cost"] == 3
    assert summary["provider"]["errors_by_category"] == {"timeout": 1}
    assert summary["time_to_first_text_ms"] == {"count": 0, "median": None, "p95": None}
    assert summary["total_ms"]["median"] == 120
    assert summary["total_ms"]["p95"] == 200


def test_summary_of_nothing_is_unknown_not_zero() -> None:
    summary = turn_diagnostics_summary([])
    assert summary["turns"] == 0
    assert summary["total_ms"] == {"count": 0, "median": None, "p95": None}
    assert summary["provider"]["input_tokens"] is None
    assert summary["provider"]["cost_microusd"] is None


def test_summary_median_and_p95_use_known_values_only() -> None:
    totals = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    payloads = [turn_diagnostics_public(_row(total=t)) for t in totals]
    summary = turn_diagnostics_summary(payloads)
    assert summary["total_ms"] == {"count": 10, "median": 550, "p95": 1000}


def test_combat_round_timings_are_adopted_under_the_shared_stage_names() -> None:
    collector = TurnDiagnostics(turn_id="t", path="normal", delivery="stream")
    collector.record_stage_ms("parsing_ms", 30)
    combat_row = {
        "event_type": COMBAT_ROUND_EVENT,
        "payload": {
            "timings_ms": {"retrieving_ms": 11, "adjudicating_ms": 22, "narrating_ms": 33},
            "timings_unavailable": ["x"],
        },
    }
    event = finalize_turn_diagnostics(collector, [combat_row])["payload"]

    assert event["path"] == "combat"
    assert event["stages_ms"] == {
        "parsing_ms": 30,
        "retrieving_ms": 11,
        "adjudicating_ms": 22,
        "narrating_ms": 33,
    }
    assert event["stages_unavailable"] == ["lock_wait_ms", "saving_ms"]
    assert "combat_ms" not in event["stages_ms"]


def test_normal_and_combat_rows_share_the_same_stage_vocabulary() -> None:
    normal = TurnDiagnostics(turn_id="n", path="normal", delivery="sync")
    normal.record_stage_ms("narrating_ms", 5)
    combat = TurnDiagnostics(turn_id="c", path="normal", delivery="sync")
    finalize_turn_diagnostics(
        combat,
        [{"event_type": COMBAT_ROUND_EVENT, "payload": {"timings_ms": {"narrating_ms": 5}}}],
    )
    n = turn_diagnostics_public(normal.to_event()["payload"])
    c = turn_diagnostics_public(combat.to_event()["payload"])
    assert set(n) == set(c)
    assert set(n["stages_ms"]) | set(n["stages_unavailable"]) == set(STAGES)
    assert set(c["stages_ms"]) | set(c["stages_unavailable"]) == set(STAGES)


def test_sync_and_stream_rows_differ_only_by_delivery() -> None:
    sync = TurnDiagnostics(turn_id="t", path="normal", delivery="sync").to_event()["payload"]
    stream = TurnDiagnostics(turn_id="t", path="normal", delivery="stream").to_event()["payload"]
    assert set(sync) == set(stream)
    assert (sync["delivery"], stream["delivery"]) == ("sync", "stream")


def test_collector_built_for_a_turn_records_the_lock_wait() -> None:
    collector = new_turn_diagnostics(turn_id="t", delivery="stream", lock_wait_ms=3)
    lock = collector.to_event()["payload"]["lock"]
    assert lock == {"wait_ms": 3, "contended": False, "attempts": None}
    assert new_turn_diagnostics(turn_id="t", delivery="sync").to_event()["payload"]["lock"] is None


@pytest.mark.asyncio
async def test_timed_node_charges_its_stage_only_when_a_collector_is_bound() -> None:
    async def node(state):
        return {"ran": state["x"]}

    wrapped = timed_node("parsing_ms", node)
    assert await wrapped({"x": 1}) == {"ran": 1}

    collector = TurnDiagnostics(turn_id="t", path="normal", delivery="sync")
    with bind_collector(collector):
        assert await wrapped({"x": 2}) == {"ran": 2}
    assert "parsing_ms" in collector.to_event()["payload"]["stages_ms"]


@pytest.mark.asyncio
async def test_timed_node_still_records_time_when_the_node_raises() -> None:
    async def failing(_state):
        raise ValueError("node failed")

    collector = TurnDiagnostics(turn_id="t", path="normal", delivery="sync")
    with bind_collector(collector), pytest.raises(ValueError):
        await timed_node("adjudicating_ms", failing)({})
    assert "adjudicating_ms" in collector.to_event()["payload"]["stages_ms"]


@pytest.mark.asyncio
async def test_bound_steps_binds_only_around_awaits_never_across_yields() -> None:
    collector = TurnDiagnostics(turn_id="t", path="normal", delivery="stream")
    seen_inside: list[object] = []

    async def steps():
        seen_inside.append(current_collector())
        yield "first"
        seen_inside.append(current_collector())
        yield "second"

    outside: list[object] = []
    async for item in bound_steps(steps(), collector):
        outside.append(current_collector())
        assert item in {"first", "second"}

    assert seen_inside == [collector, collector]
    assert outside == [None, None]
    assert current_collector() is None
