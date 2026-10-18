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
    RollModeSource,
    ability_mod,
    check_spec,
    combine_roll_modes,
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
    roll_mode_for_sources,
    second_wind_after_rest,
    second_wind_remaining,
    second_wind_status,
)
from agent.supported_rules import (
    SOLO_ADAPTATIONS,
    SUPPORTED_MECHANICS,
    UNSUPPORTED_MECHANICS,
    EngineApplied,
    actions_for_state,
    apply_narration_boundary,
    boundary_prompt_block,
    classify_unsupported_actions,
    contract_payload,
    engine_applied_for_state,
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


def test_supported_advantage_and_disadvantage() -> None:
    # Engine-owned sources only; both faces are logged and the kept one is used.
    for seed in range(20):
        adv = resolve_check(
            FIGHTER, "athletics", "medium", roll_mode="advantage", rng=random.Random(seed)
        )
        dis = resolve_check(
            FIGHTER, "athletics", "medium", roll_mode="disadvantage", rng=random.Random(seed)
        )
        assert adv.natural == max(adv.natural_rolls) and dis.natural == min(dis.natural_rolls)
        assert adv.natural_rolls == dis.natural_rolls and len(adv.natural_rolls) == 2
        assert adv.dice_log[0]["rolls"] == adv.natural_rolls
        dice = random.Random(seed)
        assert adv.natural_rolls == [dice.randint(1, 20), dice.randint(1, 20)]
    # Advantage with disadvantage is a normal roll.
    cancelled = resolve_check(
        FIGHTER,
        "athletics",
        roll_mode=combine_roll_modes("advantage", "disadvantage"),
        rng=random.Random(9),
    )
    assert cancelled.roll_expression == "1d20+5" and cancelled.roll_mode == "normal"
    # A mode that is not an engine source is rejected, never applied.
    with pytest.raises(ValueError):
        roll_mode_for_sources(["player_claimed_advantage"])
    assert roll_mode_for_sources([RollModeSource.TARGET_UNCONSCIOUS]) == "advantage"


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


def _sheet(*, remaining: int | None = None, **extra) -> dict:
    """A level-1 fighter sheet, optionally carrying the persisted Second Wind resource."""
    stats = dict(FIGHTER["stats"])
    if remaining is not None:
        stats["second_wind"] = {"uses_remaining": remaining}
    return {**FIGHTER, "hp_current": 5, "stats": stats, **extra}


def _second_wind_hits(dice_log: list[dict]) -> list[dict]:
    return [d for d in dice_log if d.get("purpose") == "second wind"]


@pytest.mark.asyncio
async def test_supported_second_wind_uses_and_rest_recovery(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    # The ogre acts after the hero and cannot hit AC 30 (only a natural 20 can), so HP is ours.
    safe_foe = {"attack_bonus": -20}

    # Use: one engine roll of 1d10+1, healing, and the sheet count is written as spent.
    state = _combat_state(
        [_player(hp=5, ac=30, init=20), _enemy("Ogre", hp=30, ac=12, init=1, **safe_foe)],
        "I use Second Wind",
        parsed=ParsedInput(intent="combat"),
    )
    state["character"] = _sheet(remaining=1)
    with use_rng(random.Random(4)):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    spent = _second_wind_hits(adj.dice_log)
    assert len(spent) == 1 and spent[0]["expression"] == "1d10+1"
    hero = next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))
    assert hero["hp_current"] == min(12, 5 + spent[0]["total"])
    assert adj.character_update.second_wind_uses_remaining == 0
    assert out["combat_state"]["second_wind_uses_remaining"] == 0
    assert out["combat_state"]["second_wind_used"] is True

    # Exhausted on the sheet: a new encounter (fresh combatants) still refuses without a roll.
    spent_state = _combat_state(
        [_player(hp=5, ac=30, init=20), _enemy("Ogre", hp=30, ac=12, init=1, **safe_foe)],
        "I use Second Wind",
        parsed=ParsedInput(intent="combat"),
    )
    spent_state["character"] = _sheet(remaining=0)
    out = await combat_round_node(spent_state)
    adj = out["adjudication_result"]
    assert not _second_wind_hits(adj.dice_log)
    assert "has already used Second Wind" in adj.mechanical_summary
    assert adj.character_update.second_wind_uses_remaining is None  # nothing new to persist
    assert out["combat_state"]["second_wind_uses_remaining"] == 0

    # Out of combat the same use is an engine effect, and a refused one rolls nothing.
    adj = AdjudicationResult(success=True)
    enforce_side_effects(
        adj,
        character=_sheet(remaining=1),
        check_result=None,
        player_input="I use Second Wind",
        rng=random.Random(9),
    )
    healed = [d for d in adj.dice_log if d.get("purpose") == "healing (second_wind)"]
    assert len(healed) == 1 and adj.character_update.second_wind_uses_remaining == 0
    assert adj.character_update.hp_delta == healed[0]["total"]
    adj = AdjudicationResult(success=True)
    enforce_side_effects(
        adj,
        character=_sheet(remaining=0),
        check_result=None,
        player_input="I use Second Wind",
        rng=random.Random(9),
    )
    assert not adj.dice_log and adj.character_update is None
    assert "has already used Second Wind" in adj.mechanical_summary

    # Recovery: a short or a long rest restores the single use; a full sheet is not rewritten.
    for rest in ("I take a short rest", "I take a long rest"):
        adj = AdjudicationResult(success=True)
        enforce_side_effects(
            adj,
            character=_sheet(remaining=0),
            check_result=None,
            player_input=rest,
            rng=random.Random(2),
        )
        assert adj.character_update.second_wind_uses_remaining == 1
        assert adj.character_update.hp_delta and adj.character_update.hp_delta > 0
        adj = AdjudicationResult(success=True)
        enforce_side_effects(
            adj,
            character=_sheet(remaining=1),
            check_result=None,
            player_input=rest,
            rng=random.Random(2),
        )
        assert adj.character_update.second_wind_uses_remaining is None


@pytest.mark.asyncio
async def test_second_wind_never_triggers_at_zero_hp_by_default(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    for seed in range(40):
        # The ogre acts before the hero and always hits; a blow that drops the hero to 0 HP.
        ogre = _enemy("Ogre", hp=30, ac=12, init=20, dmg="1d6+6")
        state = _combat_state(
            [_player(hp=3, ac=1, init=1), ogre],
            "I keep fighting the ogre",
            parsed=ParsedInput(intent="combat", check="attack", target="ogre"),
        )
        state["character"] = {**_sheet(remaining=1), "hp_current": 3}
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        adj = out["adjudication_result"]
        assert not _second_wind_hits(adj.dice_log)
        assert adj.character_update.second_wind_uses_remaining is None
        assert out["combat_state"]["second_wind_uses_remaining"] == 1
        hero = next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))
        if "Ogre hits" in adj.mechanical_summary:
            assert hero["hp_current"] <= 1  # a blow of 7+ drops the hero: subdued at 1, no rally
        else:
            assert hero["hp_current"] == 3  # a natural-1 miss leaves the hero untouched


@pytest.mark.asyncio
async def test_second_wind_auto_rally_is_an_explicit_off_by_default_mode(
    monkeypatch, fake_llm_schema, fake_retrieval
) -> None:
    from agent import graph_combat
    from agent.graph_combat import combat_round_node

    monkeypatch.setattr(graph_combat, "SECOND_WIND_AUTO_AT_ZERO_HP", True)
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    rallies = 0
    for seed in range(40):
        ogre = _enemy("Ogre", hp=30, ac=12, init=20, dmg="1d6+6")
        state = _combat_state(
            [_player(hp=3, ac=1, init=1), ogre],
            "I keep fighting the ogre",
            parsed=ParsedInput(intent="combat", check="attack", target="ogre"),
        )
        state["character"] = {**_sheet(remaining=1), "hp_current": 3}
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        sw = _second_wind_hits(out["adjudication_result"].dice_log)
        assert len(sw) <= 1
        if sw:
            rallies += 1
            assert out["combat_state"]["second_wind_uses_remaining"] == 0
            assert out["adjudication_result"].character_update.second_wind_uses_remaining == 0
    assert rallies > 0


def test_second_wind_legacy_sheet_derives_full_uses_and_clamps() -> None:
    assert second_wind_remaining(FIGHTER) == 1  # no key written: the full use is available
    assert second_wind_remaining(_sheet(remaining=0)) == 0
    assert second_wind_remaining(_sheet(remaining=7)) == 1
    malformed = {**FIGHTER, "stats": {**FIGHTER["stats"], "second_wind": "spent"}}
    assert second_wind_remaining(malformed) == 1
    assert second_wind_remaining(WIZARD) == 0
    assert second_wind_status(FIGHTER) == {"uses_remaining": 1, "uses_max": 1}
    assert second_wind_status(WIZARD) is None
    assert second_wind_after_rest(WIZARD, "long") is None
    assert second_wind_after_rest(_sheet(remaining=0), "short") == 1
    assert second_wind_after_rest(_sheet(remaining=0), None) is None


def test_second_wind_contract_matches_the_rule() -> None:
    entry = next(m for m in SUPPORTED_MECHANICS if m.id == "second_wind")
    text = f"{entry.title} {entry.scope}".lower()
    assert "once per encounter" not in text and "encounter-based" not in text
    assert "automatically" not in text and "rallies" not in text
    assert "one use" in text and "short or long rest" in text and "2024" in text
    assert "second_wind_recharge" not in {m.id for m in UNSUPPORTED_MECHANICS}
    assert "Second Wind" not in " ".join(SOLO_ADAPTATIONS)
    assert "Fighter Second Wind (one use" in scope_notice()
    # Declaring Second Wind is not a claimed effect: the boundary leaves the engine's narration.
    text = "You dig deep and Second Wind steadies you for 7 HP."
    assert apply_narration_boundary(text, classify_unsupported_actions("I use Second Wind")) == text


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


def test_supported_short_rest_spends_one_hit_die() -> None:
    character = {**FIGHTER, "hp_current": 5}
    before = dict(character)
    for seed in range(5):
        healed = heal_from_rest(character, "short", rng=random.Random(seed))
        assert healed is not None and healed.dice is not None
        # 1d10 (fighter hit die) + CON 14 (+2); the total is regained, with no minimum (PHB p.187).
        assert 3 <= healed.amount <= 12
        assert healed.dice.expression == "1d10+2"
    assert character == before  # the roll itself writes nothing: the engine's enforcement does

    # The rest spends one die and records it; with no die left it is refused without a roll.
    adj = AdjudicationResult(success=True)
    enforce_side_effects(
        adj, character=character, check_result=None, player_input="I take a short rest"
    )
    assert adj.character_update.hit_dice_spent == 1 and adj.rest_record.hit_dice_remaining == 0
    spent = {**character, "stats": {**character["stats"], "hit_dice": {"spent": 1}}}
    assert heal_from_rest(spent, "short", rng=random.Random(0)) is None
    refused = AdjudicationResult(success=True)
    enforce_side_effects(
        refused, character=spent, check_result=None, player_input="I take a short rest"
    )
    assert refused.rest_record.refused and not refused.dice_log


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


@pytest.mark.asyncio
async def test_supported_contested_maneuver_applies_effect(fake_llm_schema, fake_retrieval) -> None:
    """A contested grapple the engine wins records a restraint that changes the foe's attacks."""
    from tests.agent.test_combat import _enemy as foe_fixture, _player as hero_fixture
    from tests.agent.test_combat_effects import _entries, _find_contest, _foe

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out = await _find_contest(
        fake_llm_schema,
        lambda: [hero_fixture(init=20, ac=99), foe_fixture("Raider", init=5)],
        "I grapple the raider with my hands",
        ParsedInput(intent="combat", target="raider"),
        won=True,
    )
    assert _foe(out, "Raider")["effects"] == [
        {"kind": "grappled", "source": "hands", "applied_round": 1}
    ]
    assert "roll_mode" not in _entries(out, "Raider attack")[0]  # PHB p.291: grappled adds no penalty


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
        ("I take the dash action", ["action_economy"]),
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
    assert "advantage was not applied: no supported source" in adj.mechanical_summary


@pytest.mark.asyncio
async def test_llm_claimed_advantage_without_engine_source_does_not_change_the_roll(
    fake_llm_schema,
) -> None:
    from agent.nodes.rules_adjudicator import rules_adjudicator_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, mechanical_summary="With advantage you hit twice; roll again with advantage."
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="athletics", target="goblin"),
        "I roll with advantage and shove the goblin",
    )
    with use_rng(random.Random(12)):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    # The engine rolled the plain d20 with the same seed as an unclaimed check.
    plain = resolve_check(FIGHTER, "athletics", rng=random.Random(12))
    assert adj.roll_expression == "1d20+5" and adj.natural_roll == plain.natural
    assert adj.dice_log[0]["rolls"] == [plain.natural]
    assert "advantage was not applied" in adj.mechanical_summary


def test_engine_applied_advantage_is_described_not_stripped() -> None:
    declared = classify_unsupported_actions("I cast magic missile at the goblin")
    narration = "The darts fly true with advantage. Magic missile strikes the goblin twice."
    # Without an engine mode the advantage sentence is a claim and is removed with the spell.
    plain = apply_narration_boundary(narration, declared)
    assert "advantage" not in plain
    # With an engine-applied advantage it is a real roll and the sentence survives.
    applied = apply_narration_boundary(
        narration, declared, applied=EngineApplied(roll_mode="advantage")
    )
    assert "The darts fly true with advantage." in applied
    assert "Magic missile strikes" not in applied


@pytest.mark.asyncio
async def test_engine_applied_advantage_survives_adjudication(fake_llm_schema) -> None:
    from agent.nodes.rules_adjudicator import adjudicate, rules_adjudicator_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, mechanical_summary="You strike with advantage and hit."
    )
    state = _rules_state(
        ParsedInput(intent="combat", check="attack", target="goblin"),
        "I attack the goblin with advantage",
    )
    mode = roll_mode_for_sources([RollModeSource.TARGET_UNCONSCIOUS])
    engine = resolve_attack(FIGHTER, 12, roll_mode=mode, rng=random.Random(4))
    adj = await adjudicate(state, engine_check=engine, in_combat=True)
    assert state["engine_roll_mode"] == "advantage"
    # The real advantage is described (not stripped), and the engine states both faces.
    assert "You strike with advantage and hit." in adj.mechanical_summary
    assert "advantage was not applied" not in adj.mechanical_summary
    assert engine.describe() in adj.mechanical_summary
    assert adj.roll_expression == "2d20kh1+5" and adj.dice_log[0]["rolls"] == engine.natural_rolls
    # The node reports the mode it applied, so the turn runner can apply the same boundary.
    plain = await rules_adjudicator_node(
        _rules_state(ParsedInput(intent="combat", check="attack", target="goblin"), "I attack")
    )
    assert plain["engine_roll_mode"] == "normal"


def test_engine_applied_advantage_is_not_a_declared_unsupported_action() -> None:
    assert classify_unsupported_actions("I attack with advantage") != ()
    assert (
        classify_unsupported_actions(
            "I attack with advantage", applied=EngineApplied(roll_mode="advantage")
        )
        == ()
    )
    assert (
        classify_unsupported_actions(
            "I attack with advantage", applied=EngineApplied(roll_mode="disadvantage")
        )
        == ()
    )


def test_engine_applied_for_state_reads_only_the_recorded_engine_facts() -> None:
    assert engine_applied_for_state(None) == EngineApplied()
    assert engine_applied_for_state({}) == EngineApplied()
    # An unknown roll mode and a missing or null effect list are no engine fact at all.
    assert (
        engine_applied_for_state({"engine_roll_mode": "sideways", "engine_effects": None})
        == EngineApplied()
    )
    got = engine_applied_for_state(
        {
            "engine_roll_mode": "disadvantage",
            "engine_effects": ["restrained", "dodge"],
            "player_input": "I cast Magic Missile at the goblin",
            "character": WIZARD,
        }
    )
    assert got == EngineApplied(
        roll_mode="disadvantage", effects=("restrained", "dodge"), executed_spell=True
    )
    # A refused catalog spell is not executed by the engine.
    refused = {"player_input": "I cast Magic Missile at the goblin", "character": FIGHTER}
    assert not engine_applied_for_state(refused).executed_spell


def test_each_engine_fact_exempts_its_claim_through_the_applied_value() -> None:
    # Advantage: the engine's roll mode makes the advantage wording real, not declared.
    state = {"player_input": "I attack with advantage", "engine_roll_mode": "advantage"}
    assert actions_for_state(state) == ()
    assert actions_for_state({**state, "engine_roll_mode": "normal"})

    # A typed effect: a grapple the engine applied keeps its narration and is not declared. A restraint
    # is a different condition and does not make a grapple real (PHB p.196, p.293).
    pin = {"player_input": "I pin the goblin", "engine_effects": ["grappled"]}
    assert actions_for_state(pin) == ()
    assert actions_for_state({**pin, "engine_effects": ["restrained"]})
    narration = "The goblin is pinned to the wall. What now?"
    assert apply_narration_boundary(narration, actions_for_state(pin)) == narration
    unapplied = {"player_input": "I pin the goblin", "engine_effects": []}
    assert actions_for_state(unapplied)
    assert "pinned" not in apply_narration_boundary(
        "The goblin is pinned to the wall. What now?", actions_for_state(unapplied)
    )

    # An executed spell: its own narration survives when another action is declared.
    spell = {
        "player_input": "I cast Magic Missile at the goblin with advantage",
        "character": WIZARD,
    }
    spell_actions = actions_for_state(spell)
    assert [a.kind for a in spell_actions] == ["advantage"]
    magic = "Magic Missile sends 3 darts for 9 force damage. You gain advantage on the next turn."
    kept = apply_narration_boundary(magic, spell_actions, applied=engine_applied_for_state(spell))
    assert "Magic Missile sends 3 darts for 9 force damage." in kept
    assert "gain advantage" not in kept


def test_contract_scope_text_matches_the_engine() -> None:
    payload = repr(contract_payload())
    assert "every weapon attack is treated as proficient" not in payload
    assert "Advantage and disadvantage are not applied" not in payload
    assert "advantage_disadvantage" not in {m.id for m in UNSUPPORTED_MECHANICS}
    assert "advantage_disadvantage" in {m.id for m in SUPPORTED_MECHANICS}
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Not supported: advantage and disadvantage" not in readme


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
    actions = classify_unsupported_actions("I take the dash action")
    text = apply_narration_boundary("The goblin lunges at you. What now?", actions)
    assert text.startswith("The goblin lunges at you. What now?")
    assert "Narrative ruling: this action is not modelled" in text


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
