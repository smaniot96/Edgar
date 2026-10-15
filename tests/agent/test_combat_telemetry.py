"""Combat round telemetry (EDG-031): actors, original dice, outcomes and measured stages.

These are node-level guarantees. The API tests (tests/api/test_combat_telemetry.py) cover the
same row across the turn transaction, sync and SSE paths, and reload.
"""

import json
import random

import pytest

from agent.combat_telemetry import COMBAT_ROUND_EVENT
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import _enemy, _player, _round, _state
from tools.dice import use_rng


def _round_event(out: dict) -> dict:
    events = out.get("telemetry_events") or []
    assert len(events) == 1
    assert events[0]["event_type"] == COMBAT_ROUND_EVENT
    return events[0]["payload"]


@pytest.mark.asyncio
async def test_similarly_named_enemies_keep_distinct_actor_identities(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        [_player(init=20, ac=13), _enemy("Goblin", init=5), _enemy("Goblin 2", init=4)],
        "I attack the goblin",
    )
    state["turn_id"] = "turn-a"
    # Both foes must get their attack: keep the hero standing through two blows.
    state["character"] = {**state["character"], "hp_current": 40, "hp_max": 40}
    with use_rng(random.Random(3)):
        out = await _round(fake_llm_schema, state)
    payload = _round_event(out)

    assert payload["turn_id"] == "turn-a"
    assert [a["id"] for a in payload["actors"]] == ["Goblin", "Goblin 2"]
    assert [a["name"] for a in payload["actors"]] == ["Goblin", "Goblin 2"]
    # Each enemy's attack roll is its own, under its own stable id.
    assert [o["actor"] for o in payload["outcomes"]] == ["Goblin", "Goblin 2"]
    purposes = [d["purpose"] for d in payload["dice_log"]]
    assert "Goblin attack" in purposes and "Goblin 2 attack" in purposes


@pytest.mark.asyncio
async def test_round_stores_the_original_dice_and_outcomes(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        [_player(init=20, ac=13), _enemy("Goblin", ac=30, init=1)], "I attack the goblin"
    )
    with use_rng(random.Random(1)):
        out = await _round(fake_llm_schema, state)
    payload = _round_event(out)

    # The stored dice are the adjudicated dice, not a second roll.
    assert payload["dice_log"] == out["adjudication_result"].dice_log
    assert payload["dice_log"][0]["purpose"] == "attack check"
    goblin = payload["outcomes"][0]
    assert goblin["actor"] == "Goblin" and goblin["action"] == "attack"
    assert goblin["hit"] is True and goblin["roll"] == 22 and goblin["target_ac"] == 13
    assert goblin["damage"] == 2
    assert payload["player_action"]["action"] == "attack"
    assert payload["player_action"]["target"] == "Goblin"
    assert payload["player_action"]["hit"] is False
    dispositions = {d["id"]: d for d in payload["dispositions"]}
    assert dispositions["player"]["is_player"] is True
    assert dispositions["Goblin"]["disposition"] == "active"
    # The row is plain JSON, ready for event_log.
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.asyncio
async def test_measured_stages_are_recorded_and_unmeasured_ones_are_labelled(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state([_player(init=20, ac=13), _enemy("Goblin", init=1)], "I attack the goblin")
    with use_rng(random.Random(2)):
        out = await _round(fake_llm_schema, state)
    timings = _round_event(out)["timings_ms"]
    assert set(timings) == {"retrieving_ms", "adjudicating_ms", "narrating_ms"}
    assert all(isinstance(v, int) and v >= 0 for v in timings.values())
    assert _round_event(out)["timings_unavailable"] == []


@pytest.mark.asyncio
async def test_round_without_adjudication_labels_that_stage_unavailable(
    fake_llm_schema, fake_retrieval
) -> None:
    # A flee never reaches the adjudicator, so its stage must not be reported as measured.
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state([_player(init=1, ac=13), _enemy("Goblin", init=20)], "I run away from the fight")
    with use_rng(random.Random(5)):
        out = await _round(fake_llm_schema, state)
    assert out["combat_state"]["outcome"] == "fled"
    payload = _round_event(out)
    assert "adjudicating_ms" not in payload["timings_ms"]
    assert payload["timings_unavailable"] == ["adjudicating_ms"]
    assert payload["player_action"] == {"action": "flee", "result": "fled"}
    assert payload["outcome"] == "fled" and payload["ended"] is True
    assert not any(schema is AdjudicationResult for schema, _ in fake_llm_schema.calls)


@pytest.mark.asyncio
async def test_round_telemetry_never_carries_creature_provenance(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    goblin = _enemy("Goblin", init=1)
    goblin.update(
        {
            "creature_id": "private-creature-id",
            "source_reference": {"actor_id": "private_actor", "passage_id": "secret_stats"},
            "stat_adaptation": "legacy_solo_encounter",
        }
    )
    state = _state([_player(init=20, ac=13), goblin], "I attack the goblin")
    with use_rng(random.Random(4)):
        out = await _round(fake_llm_schema, state)
    text = json.dumps(_round_event(out))
    for private in (
        "private-creature-id",
        "private_actor",
        "secret_stats",
        "source_reference",
        "creature_id",
    ):
        assert private not in text


@pytest.mark.asyncio
async def test_turn_without_a_round_stages_no_round_telemetry(
    fake_llm_schema, fake_retrieval
) -> None:
    # The player names a foe that is not in the fight: the question costs no round and no row.
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="ogre"
    )
    state = _state([_player(init=20, ac=13), _enemy("Goblin", init=1)], "I attack the ogre")
    out = await _round(fake_llm_schema, state)
    assert "telemetry_events" not in out
