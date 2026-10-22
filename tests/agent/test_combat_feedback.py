"""Actual spell actions and HP restoration survive narration and public reload projections."""

import copy
import json

import pytest

from agent.combat_narration import PENDING_ROUND_NARRATION_KEY, finalize_round_narration
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from apps.api.services.roll_record import build_roll_record, stored_roll
from apps.api.services.turn_runner import combat_state_public
from tests.agent.confirming import confirm_turn
from tests.agent.test_monster_spellcasting import (
    SINGLE_BOLT,
    SPELLCASTER,
    WAIT,
    _priest,
    _state as caster_state,
)
from tests.agent.test_spells import FIGHTER, WIZARD, ScriptedDice, _combat_state
from tools.dice import use_rng


def _state(text, *, hp=36, foe_hp=55, second_wind=1):
    hero = copy.deepcopy(FIGHTER)
    hero.update(level=4, hp_current=hp, hp_max=36)
    hero["stats"]["STR"] = 18
    hero["stats"]["second_wind"] = {"uses_remaining": second_wind}
    state = _combat_state(hero, text, parsed=ParsedInput(intent="combat", check="attack", target="Priest"))
    priest = _priest(init=20, hp=foe_hp, spells=SINGLE_BOLT.split("1st level")[0])
    priest.update(band="melee", distance_ft=5, hp_max=55)
    state["combat_state"]["combatants"][1] = priest
    state["defer_narration"] = True
    return state


def _finish(out):
    pending = out[PENDING_ROUND_NARRATION_KEY]
    # Deliberately wrong model order and omitted damage must never become persisted fact.
    return finalize_round_narration(pending, "Eda attacks first. The priest answers with frost but Eda is unharmed.", bound=True, narrating_ms=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("enemy_faces,damage,hit", [([17, 18, 6], 6, True), ([2, 6], 0, False)])
async def test_spell_action_and_hero_attack_have_exact_order_damage_and_public_identity(fake_llm_schema, fake_retrieval, enemy_faces, damage, hit):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state("I attack the priest with my longsword.")
    with use_rng(ScriptedDice([*enemy_faces, 8, 3])):
        final = _finish(await combat_round_node(state))
    events = final["combat_state"]["round_events"]
    assert [event["kind"] for event in events] == ["enemy_spell", "player_attack"]
    spell = events[0]
    assert spell["spell"] == "Fire Bolt" and spell["damage"] == damage and spell["hit"] is hit
    assert spell["actor_id"] == "Priest" and spell["target_id"] == "player"
    text = final["narration"]
    assert text.index(events[0]["text"]) < text.index(events[1]["text"])
    assert (f"for {damage} fire damage" in text) if hit else ("misses Eda" in text)
    assert "What do you do next?" in text and "unharmed" not in text
    saved = json.loads(json.dumps(final["combat_state"]))
    assert combat_state_public(saved)["round_events"][0] == spell
    assert next(c for c in saved["combatants"] if c.get("is_player"))["hp_current"] == 36 - damage


@pytest.mark.asyncio
async def test_second_wind_cap_records_roll_actual_recovery_order_and_reload(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state("First I use Second Wind, then I attack the priest with my longsword.")
    with use_rng(ScriptedDice([17, 18, 10, 10, 3])):
        asked = await combat_round_node(state)
        final = _finish(await confirm_turn(asked, state))
    events = final["combat_state"]["round_events"]
    assert [e["kind"] for e in events] == ["enemy_spell", "second_wind", "player_attack"]
    assert "regains 10 HP" in events[1]["text"] and "roll was 14" in events[1]["text"]
    text = final["narration"]
    assert text.index(events[0]["text"]) < text.index(events[1]["text"]) < text.index(events[2]["text"])
    assert next(c for c in final["combat_state"]["combatants"] if c.get("is_player"))["hp_current"] == 36
    roll = build_roll_record(final["adjudication_result"], "test-turn")
    assert roll["healing"] == 10
    healing = next(d for d in roll["dice_log"] if d["purpose"] == "second wind")
    assert healing["total"] == 14 and healing["applied"] == 10
    reloaded = json.loads(json.dumps({"roll_status": "captured", "roll": roll}))
    assert stored_roll(reloaded)[1]["dice_log"] == roll["dice_log"]


@pytest.mark.asyncio
@pytest.mark.parametrize("hp,foe_hp,script,outcome,suffix", [
    (1, 55, [17, 18, 10], "defeat", "**Subdued.**"),
    (36, 1, [17, 18, 6, 8, 3], "victory", "**Victory!**"),
])
async def test_spell_round_outcomes_are_explicit_after_authoritative_actions(fake_llm_schema, fake_retrieval, hp, foe_hp, script, outcome, suffix):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state("I attack the priest with my longsword.", hp=hp, foe_hp=foe_hp)
    with use_rng(ScriptedDice(script)):
        final = _finish(await combat_round_node(state))
    assert final["combat_state"]["outcome"] == outcome and final["combat_state"]["ended"]
    assert "Priest casts Fire Bolt" in final["narration"] and suffix in final["narration"]
    assert "What do you do next?" not in final["narration"]
    assert final["messages"][-1].content == final["narration"]


@pytest.mark.asyncio
async def test_exhausted_second_wind_does_not_silently_attack_or_roll(fake_llm_schema, fake_retrieval):
    state = _state("I use Second Wind then attack the priest.", second_wind=0)
    out = await combat_round_node(state)
    assert "Second Wind has no uses left until a short or long rest" in out["narration"]
    assert not out["adjudication_result"].dice_log and out["combat_state"]["round"] == 1
    assert PENDING_ROUND_NARRATION_KEY not in out


@pytest.mark.asyncio
async def test_spell_round_retains_end_turn_save_after_condition(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = caster_state("I wait", WAIT, _priest(init=20, spells=SPELLCASTER.replace("Blindness/Deafness", "Sacred Flame")))
    state["defer_narration"] = True
    with use_rng(ScriptedDice([1, 20])):
        final = _finish(await combat_round_node(state))
    text = final["narration"]
    assert text.index("casts Hold Person") < text.index("is paralyzed and cannot act") < text.index("ends")
    assert "WIS" in text and "rolled 25 vs DC 13" in text
    hero = next(c for c in final["combat_state"]["combatants"] if c.get("is_player"))
    assert not any(e.get("kind") == "paralyzed" for e in hero.get("effects", []))


@pytest.mark.asyncio
async def test_spell_round_retains_player_spell_even_without_a_structured_player_event(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    hero = {**copy.deepcopy(WIZARD), "hp_current": 30, "hp_max": 30}
    state = _combat_state(hero, "I cast Magic Missile at the priest.", parsed=ParsedInput(intent="combat", check="spell_attack", target="Priest"))
    state["combat_state"]["combatants"][1] = _priest(init=20, spells=SINGLE_BOLT.split("1st level")[0])
    state["defer_narration"] = True
    with use_rng(ScriptedDice([15, 6, 1, 2, 3])):
        final = _finish(await combat_round_node(state))
    assert final["narration"].index("casts Fire Bolt") < final["narration"].index("Magic Missile sends 3 darts")
    assert "9 force damage" in final["narration"]
