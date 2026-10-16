"""Collector contract (EDG-053): unknown stays unknown, whitelisted, bounded."""

import pytest

from agent.turn_diagnostics import (
    MAX_PROVIDER_CALLS,
    TURN_DIAGNOSTICS_EVENT,
    TurnDiagnostics,
    bind_collector,
    current_collector,
    normalize_usage,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _collector(clock=None, **kw):
    return TurnDiagnostics(
        turn_id="t1", path="normal", delivery="stream", clock=clock or _Clock(), **kw
    )


def test_unmeasured_stage_is_unavailable_not_zero():
    clock = _Clock()
    c = _collector(clock)
    with c.stage("parsing_ms"):
        clock.now += 1.5
    payload = c.to_event()["payload"]
    assert payload["stages_ms"] == {"parsing_ms": 1500}
    assert "narrating_ms" in payload["stages_unavailable"]
    assert "narrating_ms" not in payload["stages_ms"]


def test_missing_usage_and_cost_stay_none_and_totals_flag_incomplete():
    c = _collector()
    c.record_provider_call(
        kind="parse",
        profile="play.parser",
        requested_model="m",
        latency_ms=10,
        usage={"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        cost_microusd=40,
    )
    c.record_provider_call(
        kind="narrate", profile="play.narrator", requested_model="m", latency_ms=9
    )
    totals = c.to_event()["payload"]["totals"]
    assert totals["input_tokens"] == 5
    assert totals["cost_microusd"] == 40
    assert totals["calls_unknown_usage"] == 1
    assert totals["calls_unknown_cost"] == 1
    assert totals["complete"] is False
    second = c.to_event()["payload"]["provider_calls"][1]
    assert second["usage"] == {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    assert second["cost_microusd"] is None


def test_no_calls_gives_none_totals_not_zero():
    totals = _collector().to_event()["payload"]["totals"]
    assert totals["input_tokens"] is None and totals["cost_microusd"] is None
    assert totals["complete"] is False


def test_usage_rejects_bools_floats_negatives_and_extra_keys():
    assert normalize_usage(
        {"input_tokens": True, "output_tokens": 1.5, "total_tokens": -1, "prompt": "secret"}
    ) == {"input_tokens": None, "output_tokens": None, "total_tokens": None}


def test_error_category_is_a_closed_vocabulary():
    c = _collector()
    c.record_provider_call(
        kind="bogus",
        profile="p",
        requested_model=None,
        latency_ms=1,
        error_category="provider said: connection refused at 10.0.0.7",
    )
    call = c.to_event()["payload"]["provider_calls"][0]
    assert call["error_category"] == "unknown"
    assert call["kind"] == "other"


def test_provider_calls_are_bounded():
    c = _collector()
    for _ in range(MAX_PROVIDER_CALLS + 3):
        c.record_provider_call(kind="parse", profile="p", requested_model="m", latency_ms=1)
    payload = c.to_event()["payload"]
    assert len(payload["provider_calls"]) == MAX_PROVIDER_CALLS
    assert payload["totals"]["calls_dropped"] == 3


def test_first_text_is_recorded_once_and_event_shape():
    clock = _Clock()
    c = _collector(clock)
    clock.now += 0.25
    c.record_first_text()
    clock.now += 1
    c.record_first_text()
    event = c.to_event()
    assert event["event_type"] == TURN_DIAGNOSTICS_EVENT
    assert event["payload"]["time_to_first_text_ms"] == 250
    assert event["payload"]["total_ms"] == 1250


def test_bind_and_unknown_stage():
    c = _collector()
    assert current_collector() is None
    with bind_collector(c):
        assert current_collector() is c
    assert current_collector() is None
    with pytest.raises(ValueError):
        c.record_stage_ms("bogus_ms", 1)
