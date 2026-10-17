"""The turn stream contract: committed schema freshness and backend drift detection (EDG-058).

The committed `frontend/src/api/stream.schema.json` is rendered from `apps/api/schemas/stream.py`.
A change on either side without the other must fail here or in the frontend contract test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

from agent.models.adjudication import AdjudicationResult
from apps.api.schemas import stream
from apps.api.schemas.stream import (
    STREAM_EVENTS,
    STREAM_STAGES,
    AdjudicationFrame,
    CombatOutcomeEntryPublic,
    CombatStatePublic,
    frame_violations,
    render_stream_schema,
)
from apps.api.services import turn_runner
from apps.api.services.combat import resolved_combat_outcomes_public
from apps.api.services.turn_runner import sse_event
from tests.api.test_stream_frames import representative_frames

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "stream.schema.json"

# AdjudicationResult fields the runner strips before the frame leaves the engine.
ENGINE_ONLY_ADJUDICATION_FIELDS = {
    "story_effects",
    "accepted_story_effects",
    "proposed_prepared_amendment",
    "accepted_prepared_amendment",
}


def _committed() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_committed_schema_is_current() -> None:
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render_stream_schema(), (
        "frontend/src/api/stream.schema.json is stale; run "
        "`.venv/bin/python -m tools.generate_stream_schema`"
    )


def test_schema_declares_every_event_and_stage_in_order() -> None:
    document = _committed()
    assert document["events"] == list(STREAM_EVENTS)
    assert document["statusStages"] == list(STREAM_STAGES)
    assert set(document["frames"]) == set(STREAM_EVENTS)


def test_adjudication_frame_matches_the_engine_result_minus_engine_only_fields() -> None:
    engine_fields = set(AdjudicationResult.model_fields) - ENGINE_ONLY_ADJUDICATION_FIELDS
    assert set(AdjudicationFrame.model_fields) == engine_fields


def test_representative_frames_conform_to_their_models() -> None:
    for label, (event, data) in representative_frames().items():
        assert frame_violations(event, data) == [], label


def test_undeclared_wire_field_is_a_violation() -> None:
    assert frame_violations("status", {"stage": "parsing", "extra": 1})
    assert frame_violations("token", {"text": "x", "bonus": True})


def test_unknown_stage_and_unknown_event_are_violations() -> None:
    assert frame_violations("status", {"stage": "thinking"})
    assert frame_violations("trace", {"text": "x"}) == ["unknown event: trace"]


def test_missing_required_done_field_is_a_violation() -> None:
    _event, done = representative_frames()["done_non_combat"]
    del done["roll"]
    assert frame_violations("done", done) == ["roll: missing"]


def test_violation_messages_name_fields_never_values() -> None:
    secret = "provider said: connection refused at 10.0.0.7"
    violations = frame_violations("status", {"stage": secret})
    assert violations
    assert all(secret not in message for message in violations)


class _RecordingLog:
    """Stands in for the runner's structlog logger so the test sees exactly what is logged.

    `capture_logs` cannot be used here: once another test has cached the runner's bound logger,
    structlog no longer routes its calls through the capture processor.
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.entries.append({"event": event, **fields})


def test_sse_event_sends_a_violating_frame_unchanged_and_logs_it_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(turn_runner, "log", recorder)
    bogus = {"stage": "thinking", "note": "player private text"}
    wire = sse_event("status", bogus)
    assert wire == 'event: status\ndata: {"stage": "thinking", "note": "player private text"}\n\n'
    assert len(recorder.entries) == 1
    entry = recorder.entries[0]
    assert entry["event"] == "sse_frame_contract_violation"
    assert entry["frame"] == "status"
    assert "player private text" not in json.dumps(entry, default=str)


def test_conforming_frames_log_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingLog()
    monkeypatch.setattr(turn_runner, "log", recorder)
    for event, data in representative_frames().values():
        sse_event(event, data)
    assert recorder.entries == []


def test_adding_a_backend_field_changes_the_rendered_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberate drift: a new `token` field must change the contract, so the frontend notices."""
    from pydantic import create_model

    drifted_token = create_model(
        "TokenFrame",
        __base__=stream._Frame,
        text=(str, ...),
        channel=(str, "narration"),
    )
    monkeypatch.setitem(stream.FRAME_MODELS, "token", drifted_token)
    assert render_stream_schema() != SCHEMA_PATH.read_text(encoding="utf-8")


def test_adding_a_stage_changes_the_rendered_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberate drift: a new pipeline stage must change the contract the frontend checks."""
    from pydantic import create_model

    drifted_status = create_model(
        "StatusFrame",
        __base__=stream._Frame,
        stage=(Literal[(*STREAM_STAGES, "trace")], ...),  # type: ignore[valid-type]
    )
    monkeypatch.setitem(stream.FRAME_MODELS, "status", drifted_status)
    assert render_stream_schema() != SCHEMA_PATH.read_text(encoding="utf-8")


def test_combat_view_on_done_conforms_to_the_public_model() -> None:
    _event, done = representative_frames()["done_combat"]
    CombatStatePublic.model_validate(done["combat_state"])


def test_resolved_outcome_entries_conform_to_the_public_model() -> None:
    engine_row = [
        {
            "id": 3,
            "outcome": "victory",
            "round": 4,
            "combatants": [
                {
                    "name": "Goblin",
                    "is_player": False,
                    "alive": False,
                    "disposition": "defeated",
                    "source_reference": {"private": "binding"},
                }
            ],
        }
    ]
    public = resolved_combat_outcomes_public(engine_row)
    CombatOutcomeEntryPublic.model_validate(public[0])
    assert "source_reference" not in public[0]["combatants"][0]
