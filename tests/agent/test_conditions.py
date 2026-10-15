"""EDG-043 (first slice): spell conditions applied by the engine to foes.

Blinded (Blindness/Deafness: a Constitution save, a repeat save at the end of each foe turn, one
minute) and marked (Guiding Bolt: the next attack roll against the foe has advantage). Dice are
scripted where the exact order matters (`ScriptedDice`, the same helper the spell tests use) and
seeded elsewhere; every expectation is a rules outcome, not a copy of the engine output.
"""

import json
import random

import pytest

from agent.combatants import attack_roll_mode, effects_of, view_fields
from agent.conditions import (
    apply_spell_condition,
    consume_mark,
    end_of_turn_saves,
    expire_effects,
)
from agent.graph_combat import combat_round_node, initiative_view
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterStats
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.spells import CATALOG, cast_request, plan_cast, resolve_spell
from agent.supported_rules import (
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from tests.agent.test_spells import CLERIC, ScriptedDice, _cast, _combat_state, _rules_state
from tools.dice import use_rng

# A cleric at level 3 has second-level slots (Blindness/Deafness); its spell save DC is 13.
CLERIC_3 = {**CLERIC, "level": 3}
GUIDING_BOLT = next(s for s in CATALOG if s.name == "Guiding Bolt")
BLINDNESS = next(s for s in CATALOG if s.name == "Blindness/Deafness")


def _foe_effect(kind: str = "blinded", **extra) -> dict:
    return {"kind": kind, "source": "Blindness/Deafness", "applied_round": 1, **extra}


def _foe_in(out: dict, name: str = "Goblin") -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


def _summary(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


def _next_round(out: dict, message: str, parsed: ParsedInput, *, round_num: int) -> dict:
    """The saved combat row round-tripped through JSON, as the next turn reads it."""
    saved = json.loads(json.dumps(out["combat_state"]["combatants"]))
    follow = _combat_state(CLERIC_3, message, goblin_hp=30, parsed=parsed)
    follow["combat_state"]["combatants"] = saved
    follow["combat_state"]["round"] = round_num
    return follow


# --- pure model: attack roll modes and the timed effects ------------------------------------


@pytest.mark.parametrize(
    ("attacker", "target", "ranged", "mode"),
    [
        # A blinded attacker rolls at disadvantage; attacks against it have advantage.
        ({"effects": [_foe_effect()]}, {}, False, "disadvantage"),
        ({}, {"effects": [_foe_effect()]}, False, "advantage"),
        ({}, {"effects": [_foe_effect()]}, True, "advantage"),
        # A mark gives advantage to the attack roll made against the foe, at range too.
        ({}, {"effects": [_foe_effect("marked")]}, True, "advantage"),
        # Opposing sources cancel to normal, as the rules do.
        ({"effects": [_foe_effect()]}, {"effects": [_foe_effect("marked")]}, False, "normal"),
        ({"effects": [_foe_effect()]}, {"effects": [{"kind": "restrained"}]}, False, "normal"),
        # Both sources give disadvantage here: a blinded attacker against a prone target at range.
        ({"effects": [_foe_effect()]}, {"effects": [{"kind": "prone"}]}, True, "disadvantage"),
    ],
)
def test_blinded_and_marked_follow_the_attack_roll_modes(attacker, target, ranged, mode) -> None:
    assert attack_roll_mode(attacker, target, ranged=ranged) == mode


def test_apply_refuses_kinds_this_module_does_not_apply() -> None:
    foe = {"name": "Goblin", "hp_current": 7}
    with pytest.raises(ValueError):
        apply_spell_condition(foe, "restrained", source="x", round_applied=1, duration_rounds=1)
    assert "effects" not in foe


def test_timed_effects_end_after_their_last_round_only() -> None:
    goblin = {"name": "Goblin", "hp_current": 7}
    assert apply_spell_condition(
        goblin,
        "blinded",
        source="Blindness/Deafness",
        round_applied=2,
        duration_rounds=10,
        repeat_save={"ability": "CON", "dc": 13},
    )
    assert apply_spell_condition(
        goblin, "marked", source="Guiding Bolt", round_applied=2, duration_rounds=2
    )
    assert effects_of(goblin)[0]["expires_after_round"] == 11
    assert effects_of(goblin)[1]["expires_after_round"] == 3
    # Round 3 is the caster's next turn: the mark ends there, the minute does not.
    assert expire_effects([goblin], 2) == []
    assert expire_effects([goblin], 3) == [("Goblin", "marked")]
    assert [e["kind"] for e in effects_of(goblin)] == ["blinded"]
    assert expire_effects([goblin], 11) == [("Goblin", "blinded")]
    assert effects_of(goblin) == []


def test_consuming_a_mark_happens_once() -> None:
    goblin = {"name": "Goblin", "hp_current": 7}
    assert consume_mark(goblin) is False
    apply_spell_condition(
        goblin, "marked", source="Guiding Bolt", round_applied=1, duration_rounds=2
    )
    assert consume_mark(goblin) is True
    assert effects_of(goblin) == [] and consume_mark(goblin) is False


def test_repeat_save_uses_the_stated_bonus_and_ends_the_effect_on_success() -> None:
    stats = MonsterStats(name="Bandit", hp_max=11, ac=12, saves={"CON": 4})
    bandit = {
        "name": "Bandit",
        "hp_current": 11,
        "monster_stats": stats.to_dict(),
    }
    apply_spell_condition(
        bandit,
        "blinded",
        source="Blindness/Deafness",
        round_applied=1,
        duration_rounds=10,
        repeat_save={"ability": "CON", "dc": 13},
    )
    with use_rng(ScriptedDice([8])):  # 8 + 4 = 12 keeps the effect
        (kept,) = end_of_turn_saves(bandit)
    assert kept.total == 12 and not kept.ended and effects_of(bandit)
    with use_rng(ScriptedDice([9])):  # 9 + 4 = 13 meets DC 13 and ends it
        (ended,) = end_of_turn_saves(bandit)
    assert ended.total == 13 and ended.ended and effects_of(bandit) == []


def test_legacy_rows_and_plain_restraints_keep_their_behaviour() -> None:
    # A row written before timed effects existed: no expiry and no repeat save, so nothing ends it.
    legacy = {
        "name": "Goblin",
        "hp_current": 7,
        "effects": [{"kind": "blinded", "source": "x", "applied_round": 2}],
    }
    assert attack_roll_mode(legacy, {}) == "disadvantage"
    assert expire_effects([legacy], 99) == [] and effects_of(legacy)
    assert end_of_turn_saves(legacy) == []
    assert "expires_after_round" not in view_fields(legacy)["effects"][0]
    restrained = {
        "name": "Wolf",
        "hp_current": 3,
        "effects": [{"kind": "restrained", "source": "rope", "applied_round": 1}],
    }
    assert expire_effects([restrained], 99) == [] and effects_of(restrained)


def test_timed_effects_survive_a_json_round_trip_and_reach_the_hud() -> None:
    goblin = {"name": "Goblin", "hp_current": 7}
    apply_spell_condition(
        goblin,
        "blinded",
        source="Blindness/Deafness",
        round_applied=3,
        duration_rounds=10,
        repeat_save={"ability": "CON", "dc": 13},
    )
    goblin["init"] = 5
    stored = json.loads(
        json.dumps([{"name": "player", "is_player": True, "hp_current": 9, "init": 15}, goblin])
    )
    reloaded = next(c for c in stored if c["name"] == "Goblin")
    assert attack_roll_mode({}, reloaded) == "advantage"
    view = next(c for c in initiative_view(stored) if c["name"] == "Goblin")
    assert view["effects"] == [
        {
            "kind": "blinded",
            "source": "Blindness/Deafness",
            "applied_round": 3,
            "expires_after_round": 12,
        }
    ]
    with use_rng(ScriptedDice([6])):
        (save,) = end_of_turn_saves(reloaded)
    assert save.kind == "blinded" and not save.ended and effects_of(reloaded)


# --- the spells -------------------------------------------------------------------------------


def test_guiding_bolt_and_blindness_are_declared_catalog_spells() -> None:
    assert GUIDING_BOLT.kind == "attack" and GUIDING_BOLT.condition == "marked"
    assert (GUIDING_BOLT.dice, GUIDING_BOLT.die, GUIDING_BOLT.damage_type) == (4, 6, "radiant")
    assert BLINDNESS.kind == "condition" and BLINDNESS.condition == "blinded"
    assert (BLINDNESS.level, BLINDNESS.save_ability, BLINDNESS.duration_rounds) == (2, "CON", 10)
    request = cast_request("I cast Blindness at the goblin", None, CLERIC_3)
    assert request is not None and request[0] is BLINDNESS and request[2] is None


def test_blindness_outside_combat_spends_nothing_and_rolls_nothing() -> None:
    _spell, plan, refusal = cast_request("I cast Blindness/Deafness at the goblin", None, CLERIC_3)
    assert refusal is None
    result = resolve_spell(CLERIC_3, plan)  # no target: no foe is tracked
    assert result.condition is None and result.dice_log == []


def test_guiding_bolt_upcast_adds_a_die_per_slot_above_first() -> None:
    plan, refusal = plan_cast(CLERIC_3, GUIDING_BOLT, requested_slot=2)
    assert refusal is None and plan is not None and plan.slot_level == 2
    result = resolve_spell(CLERIC_3, plan, target_ac=12, rng=ScriptedDice([15, 1, 1, 1, 1, 1]))
    damage = next(d for d in result.dice_log if d.get("purpose") == "Guiding Bolt damage")
    assert damage["expression"] == "5d6" and result.damage == 5


def test_deafness_declaration_is_refused_and_never_blinds() -> None:
    _spell, plan, refusal = cast_request(
        "I cast Blindness/Deafness to deafen the goblin", None, CLERIC_3
    )
    assert plan is None
    assert refusal == (
        "Blindness/Deafness is not cast: this engine applies only blindness, so no spell "
        "effect is applied."
    )


# --- the combat round -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blindness_failed_save_blinds_and_the_repeat_save_ends_it(
    fake_llm_schema, fake_retrieval
) -> None:
    """The CON save fails and blinds the goblin (DC 13). The goblin then attacks at disadvantage
    and repeats its save at the end of its turn. A failed repeat keeps the effect; a success ends it.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    cast = "I cast Blindness/Deafness at the goblin"
    # Cast save 12 + 0 fails DC 13. Goblin attack: disadvantage, draws 2 and 3, misses AC 14.
    # Its repeat save: 6 fails, the effect is kept.
    state = _combat_state(CLERIC_3, cast, parsed=_cast("I cast"))
    with use_rng(ScriptedDice([12, 2, 3, 6])):
        kept = await combat_round_node(state)
    assert _foe_in(kept)["effects"] == [
        _foe_effect(
            applied_round=1,
            expires_after_round=10,
            save={"ability": "CON", "dc": 13},
        )
    ]
    assert "Goblin fails the CON save against Blindness/Deafness (DC 13)." in _summary(kept)
    assert "Goblin is blinded: its attacks have disadvantage" in _summary(kept)
    attack = _entries(kept, "Goblin attack")[0]
    assert attack["roll_mode"] == "disadvantage" and attack["rolls"] == [2, 3]
    assert (
        "Goblin repeats its CON save against blinded (rolled 6 vs DC 13) and keeps the effect."
        in _summary(kept)
    )
    assert kept["adjudication_result"].character_update.spell_slots_expended == {"2": 1}

    # Round 2: the cleric attacks the blinded goblin (advantage). The goblin then attacks at
    # disadvantage and repeats its save. The engine's dice pick the seed whose repeat save succeeds.
    for seed in range(300):
        follow = _next_round(
            kept,
            "I attack the goblin",
            ParsedInput(intent="combat", check="attack", target="goblin"),
            round_num=2,
        )
        with use_rng(random.Random(seed)):
            ended = await combat_round_node(follow)
        if "ends the effect" in _summary(ended):
            break
    else:
        pytest.fail("no seed produced a successful repeat save")
    assert _entries(ended, "attack check")[0]["roll_mode"] == "advantage"
    assert _entries(ended, "Goblin attack")[0]["roll_mode"] == "disadvantage"
    assert _foe_in(ended)["effects"] == []


@pytest.mark.asyncio
async def test_successful_blindness_save_records_no_condition(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _combat_state(
        CLERIC_3, "I cast Blindness/Deafness at the goblin", parsed=_cast("I cast")
    )
    with use_rng(ScriptedDice([15, 2])):  # the save 15 succeeds; the goblin's attack is normal
        out = await combat_round_node(state)
    assert _foe_in(out).get("effects", []) == []
    assert (
        "succeeds on the CON save against Blindness/Deafness (DC 13) and is unaffected."
        in _summary(out)
    )
    attack = _entries(out, "Goblin attack")[0]
    assert len(attack["rolls"]) == 1 and attack.get("roll_mode") is None


@pytest.mark.asyncio
async def test_guiding_bolt_hit_marks_and_the_next_attack_spends_the_mark(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    # Attack 15 + 5 hits AC 12; 4d6 = 14; the goblin's attack 2 + 3 misses AC 14.
    state = _combat_state(
        CLERIC, "I cast Guiding Bolt at the goblin", goblin_hp=30, parsed=_cast("I cast")
    )
    with use_rng(ScriptedDice([15, 2, 3, 4, 5, 2])):
        marked = await combat_round_node(state)
    foe = _foe_in(marked)
    assert foe["hp_current"] == 16
    assert foe["effects"] == [
        {"kind": "marked", "source": "Guiding Bolt", "applied_round": 1, "expires_after_round": 2}
    ]
    assert "Goblin is marked: the next attack roll against it has advantage." in _summary(marked)
    assert marked["adjudication_result"].character_update.spell_slots_expended == {"1": 1}

    # Round 2: the cleric's weapon attack rolls at advantage, and that roll spends the mark.
    follow = _next_round(
        marked,
        "I attack the goblin",
        ParsedInput(intent="combat", check="attack", target="goblin"),
        round_num=2,
    )
    with use_rng(random.Random(3)):
        attacked = await combat_round_node(follow)
    assert _entries(attacked, "attack check")[0]["roll_mode"] == "advantage"
    assert _foe_in(attacked)["effects"] == []


@pytest.mark.asyncio
async def test_unspent_mark_ends_at_the_end_of_the_casters_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _combat_state(
        CLERIC, "I cast Guiding Bolt at the goblin", goblin_hp=30, parsed=_cast("I cast")
    )
    with use_rng(ScriptedDice([15, 2, 3, 4, 5, 2])):
        marked = await combat_round_node(state)
    # Sacred Flame is a save, not an attack roll: it does not spend the mark, so the mark ends at
    # the end of the cleric's turn in this round.
    follow = _next_round(marked, "I cast Sacred Flame at the goblin", _cast("I cast"), round_num=2)
    with use_rng(random.Random(5)):
        cast = await combat_round_node(follow)
    assert _foe_in(cast)["effects"] == []
    assert "Goblin is no longer marked." in _summary(cast)


@pytest.mark.asyncio
async def test_narrated_conditions_on_a_foe_record_no_effect(
    fake_llm_schema, fake_retrieval
) -> None:
    """Prose that says a foe is poisoned or frightened applies nothing, and an attack made without a
    spell keeps its normal roll: narration never supplies an effect."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    fake_llm_schema.narration = (
        "Your blade bites; the goblin looks poisoned and frightened. What next?"
    )
    state = _combat_state(
        CLERIC_3,
        "I attack the goblin",
        goblin_hp=30,
        parsed=ParsedInput(intent="combat", check="attack", target="goblin"),
    )
    with use_rng(random.Random(8)):
        out = await combat_round_node(state)
    assert _foe_in(out).get("effects", []) == []
    assert all("roll_mode" not in e for e in _entries(out, "Goblin attack"))


@pytest.mark.asyncio
async def test_refused_deafness_declaration_spends_no_slot_and_rolls_nothing(
    fake_llm_schema, fake_retrieval
) -> None:
    state = _combat_state(
        CLERIC_3, "I cast Blindness/Deafness to deafen the goblin", parsed=_cast("I cast")
    )
    with use_rng(ScriptedDice([])):
        out = await combat_round_node(state)
    assert "applies only blindness" in out["narration"]
    assert _foe_in(out).get("effects", []) == [] and out["combat_state"]["round"] == 1
    assert out["adjudication_result"].character_update is None


@pytest.mark.asyncio
async def test_blindness_outside_combat_is_refused_before_a_slot_is_spent(
    fake_llm_schema,
) -> None:
    state = _rules_state(
        CLERIC_3, "I cast Blindness/Deafness at the goblin", ParsedInput(intent="exploration")
    )
    with use_rng(ScriptedDice([])):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.success is False and "needs a foe in combat" in adj.mechanical_summary
    assert adj.character_update is None and adj.dice_log == []


# --- the narration boundary -------------------------------------------------------------------


def test_narrated_blindness_survives_only_when_the_engine_blinded_the_foe() -> None:
    text = "I cast Blindness/Deafness at the goblin"
    narration = "The goblin is blinded and staggers. What do you do next?"
    unrecorded = classify_unsupported_actions(text, character=CLERIC_3)
    assert [a.kind for a in unrecorded] == ["condition"]
    cleaned = apply_narration_boundary(narration, unrecorded)
    assert "The goblin is blinded" not in cleaned and "What do you do next?" in cleaned
    assert "recorded no blinded condition" in cleaned

    recorded = classify_unsupported_actions(
        text, character=CLERIC_3, applied=EngineApplied(effects=("blinded",))
    )
    assert recorded == ()
    assert apply_narration_boundary(narration, recorded) == narration


def test_other_conditions_are_still_ruled_when_the_engine_blinded_a_foe() -> None:
    text = "I cast Blindness/Deafness at the goblin and try to frighten it"
    actions = classify_unsupported_actions(
        text, character=CLERIC_3, applied=EngineApplied(effects=("blinded",))
    )
    assert [a.kind for a in actions] == ["condition"]
    narration = "The goblin is blinded. It is frightened now."
    cleaned = apply_narration_boundary(
        narration, actions, applied=EngineApplied(effects=("blinded",))
    )
    assert "The goblin is blinded." in cleaned
    assert "frightened" not in cleaned and "Narrative ruling" in cleaned


def test_executed_guiding_bolt_is_not_an_unsupported_action() -> None:
    assert classify_unsupported_actions("I cast Guiding Bolt at the goblin", character=CLERIC) == ()
