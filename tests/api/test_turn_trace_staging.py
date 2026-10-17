"""stage_turn_trace chooses the narrator profile from the turn's path and respects opt-in (EDG-138)."""

from agent.turn_diagnostics import TURN_DIAGNOSTICS_EVENT
from agent.turn_trace import TURN_TRACE_EVENT
from apps.api.services.turn_runner import stage_turn_trace


def _result(path: str) -> dict:
    return {
        "turn_id": "t-1",
        "rules_edition": "2014",
        "source_mode": "module",
        "telemetry_events": [
            {"event_type": TURN_DIAGNOSTICS_EVENT, "payload": {"path": path, "turn_id": "t-1"}}
        ],
    }


def _trace(staged: dict) -> dict:
    (event,) = [e for e in staged["telemetry_events"] if e["event_type"] == TURN_TRACE_EVENT]
    return event["payload"]


def test_combat_turn_records_the_combat_narrator_profile():
    staged = stage_turn_trace(_result("combat"), debug=True, delivery="stream")
    assert _trace(staged)["settings"]["narrator_profile"] == "play.combat_narrator"


def test_normal_turn_records_the_narrator_profile():
    staged = stage_turn_trace(_result("normal"), debug=True, delivery="sync")
    assert _trace(staged)["settings"]["narrator_profile"] == "play.narrator"


def test_no_trace_without_debug_opt_in_or_turn_id():
    assert stage_turn_trace(_result("normal"), debug=False, delivery="sync") == _result("normal")
    no_id = {**_result("normal"), "turn_id": None}
    staged = stage_turn_trace(no_id, debug=True, delivery="sync")
    assert all(e["event_type"] != TURN_TRACE_EVENT for e in staged["telemetry_events"])
