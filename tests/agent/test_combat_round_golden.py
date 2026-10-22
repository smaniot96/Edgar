"""Characterization net for `combat_round_node` (EDG-057).

Each scenario runs one full round through the real node with seeded dice and the scripted fake
provider, then snapshots everything the round returns: combat state, adjudication (with its dice
log), the telemetry row (stage timings removed), narration, messages and the deferred-narration
contract (EDG-079). The snapshot lives in tests/fixtures/combat/round_golden.json.

A refactor of graph_combat must leave that file unchanged. Regenerate it only for an intended
behaviour change, with EDG057_REGEN_GOLDEN=1, and say so in the commit that does it.
"""

from __future__ import annotations

import json
import os
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from agent.graph_combat import (
    PENDING_ROUND_NARRATION_KEY,
    EncounterSetup,
    EnemyStat,
    PendingRoundNarration,
    combat_round_node,
    finalize_round_narration,
)
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import FIGHTER, IDLE, TOUGH, _enemy, _player, _state
from tests.agent.test_monster_stats import TROLL_TEXT, troll_identity
from tools.dice import use_rng

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "combat" / "round_golden.json"
REGEN = bool(os.environ.get("EDG057_REGEN_GOLDEN"))

WIZARD = {
    "id": 2,
    "name": "Ilse",
    "class": "Wizard",
    "level": 1,
    "hp_current": 8,
    "hp_max": 8,
    "stats": {"STR": 8, "DEX": 14, "CON": 12, "INT": 16, "WIS": 12, "CHA": 10},
    "inventory": {"weapons": ["quarterstaff"], "armor": "none"},
}
CAPTIVE = {
    "name": "Bandit",
    "disposition": "captive",
    "combat_id": 7,
    "combatant_id": "Bandit",
    "combat_round": 4,
}


def _attack(target: str) -> ParsedInput:
    return ParsedInput(intent="combat", check="attack", target=target)


def _with_character(state: dict, character: dict) -> dict:
    state["character"] = dict(character)
    return state


# --- scenarios ----------------------------------------------------------------------------------
# Each builder configures the fake provider (and any monkeypatch it needs) and returns the state.
# Dice seeds were chosen so each scenario reaches the outcome named in its docstring.


def _plain_attack(fake, monkeypatch) -> dict:
    """The hero's sword attack on a goblin; the goblin answers (initiative 5 after the hero's 20)."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state([_player(init=20, ac=13), _enemy("Goblin", init=5)], "I attack the goblin")
    state["parsed_input"] = _attack("goblin")
    return _with_character(state, {**FIGHTER, "hp_current": 12, "hp_max": 12})


def _enemy_turns_in_initiative_order(fake, monkeypatch) -> dict:
    """A wolf acts before the hero (18 > 10), an ogre after: the hero acts in between."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        [_player(init=10, ac=30), _enemy("Wolf", init=18), _enemy("Ogre", init=2, hp=30)],
        "I swing at the ogre",
    )
    state["parsed_input"] = _attack("ogre")
    return _with_character(state, {**FIGHTER, "hp_current": 12, "hp_max": 12})


def _player_defeat(fake, monkeypatch) -> dict:
    """A wounded hero waits while an ogre that cannot be hit lands the blow that subdues them."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        [_player(init=20, ac=5), _enemy("Ogre", hp=500, ac=99, init=1)], "I wait and watch"
    )
    state["parsed_input"] = IDLE
    return _with_character(state, {**FIGHTER, "hp_current": 3, "hp_max": 12})


def _enemy_defeat_victory(fake, monkeypatch) -> dict:
    """The hero's blow ends the last foe, a boss: victory, with the completion ruling."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        [_player(init=20, ac=99), _enemy("Goblin", hp=1, ac=1, init=1, boss=True)],
        "I attack the goblin",
    )
    state["parsed_input"] = _attack("goblin")
    return _with_character(state, TOUGH)


def _flee(fake, monkeypatch) -> dict:
    """The hero declares leaving the fight; no attack is rolled."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state([_player(init=20, ac=99), _enemy("Ogre", hp=500, ac=99, init=1)], "I flee!")
    state["parsed_input"] = IDLE
    return _with_character(state, TOUGH)


def _second_wind(fake, monkeypatch) -> dict:
    """A fighter with one Second Wind use spends it; the ogre cannot reach them."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    stats = {**FIGHTER["stats"], "second_wind": {"uses_remaining": 1}}
    state = _state(
        [
            {**_player(init=20, ac=30), "hp_current": 5},
            {**_enemy("Ogre", hp=30, ac=12, init=1), "attack_bonus": -20},
        ],
        "I use Second Wind",
    )
    state["parsed_input"] = ParsedInput(intent="combat")
    return _with_character(state, {**FIGHTER, "hp_current": 5, "stats": stats})


def _declared_spell(fake, monkeypatch) -> dict:
    """A wizard casts Fire Bolt: a catalog spell attack resolved by the engine."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        [_player(init=15, ac=14), _enemy("Goblin", hp=20, ac=12, init=5)],
        "I cast Fire Bolt at the goblin",
    )
    state["parsed_input"] = ParsedInput(intent="combat", check="spell_attack", target="goblin")
    return _with_character(state, WIZARD)


def _captive_ruling(fake, monkeypatch) -> dict:
    """A named captive outside any fight: a deterministic engine ruling, no dice, no narrator."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, "I release the bandit")
    state.pop("combat_state", None)
    state["combat_captives"] = [dict(CAPTIVE)]
    state["parsed_input"] = ParsedInput(intent="normal", target="Bandit")
    return state


def _clarification_question(fake, monkeypatch) -> dict:
    """Two foes and an unknown target: the engine asks who is meant and rolls nothing."""
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state([_player(), _enemy("Goblin"), _enemy("Wolf")], "I attack the dragon")
    state["parsed_input"] = _attack("dragon")
    return state


def _encounter_setup(fake, monkeypatch) -> dict:
    """No fight yet: the builder proposes a verified troll from its exact record; initiative is rolled."""
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *a, **k: troll_identity())
    fake.responses[EncounterSetup] = EncounterSetup(enemies=[EnemyStat(name="Troll", hp_max=73)])
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, "I attack the troll")
    state["parsed_input"] = _attack("Troll")
    assert "Troll" in TROLL_TEXT
    return _with_character(state, {**FIGHTER, "hp_current": 40, "hp_max": 40})


SCENARIOS: dict[str, tuple[Callable[..., dict], int]] = {
    "plain_attack": (_plain_attack, 3),
    "enemy_turns_in_initiative_order": (_enemy_turns_in_initiative_order, 4),
    "player_defeat": (_player_defeat, 1),
    "enemy_defeat_victory_with_suffix": (_enemy_defeat_victory, 0),
    "flee": (_flee, 1),
    "second_wind": (_second_wind, 4),
    "declared_spell": (_declared_spell, 2),
    "captive_ruling": (_captive_ruling, 0),
    "clarification_question": (_clarification_question, 0),
    "encounter_setup_roster": (_encounter_setup, 5),
}


# --- snapshotting -------------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Plain JSON data for a round's values. An unknown type fails loudly rather than being guessed."""
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, BaseMessage):
        return {"type": value.type, "content": value.content}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsnapshotted value of type {type(value).__name__}")


def _round_row(event: dict[str, Any]) -> dict[str, Any]:
    # Stage timings are wall-clock; everything else in the telemetry row must match exactly.
    payload = {
        k: v for k, v in event["payload"].items() if k not in ("timings_ms", "timings_unavailable")
    }
    return {"event_type": event["event_type"], "payload": _jsonable(payload)}


def _snapshot(out: dict[str, Any]) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for key, value in out.items():
        if key == PENDING_ROUND_NARRATION_KEY:
            snap[key] = _pending_snapshot(value)
        elif key == "telemetry_events":
            snap[key] = [_round_row(event) for event in value]
        else:
            snap[key] = _jsonable(value)
    return snap


def _pending_snapshot(pending: PendingRoundNarration) -> dict[str, Any]:
    return {
        "outcome": pending.outcome,
        "profile": pending.profile,
        "adjudication": _jsonable(pending.adjudication),
        "combatants": _jsonable(pending.combatants),
        "base_update": _jsonable(pending.base_update),
        "round_event": _jsonable(pending.round_event),
        "stage_ms_keys": sorted(pending.stage_ms),
        "narr_state": _jsonable(pending.narr_state),
    }


async def _run(name: str, fake, monkeypatch, *, defer: bool = False) -> dict[str, Any]:
    builder, seed = SCENARIOS[name]
    state = builder(fake, monkeypatch)
    if defer:
        state["defer_narration"] = True
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)


def _finalized(out: dict[str, Any], fake) -> dict[str, Any]:
    """The deferred round completed the way the SSE runner completes it: one narrator text."""
    pending = out.get(PENDING_ROUND_NARRATION_KEY)
    if pending is None:
        return out
    return finalize_round_narration(pending, fake.narration, bound=True, narrating_ms=0)


def _load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _store_golden(entries: dict[str, Any]) -> None:
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(
        json.dumps(entries, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_round_matches_golden(name, fake_llm_schema, fake_retrieval, monkeypatch) -> None:
    fake_llm_schema.narration = "Scripted narration."
    snap = _snapshot(await _run(name, fake_llm_schema, monkeypatch))
    if REGEN:
        entries = _load_golden() if GOLDEN_PATH.exists() else {}
        entries[name] = snap
        _store_golden(entries)
    assert snap == _load_golden()[name]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_deferred_narration_finalises_to_the_sync_round(
    name, fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    fake_llm_schema.narration = "Scripted narration."
    sync = _snapshot(await _run(name, fake_llm_schema, monkeypatch))
    deferred = await _run(name, fake_llm_schema, monkeypatch, defer=True)
    assert _snapshot(_finalized(deferred, fake_llm_schema)) == sync


@pytest.mark.asyncio
async def test_golden_covers_each_named_branch(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    """The branches the golden is meant to pin are actually exercised, not silently skipped."""
    golden = _load_golden()
    assert set(golden) == set(SCENARIOS)
    assert golden["player_defeat"]["combat_state"]["outcome"] == "defeat"
    assert golden["enemy_defeat_victory_with_suffix"]["combat_state"]["outcome"] == "victory"
    assert golden["flee"]["combat_state"]["outcome"] == "fled"
    assert "Who do you attack?" in golden["clarification_question"]["narration"]
    assert golden["captive_ruling"]["captive_outcome"]
    assert golden["encounter_setup_roster"]["combat_state"]["round"] == 1
    assert golden["encounter_setup_roster"]["adjudication_result"]["dice_log"] == []
    assert "Confirm before any dice" in golden["encounter_setup_roster"]["narration"]
    assert any(
        c["name"] == "Troll" for c in golden["encounter_setup_roster"]["combat_state"]["combatants"]
    )
    assert golden["plain_attack"]["telemetry_events"][0]["payload"]["round"] == 1
    assert golden["flee"]["telemetry_events"][0]["payload"]["outcome"] == "fled"
