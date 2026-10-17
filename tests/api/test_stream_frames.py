"""Golden wire format for every SSE frame the turn runner emits (EDG-058).

Each representative frame is built from the real projections (`public_adjudication`,
`error_body`, `combat_state_public`, `_debug_payload`) and serialised by the real `sse_event`.
The result must equal the committed golden text byte for byte, so typing or validation work on
the contract can never change what goes on the wire.

Regenerate deliberately with `EDGAR_REGEN_STREAM_GOLDEN=1 pytest tests/api/test_stream_frames.py`
and review the diff: a golden change is a protocol change and needs a matching frontend update.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.models.adjudication import AdjudicationResult
from apps.api.schemas.stream import frame_violations
from apps.api.services import turn_runner
from apps.api.services.turn_runner import (
    _debug_payload,
    combat_state_public,
    error_body,
    public_adjudication,
    sse_event,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "stream_frames.golden.json"


def _combat_state() -> dict[str, Any]:
    """A roster with one hero and one foe, shaped like the persisted combat dict."""
    return {
        "id": 7,
        "round": 2,
        "ended": False,
        "outcome": None,
        "current_turn_index": 0,
        "player_ac": 15,
        "second_wind_used": False,
        "raging": False,
        "economy": {"round": 2, "action": "attack", "bonus_action": None, "dodging": False},
        "initiative_order": ["Edgar", "Goblin"],
        "initiative": [
            {"id": "Edgar", "name": "Edgar", "init": 18, "is_player": True, "hp": 20, "hp_max": 24},
            {"id": "Goblin", "name": "Goblin", "init": 9, "is_player": False, "hp": 4, "hp_max": 7},
        ],
        "combatants": [
            {
                "name": "Edgar",
                "display_name": "Edgar",
                "is_player": True,
                "hp_current": 20,
                "hp_max": 24,
                "ac": 15,
                "alive": True,
                "disposition": "active",
            },
            {
                "name": "Goblin",
                "is_player": False,
                "hp_current": 4,
                "hp_max": 7,
                "ac": 13,
                "alive": True,
                "disposition": "active",
                "reaction_used": False,
            },
        ],
    }


def _done_payload(combat_state: dict[str, Any] | None) -> dict[str, Any]:
    """The `done` frame, keys in the order `persist_turn` builds them."""
    return {
        "event_id": 41,
        "narration": "The goblin lunges; your blade bites.",
        "citations": [],
        "ruling_citation_ids": [],
        "rules_edition": "2014",
        "character": {"id": 3, "name": "Edgar", "hp_current": 20, "hp_max": 24, "level": 3},
        "current_scene_id": "cellar",
        "combat_state": combat_state_public(combat_state),
        "campaign_complete": False,
        "retrieval_degraded": False,
        "source_status": [],
        "source_mode": "module",
        "roll_status": "no_roll",
        "roll": None,
    }


def representative_frames() -> dict[str, tuple[str, Any]]:
    """Label -> (event name, payload). One or more frames for every event the runner emits."""
    combat = _combat_state()
    adjudication = AdjudicationResult(
        success=True,
        damage=4,
        conditions=[],
        mechanical_summary="Hit the goblin for 4 damage.",
        dice_result=15,
        check="attack",
        dc=13,
        against="AC",
        modifier=3,
        roll_expression="1d20+3",
        natural_roll=12,
        dice_log=[{"expression": "1d20+3"}],
        completion_status="none",
    )
    frames: dict[str, tuple[str, Any]] = {}
    for stage in ("parsing", "retrieving", "adjudicating", "narrating", "saving"):
        frames[f"status_{stage}"] = ("status", {"stage": stage})
    frames["token"] = ("token", {"text": "The door creaks open. "})
    frames["adjudication"] = ("adjudication", public_adjudication(adjudication))
    frames["error_code"] = ("error", error_body("provider_unavailable", event=None))
    frames["error_exception"] = (
        "error",
        error_body(ValueError("provider said: connection refused at 10.0.0.7"), event=None),
    )
    frames["done_non_combat"] = ("done", _done_payload(None))
    frames["done_combat"] = ("done", _done_payload(combat))
    frames["debug"] = (
        "debug",
        _debug_payload(
            parsed=None,
            rules_ctx=[],
            adv_ctx=[],
            world_flags_in={"cellar_door": "open"},
            combat_state=combat,
            timings={"parsing_ms": 1, "total_ms": 2},
        ),
    )
    return frames


@pytest.fixture
def stable_support_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the environment-dependent parts of the frames so the golden text is stable.

    Error frames carry a random support reference, and the debug frame reports the configured
    model profiles. Neither is part of the contract under test, so both are fixed here.
    """
    monkeypatch.setattr(secrets, "token_hex", lambda n: "0" * (2 * n))
    monkeypatch.setattr(turn_runner, "current_model_profiles", lambda: None)
    monkeypatch.setattr(turn_runner, "profile_diagnostics", lambda _profiles: {"stub": True})
    monkeypatch.setattr(
        turn_runner, "effective_profile", lambda _stage: SimpleNamespace(model="stub-model")
    )


def _wire(event: str, data: Any) -> str:
    return sse_event(event, data)


def _golden() -> dict[str, str]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_file_has_every_frame_label(stable_support_refs) -> None:
    """Keep the regeneration path honest: the file on disk is the full, sorted set of labels."""
    frames = representative_frames()
    golden = _golden()
    assert sorted(golden) == sorted(frames)


@pytest.mark.parametrize("label", sorted(representative_frames()))
def test_golden_wire_format_per_frame(label: str, stable_support_refs) -> None:
    event, data = representative_frames()[label]
    assert frame_violations(event, data) == []
    assert _wire(event, data) == _golden()[label]


def test_regenerate_golden_when_requested(stable_support_refs) -> None:
    if os.environ.get("EDGAR_REGEN_STREAM_GOLDEN") != "1":
        pytest.skip("set EDGAR_REGEN_STREAM_GOLDEN=1 to rewrite the golden file")
    wire = {label: _wire(event, data) for label, (event, data) in representative_frames().items()}
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(json.dumps(wire, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_every_frame_type_is_covered_by_a_golden_frame(stable_support_refs) -> None:
    events = {event for event, _data in representative_frames().values()}
    assert events == {"status", "token", "adjudication", "error", "done", "debug"}


def test_sse_frame_shape_is_event_line_then_data_line() -> None:
    wire = _wire("token", {"text": "a"})
    assert wire == 'event: token\ndata: {"text": "a"}\n\n'
