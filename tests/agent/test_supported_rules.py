"""EDG-028: the supported-rules contract, its code paths, and unsupported-action enforcement.

Supported mechanics are exercised directly. Unsupported declared actions are driven through a
fake provider that deliberately claims an effect; the claim must not reach the adjudication,
the round summary or the narration as an engine result.
"""

import dataclasses
import random
from pathlib import Path

import pytest

from agent.models.adjudication import AdjudicationResult, CharacterUpdate, FlagUpdate
from agent.models.parsed_input import ParsedInput
from agent.resolution import (
    COMPLETION_FLAG,
    CONDITIONS_5E,
    ability_mod,
    check_spec,
    decide_completion,
    enforce_side_effects,
    find_healing_consumable,
    heal_from_consumable,
    heal_from_rest,
    initiative_bonus,
    normalise_conditions,
    proficiency_bonus,
    resolve_attack,
    resolve_check,
    roll_attack_damage,
)
from agent.supported_rules import (
    SUPPORTED_MECHANICS,
    UNSUPPORTED_MECHANICS,
    apply_narration_boundary,
    boundary_prompt_block,
    classify_unsupported_actions,
    contract_payload,
    scope_notice,
    verify_references,
)
from tools.dice import use_rng

ROOT = Path(__file__).resolve().parents[2]

FIGHTER = {
    "id": 1,
    "name": "Eda",
    "class": "Fighter",
    "level": 1,
    "hp_current": 12,
    "hp_max": 12,
    "stats": {"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
    "inventory": {"weapons": ["longsword", "shield"], "armor": "chain mail"},
}
WIZARD = {
    "id": 2,
    "name": "Ilse",
    "class": "Wizard",
    "level": 1,
    "hp_current": 8,
    "hp_max": 8,
    "stats": {"STR": 8, "DEX": 14, "CON": 12, "INT": 16, "WIS": 10, "CHA": 10},
    "inventory": {"weapons": ["dagger"], "armor": ""},
}


# --------------------------------------------------------------------------------------------
# Contract consistency
# --------------------------------------------------------------------------------------------


def test_contract_references_resolve_to_code_and_tests() -> None:
    assert verify_references(ROOT) == []


def test_contract_ids_are_unique_and_disjoint() -> None:
    supported = [m.id for m in SUPPORTED_MECHANICS]
    unsupported = [m.id for m in UNSUPPORTED_MECHANICS]
    assert len(set(supported)) == len(supported)
    assert len(set(unsupported)) == len(unsupported)
    assert not set(supported) & set(unsupported)


def test_unsupported_entries_name_their_owner_or_are_unassigned() -> None:
    for mechanic in UNSUPPORTED_MECHANICS:
        assert mechanic.owner == "unassigned" or mechanic.owner.startswith("EDG-")


def test_public_payload_omits_internal_references() -> None:
    payload = contract_payload()
    text = repr(payload)
    assert "tests/" not in text and "implementation" not in text and "EDG-" not in text
    assert {m["id"] for m in payload["engine_executed"]} == {m.id for m in SUPPORTED_MECHANICS}
    assert {m["id"] for m in payload["not_supported"]} == {m.id for m in UNSUPPORTED_MECHANICS}
    assert "Exploding" not in scope_notice()  # not part of the simulated 5e subset


# --------------------------------------------------------------------------------------------
# Supported mechanics: one regression per contract entry
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("difficulty", "dc"),
    [("easy", 10), ("medium", 15), ("hard", 20), ("very_hard", 25), (None, 15)],
)
def test_supported_d20_checks_use_dc_tiers(difficulty, dc) -> None:
    for seed in range(20):
        result = resolve_check(FIGHTER, "athletics", difficulty, rng=random.Random(seed))
        assert result.dc == dc and result.against == "DC"
        # One d20 only: no advantage or disadvantage is applied.
        assert result.roll_expression == "1d20+5"
        assert result.success is (result.total >= dc)


def test_supported_ability_modifiers_and_proficiency() -> None:
    assert [ability_mod(s) for s in (1, 8, 9, 10, 11, 16, 20)] == [-5, -1, -1, 0, 0, 3, 5]
    assert [proficiency_bonus(lvl) for lvl in (1, 4, 5, 9, 13)] == [2, 2, 3, 4, 5]
    athletics = check_spec(FIGHTER, "athletics")
    assert athletics.proficient and athletics.modifier == 3 + 2
    stealth = check_spec(FIGHTER, "stealth")  # not a fighter default: no proficiency added
    assert not stealth.proficient and stealth.modifier == 1


def test_supported_weapon_attack_crit_and_fumble() -> None:
    crit_seen = fumble_seen = False
    for seed in range(200):
        hit = resolve_attack(
            FIGHTER, 30, hint="I strike with my longsword", rng=random.Random(seed)
        )
        assert hit.roll_expression == "1d20+5"  # STR 16 (+3) plus proficiency
        if hit.natural == 20:
            crit_seen = True
            assert hit.success and hit.critical
            _, dmg = roll_attack_damage(FIGHTER, hit, rng=random.Random(seed))
            assert dmg.expression.startswith("2d8")  # weapon dice doubled on a critical
        if hit.natural == 1:
            fumble_seen = True
            assert not hit.success and hit.fumble
        else:
            assert hit.success is (hit.natural == 20 or hit.total >= 30)
    assert crit_seen and fumble_seen


def test_supported_armor_class_from_table() -> None:
    from agent.graph_combat import _player_ac

    assert (
        _player_ac(
            {"stats": {"DEX": 16}, "inventory": {"armor": "half plate", "weapons": ["shield"]}}
        )
        == 19
    )
    assert _player_ac({"stats": {"DEX": 16}, "inventory": {"armor": "chain mail"}}) == 16
    assert _player_ac({"stats": {"DEX": 12}, "inventory": {"armor": ""}}) == 11


def test_supported_initiative_uses_dex() -> None:
    assert initiative_bonus(FIGHTER) == 1
    assert initiative_bonus({"stats": {"DEX": 16}}) == 3


def _enemy(name: str, *, hp: int, ac: int, init: int, attack_bonus: int = 3, dmg: str = "1d6+1"):
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp,
        "ac": ac,
        "attack_bonus": attack_bonus,
        "damage_dice": dmg,
        "initiative_bonus": 1,
        "init": init,
        "init_bonus": 1,
        "is_boss": False,
        "is_player": False,
        "alive": True,
    }


def _player(*, hp: int, ac: int, init: int, second_wind_used: bool = False) -> dict:
    return {
        "name": "player",
        "display_name": "Eda",
        "is_player": True,
        "hp_current": hp,
        "hp_max": 12,
        "ac": ac,
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "second_wind_used": second_wind_used,
    }


def _combat_state(combatants: list[dict], message: str, *, parsed=None) -> dict:
    return {
        "player_input": message,
        "session_id": 1,
        "campaign_id": 1,
        "messages": [],
        "character": dict(FIGHTER),
        "world_flags": {},
        "npcs": [],
        "adventure_collections": [],
        "parsed_input": parsed or ParsedInput(intent="combat", check="attack", target="goblin"),
        "combat_state": {"id": 7, "round": 1, "ended": False, "combatants": combatants},
    }


@pytest.mark.asyncio
async def test_supported_combat_round_victory(fake_llm_schema, fake_retrieval) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    victories = 0
    for seed in range(60):
        goblin = _enemy("Goblin", hp=1, ac=5, init=1)
        state = _combat_state([_player(hp=12, ac=13, init=20), goblin], "I attack the goblin")
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        combat = out["combat_state"]
        foe = next(c for c in combat["combatants"] if c["name"] == "Goblin")
        if foe["hp_current"] <= 0:
            victories += 1
            assert combat["ended"] and combat["outcome"] == "victory"
        else:
            assert not combat["ended"]
    assert victories > 0


@pytest.mark.asyncio
async def test_supported_second_wind_once_per_encounter(fake_llm_schema, fake_retrieval) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    rallies = 0
    for seed in range(40):
        # The ogre acts before the hero, has a hit that always exceeds 3 HP, and the hero's AC is 1.
        ogre = _enemy("Ogre", hp=30, ac=12, init=20, dmg="1d6+6")
        state = _combat_state(
            [_player(hp=3, ac=1, init=1), ogre],
            "I keep fighting the ogre",
            parsed=ParsedInput(intent="combat", check="attack", target="ogre"),
        )
        state["character"] = {**FIGHTER, "hp_current": 3}
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        sw = [d for d in out["adjudication_result"].dice_log if d.get("purpose") == "second wind"]
        assert len(sw) <= 1
        if sw:
            rallies += 1
            assert out["combat_state"]["second_wind_used"] is True
            hero = next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))
            assert hero["hp_current"] >= 1  # the rally keeps the hero in the fight
    assert rallies > 0

    # Once spent, a second explicit use does not roll again.
    spent = _combat_state(
        [_player(hp=3, ac=1, init=20, second_wind_used=True), _enemy("Ogre", hp=30, ac=12, init=1)],
        "I use Second Wind",
        parsed=ParsedInput(intent="combat"),
    )
    spent["character"] = {**FIGHTER, "hp_current": 3}
    out = await combat_round_node(spent)
    assert not [d for d in out["adjudication_result"].dice_log if d.get("purpose") == "second wind"]
    assert "already used Second Wind" in out["adjudication_result"].mechanical_summary


def test_supported_hazard_damage_needs_failed_check() -> None:
    base = resolve_check(FIGHTER, "dex", "medium", rng=random.Random(0))
    failed = dataclasses.replace(base, success=False)
    passed = dataclasses.replace(base, success=True)

    adj = AdjudicationResult(
        success=False, character_update=CharacterUpdate(damage_dice="1d6", hp_delta=-6)
    )
    enforce_side_effects(adj, character=FIGHTER, check_result=failed, rng=random.Random(1))
    assert adj.damage and adj.damage >= 1
    assert any(d.get("purpose") == "damage taken" for d in adj.dice_log)

    adj = AdjudicationResult(success=True, character_update=CharacterUpdate(damage_dice="1d6"))
    enforce_side_effects(adj, character=FIGHTER, check_result=passed, rng=random.Random(1))
    assert adj.damage is None
    assert adj.character_update.hp_delta in (None, 0)


def test_supported_consumable_healing_needs_inventory() -> None:
    with_potion = {**FIGHTER, "inventory": {"items": ["potion of healing"]}}
    item = find_healing_consumable(
        with_potion, player_input="I drink a potion of healing", parsed=None
    )
    assert item == "potion of healing"
    heal = heal_from_consumable(with_potion, item, rng=random.Random(3))
    assert heal is not None and 4 <= heal.amount <= 10  # 2d4+2
    without = {**FIGHTER, "inventory": {"items": ["rope"]}}
    assert find_healing_consumable(without, player_input="I drink a potion of healing") is None


def test_supported_long_rest_restores_hp() -> None:
    healed = heal_from_rest({"hp_current": 5, "hp_max": 12, "class": "Fighter"}, "long")
    assert healed is not None and healed.amount == 7


def test_supported_short_rest_has_no_ledger() -> None:
    character = {**FIGHTER, "hp_current": 5}
    before = dict(character)
    for seed in range(5):
        healed = heal_from_rest(character, "short", rng=random.Random(seed))
        assert healed is not None and healed.dice is not None
        # 1d10 (fighter hit die) + CON 14 (+2), minimum 1.
        assert 3 <= healed.amount <= 12
        assert healed.dice.expression == "1d10+2"
    assert character == before  # no Hit Dice count is written anywhere


def test_supported_conditions_are_tags_only() -> None:
    assert len(CONDITIONS_5E) == 15
    assert normalise_conditions(["Blind", "stunned", "dragon", "prone"]) == [
        "blinded",
        "stunned",
        "prone",
    ]


def test_supported_completion_needs_confirmation() -> None:
    adj = AdjudicationResult(
        success=True, flags_set=[FlagUpdate(key=COMPLETION_FLAG, value="true")]
    )
    status = decide_completion(adj, world_flags={}, combat_active=False, boss_victory=False)
    assert status == "pending"
    assert COMPLETION_FLAG not in {f.key for f in adj.flags_set}


# --------------------------------------------------------------------------------------------
# Classification of declared actions
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("I cast magic missile at the goblin", ["spell"]),
        ("I cast a cantrip at the goblin", ["spell"]),
        ("I shove the goblin and pin it to the floor", ["maneuver"]),
        ("I grapple the guard", ["maneuver"]),
        ("I attack with advantage", ["advantage"]),
        ("I attack from behind with disadvantage", ["advantage"]),
        ("I use Action Surge and attack twice", ["feature"]),
        ("I take the dodge action", ["action_economy"]),
        ("I use my bonus action to hide", ["action_economy"]),
        ("I stand in cover and blind the goblin", ["condition", "positioning"]),
        ("I flank the ogre from the high ground", ["positioning"]),
        ("I attack the goblin", []),
        ("I drink a potion of healing", []),
        ("I use Second Wind", []),
        ("I take a short rest", []),
        ("I search the room for traps", []),
        ("Lancio Dardo Incantato sul goblin.", ["spell"]),
        ("I cast a glance at the door", []),
        ("I take advantage of the distraction to slip past", []),
        ("I cast my net over the crowd of fishers", []),
        ("I disarm the trap with my thieves tools", []),
    ],
)
def test_declared_actions_are_classified(text, kinds) -> None:
    assert [a.kind for a in classify_unsupported_actions(text)] == kinds


def test_parsed_spell_attack_is_a_declared_spell_even_without_spell_words() -> None:
    parsed = ParsedInput(intent="combat", check="spell_attack", target="goblin")
    assert [a.kind for a in classify_unsupported_actions("I hurl force at the goblin", parsed)] == [
        "spell"
    ]


# --------------------------------------------------------------------------------------------
# Unsupported declared actions with a claiming fake provider
# --------------------------------------------------------------------------------------------


def _rules_state(parsed: ParsedInput, text: str, **extra) -> dict:
    return {
        "player_input": text,
        "parsed_input": parsed,
        "character": FIGHTER,
        "world_flags": {},
        "current_scene_id": "cliff_base",
        "rules_context": [],
        **extra,
    }


@pytest.mark.asyncio
async def test_claimed_spell_effect_is_never_rolled_or_persisted(fake_llm_schema) -> None:
    from agent.nodes.rules_adjudicator import rules_adjudicator_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        damage=7,
        mechanical_summary="Eldritch blast hits the goblin for 3d4 force damage.",
        conditions=["stunned"],
        character_update=CharacterUpdate(
            hp_delta=-7, damage_dice="3d4", add_conditions=["stunned"]
        ),
        flags_set=[
            FlagUpdate(key="goblin_pinned", value="true"),
            FlagUpdate(key="goblin_stunned", value="true"),
        ],
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="spell_attack", target="goblin"),
        "I cast eldritch blast at the goblin",
    )
    out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.check is None and adj.dice_log == [] and adj.dice_result is None
    assert adj.damage is None and adj.conditions == []
    assert adj.character_update is None or (
        adj.character_update.damage_dice is None
        and adj.character_update.add_conditions == []
        and adj.character_update.hp_delta in (None, 0)
    )
    assert adj.flags_set == []
    assert "3d4" not in adj.mechanical_summary and "hits the goblin" not in adj.mechanical_summary
    assert "Narrative ruling" in adj.mechanical_summary


@pytest.mark.asyncio
async def test_claimed_maneuver_rolls_plain_check_and_records_no_restraint(fake_llm_schema) -> None:
    from agent.nodes.rules_adjudicator import rules_adjudicator_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        mechanical_summary="The goblin is pinned and restrained by your rope.",
        conditions=["restrained", "prone"],
        character_update=CharacterUpdate(add_conditions=["restrained", "prone"]),
        flags_set=[FlagUpdate(key="goblin_restrained", value="true")],
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="athletics", target="goblin"),
        "I pin the goblin to the floor with my rope",
    )
    with use_rng(random.Random(5)):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.check == "athletics"  # only the ordinary check is rolled
    assert adj.conditions == [] and adj.flags_set == []
    assert adj.character_update is None or adj.character_update.add_conditions == []
    assert "pinned" not in adj.mechanical_summary and "restrained by" not in adj.mechanical_summary
    assert "Narrative ruling: this maneuver has no engine effect" in adj.mechanical_summary
    # The engine's own roll line (it contains "1d20+N") survives the boundary.
    assert "athletics:" in adj.mechanical_summary and "d20 rolled" in adj.mechanical_summary


@pytest.mark.asyncio
async def test_claimed_advantage_is_not_applied(fake_llm_schema) -> None:
    from agent.nodes.rules_adjudicator import rules_adjudicator_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, mechanical_summary="You strike with advantage and crit."
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="attack", target="goblin"),
        "I attack the goblin with advantage",
    )
    with use_rng(random.Random(2)):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.roll_expression == "1d20+5"
    assert "You strike" not in adj.mechanical_summary
    assert "advantage and disadvantage are not applied" in adj.mechanical_summary


@pytest.mark.asyncio
async def test_narration_cannot_present_unsupported_effect_as_executed(fake_llm_schema) -> None:
    from agent.nodes.narrator import narrator_node

    fake_llm_schema.narration = (
        "You weave an eldritch blast that strikes the goblin for 3d4 force damage. "
        "The goblin is pinned to the wall. You feel the spell drain your slot. "
        "What do you do next?"
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="spell_attack", target="goblin"),
        "I cast eldritch blast at the goblin",
        adjudication_result=AdjudicationResult(
            success=True,
            mechanical_summary="Narrative ruling: the declared spell has no engine mechanics here.",
        ),
    )
    out = await narrator_node(state)
    narration = out["narration"]
    # The appended ruling may name these mechanics; the claimed sentences must not survive.
    for claim in ("3d4", "pinned to the wall", "drain your slot", "eldritch blast that strikes"):
        assert claim not in narration
    assert "What do you do next?" in narration
    assert "Narrative ruling" in narration


def test_clean_narration_still_states_the_ruling() -> None:
    actions = classify_unsupported_actions("I take the dodge action")
    text = apply_narration_boundary("The goblin lunges at you. What now?", actions)
    assert text.startswith("The goblin lunges at you. What now?")
    assert "Narrative ruling: action economy is not tracked" in text


def test_supported_turns_are_untouched() -> None:
    assert classify_unsupported_actions("I attack the goblin") == ()
    assert apply_narration_boundary("A plain line.", ()) == "A plain line."
    assert boundary_prompt_block(()) is None


def test_boundary_reaches_the_adjudicator_and_narrator_prompts() -> None:
    from agent.nodes.narrator import build_narrator_prompt
    from agent.nodes.rules_adjudicator import build_adjudicator_prompt

    state = _rules_state(
        ParsedInput(intent="combat", check="spell_attack"), "I cast fireball at the goblin"
    )
    adj_prompt = build_adjudicator_prompt(state, None)[1]["content"]
    assert "Supported-action boundary for this turn" in adj_prompt
    assert "no engine mechanics" in adj_prompt
    narr_state = {**state, "adjudication_result": AdjudicationResult(success=True)}
    system, user = build_narrator_prompt(narr_state)
    assert "Supported-action boundary for this turn" in user.content
    assert "Simplified rules scope" in system.content
