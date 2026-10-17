"""Turn trace builder contract (EDG-138): whitelisted, bounded, opt-in, linkable by turn_id.

Pure unit tests, no database. Secret-looking strings here are plain prose that must never appear in
a stored row. None is token-shaped, so the secret scanner stays quiet.
"""

from __future__ import annotations

import json

from agent.models.parsed_input import ParsedInput
from agent.turn_trace import (
    TRACE_MAX_CHUNKS,
    TRACE_SNIPPET_WIDTH,
    TURN_TRACE_EVENT,
    turn_trace_event,
    turn_trace_public,
)

PROVIDER_ERROR = "provider said: connection refused at 10.0.0.7"
PLANNER_NOTE = "planner note: the hidden witness is the baron's cousin"
POLICY_SELECTOR = "adventure-reviewed-policy-7f3a"
RECALL_SELECTOR = "recall-fact-review-91c2"
CREATURE_ID = "creature-review-4d10"


def _rule(text: str = "Grappling uses Athletics against a target.", **extra) -> dict:
    return {
        "text": text,
        "source": "PHB",
        "kind": "rules",
        "page": 49,
        "collection": "rules_phb",
        "score": 0.75,
        "rules_edition": "2014",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        **extra,
    }


def _adventure(text: str, audience: str) -> dict:
    return {
        "text": text,
        "source": "ruined_tower",
        "kind": "adventure",
        "page": 3,
        "collection": "adventure_ruined_tower",
        "score": 0.5,
        "rules_edition": "2014",
        "source_sha256": "c" * 64,
        "extraction_sha256": "d" * 64,
        "audience": audience,
        "scene_id": "tower_gate",
        "passage_id": "gate_text",
        "source_reference": CREATURE_ID,
    }


def _parsed(**overrides) -> ParsedInput:
    fields = {
        "intent": "exploration",
        "entities": {"npc": "Mara", "nested": {"secret": PROVIDER_ERROR}, "count": 2},
        "check": "perception",
        "difficulty": "easy",
        "target": "door",
        "retrieval_query": "Perception check to notice a hidden door",
        "resolution_policy_id": POLICY_SELECTOR,
        "recall_fact_id": RECALL_SELECTOR,
    }
    return ParsedInput(**{**fields, **overrides})


def _event(**overrides) -> dict:
    kwargs = {
        "turn_id": "turn-1",
        "delivery": "stream",
        "path": "normal",
        "parsed_input": _parsed(),
        "rules_context": [_rule()],
        "adventure_context": [_adventure(PLANNER_NOTE, "planner")],
        "rules_edition": "2014",
        "source_mode": "module",
        "narrator_profile": "play.narrator",
        "model_profiles": {
            "profile_version": 3,
            "profiles": {
                "play.narrator": {
                    "model": "gpt-test-narrator",
                    "reasoning_effort": "low",
                    "api": "responses",
                    "api_key_env": "OPENAI_API_KEY",
                    "base_url": PROVIDER_ERROR,
                },
            },
        },
    }
    kwargs.update(overrides)
    return turn_trace_event(**kwargs)


def test_no_turn_id_means_no_row():
    assert _event(turn_id=None) is None
    assert _event(turn_id="") is None


def test_row_is_versioned_and_references_companions_instead_of_copying_them():
    event = _event()
    assert event["event_type"] == TURN_TRACE_EVENT == "turn_trace"
    payload = event["payload"]
    assert payload["version"] == 1
    assert payload["turn_id"] == "turn-1"
    assert payload["composed_with"] == ["turn_diagnostics", "combat_round"]
    # Measured stages, usage, cost and dice live in their own rows and must not be duplicated here.
    for forbidden in ("timings_ms", "totals", "provider_calls", "dice_log", "stages_ms"):
        assert forbidden not in json.dumps(payload)


def test_parsed_intent_keeps_the_original_understanding_without_selectors():
    payload = _event()["payload"]
    parsed = payload["parsed_input"]
    assert parsed["intent"] == "exploration"
    assert parsed["check"] == "perception"
    assert parsed["retrieval_query"] == "Perception check to notice a hidden door"
    assert parsed["target"] == "door"
    serialised = json.dumps(payload)
    assert POLICY_SELECTOR not in serialised
    assert RECALL_SELECTOR not in serialised
    assert "resolution_policy_id" not in serialised
    assert "recall_fact_id" not in serialised


def test_entities_keep_scalars_only_and_drop_nested_planner_structures():
    entities = _event()["payload"]["parsed_input"]["entities"]
    assert entities == {"npc": "Mara", "count": 2}
    assert PROVIDER_ERROR not in json.dumps(entities)


def test_planner_only_adventure_text_is_withheld_but_its_citation_is_kept():
    adventure = _event()["payload"]["rag"]["adventure"]
    (chunk,) = adventure["chunks"]
    assert chunk["text_withheld"] is True
    assert "text" not in chunk
    assert chunk["source"] == "ruined_tower"
    assert chunk["citation"]["audience"] == "planner"
    serialised = json.dumps(_event())
    assert PLANNER_NOTE not in serialised
    assert CREATURE_ID not in serialised
    assert "scene_id" not in serialised
    assert "passage_id" not in serialised


def test_player_rules_snippet_is_kept_with_citation_identity():
    (chunk,) = _event()["payload"]["rag"]["rules"]["chunks"]
    assert chunk["text_withheld"] is False
    assert chunk["text"].startswith("Grappling uses Athletics")
    citation = chunk["citation"]
    assert citation["rules_edition"] == "2014"
    assert citation["source_sha256"] == "a" * 64
    assert citation["extraction_sha256"] == "b" * 64
    assert citation["page"] == 49
    assert citation["edition_status"] in {"compatible", "unverified", "incompatible"}
    assert citation["reference_id"]


def test_model_settings_are_profile_names_and_models_only():
    settings = _event()["payload"]["settings"]
    assert settings["narrator_profile"] == "play.narrator"
    assert settings["profile_version"] == 3
    assert settings["profiles"] == {
        "play.narrator": {"model": "gpt-test-narrator", "reasoning_effort": "low", "api": "responses"}
    }
    serialised = json.dumps(settings)
    assert PROVIDER_ERROR not in serialised
    assert "api_key" not in serialised
    assert "base_url" not in serialised


def test_snippets_and_chunk_counts_are_bounded():
    long_text = "x" * 5000
    rules = [_rule(text=long_text, page=i) for i in range(TRACE_MAX_CHUNKS + 5)]
    payload = _event(rules_context=rules)["payload"]
    block = payload["rag"]["rules"]
    assert block["count"] == TRACE_MAX_CHUNKS + 5
    assert len(block["chunks"]) == TRACE_MAX_CHUNKS
    assert block["chunks_dropped"] == 5
    assert all(len(c["text"]) == TRACE_SNIPPET_WIDTH for c in block["chunks"])
    # A hostile input stays small even at the bounds.
    assert len(json.dumps(payload)) < 20_000


def test_public_projection_rejects_other_versions_and_keeps_bounds():
    payload = _event()["payload"]
    assert turn_trace_public(None) is None
    assert turn_trace_public({**payload, "version": 99}) is None
    public = turn_trace_public(payload)
    assert public["turn_id"] == "turn-1"
    assert set(public["rag"]) == {"rules", "adventure"}
    assert public["rag"]["rules"]["chunks_dropped"] == 0


def test_stage_turn_trace_is_off_unless_debug_was_requested(monkeypatch):
    from apps.api.services import turn_runner

    monkeypatch.setattr(turn_runner, "profile_diagnostics", lambda _snapshot: {"profiles": {}})
    base = {"turn_id": "turn-1", "telemetry_events": [], "parsed_input": _parsed()}
    unchanged = turn_runner.stage_turn_trace(base, debug=False, delivery="sync")
    assert unchanged is base
    staged = turn_runner.stage_turn_trace(base, debug=True, delivery="sync")
    assert [e["event_type"] for e in staged["telemetry_events"]] == [TURN_TRACE_EVENT]
    assert staged["telemetry_events"][0]["payload"]["delivery"] == "sync"
    # The input state is not mutated: the caller's telemetry list stays as it was.
    assert base["telemetry_events"] == []


def test_stage_turn_trace_reports_combat_path_from_the_turn_row(monkeypatch):
    from apps.api.services import turn_runner

    monkeypatch.setattr(turn_runner, "profile_diagnostics", lambda _snapshot: {"profiles": {}})
    turn_row = {"event_type": "turn_diagnostics", "payload": {"turn_id": "turn-1", "path": "combat"}}
    state = {"turn_id": "turn-1", "telemetry_events": [turn_row]}
    staged = turn_runner.stage_turn_trace(state, debug=True, delivery="stream")
    assert staged["telemetry_events"][-1]["payload"]["path"] == "combat"


def test_stage_turn_trace_without_turn_id_stages_nothing(monkeypatch):
    from apps.api.services import turn_runner

    monkeypatch.setattr(turn_runner, "profile_diagnostics", lambda _snapshot: {"profiles": {}})
    state = {"turn_id": None, "telemetry_events": []}
    assert turn_runner.stage_turn_trace(state, debug=True, delivery="sync") is state


def test_non_scalar_entity_values_are_dropped_and_entity_count_is_bounded():
    many = {f"k{i}": i for i in range(20)}
    parsed = _parsed(entities={**many, "planner": {"deep": PLANNER_NOTE}, "flag": None})
    stored = _event(parsed_input=parsed)["payload"]["parsed_input"]["entities"]
    assert len(stored) <= 8
    assert "planner" not in stored
    assert PLANNER_NOTE not in json.dumps(stored)
