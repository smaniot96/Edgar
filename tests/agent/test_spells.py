"""EDG-027: declared catalog spells resolve through typed engine operations, slots persist.

Dice are scripted (`ScriptedDice` returns the faces in order and fails on an unexpected roll), so
every hit, miss, crit, save, heal and refusal is asserted against exact engine numbers.
"""

import random

import pytest

from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.models.parsed_input import ParsedInput
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.resolution import enforce_side_effects, roll_attack_damage
from agent.spells import (
    CATALOG,
    cast_request,
    expended_slots,
    find_catalog_spell,
    plan_cast,
    requested_slot_level,
    resolve_spell,
    slot_totals,
    spell_save_dc,
)
from agent.supported_rules import (
    apply_narration_boundary,
    classify_unsupported_actions,
    executed_spell_for_state,
)
from tools.dice import use_rng

FIRE_BOLT = next(s for s in CATALOG if s.name == "Fire Bolt")
MAGIC_MISSILE = next(s for s in CATALOG if s.name == "Magic Missile")
SACRED_FLAME = next(s for s in CATALOG if s.name == "Sacred Flame")
CURE_WOUNDS = next(s for s in CATALOG if s.name == "Cure Wounds")


class ScriptedDice(random.Random):
    """Returns the scripted die faces in order; an unexpected roll fails the test."""

    def __init__(self, faces: list[int]) -> None:
        super().__init__(0)
        self.faces = list(faces)

    def randint(self, a: int, b: int) -> int:
        assert self.faces, f"unexpected roll d{b - a + 1}: the script is exhausted"
        face = self.faces.pop(0)
        assert a <= face <= b, (a, b, face)
        return face


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
CLERIC = {
    "id": 3,
    "name": "Dara",
    "class": "Cleric",
    "level": 1,
    "hp_current": 12,
    "hp_max": 12,
    "stats": {"STR": 10, "DEX": 12, "CON": 14, "INT": 10, "WIS": 16, "CHA": 10},
    "inventory": {"weapons": ["mace"], "armor": "scale mail"},
}
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


def _with_slots(character: dict, expended: dict[str, int]) -> dict:
    return {**character, "stats": {**character["stats"], "spell_slots_expended": expended}}


def _foe(name: str = "Goblin", hp: int = 20, ac: int = 12) -> dict:
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp,
        "ac": ac,
        "attack_bonus": 3,
        "damage_dice": "1d6+1",
        "initiative_bonus": 1,
        "init": 5,
        "init_bonus": 1,
        "is_boss": False,
        "is_player": False,
        "alive": True,
    }


def _hero(character: dict, ac: int = 14) -> dict:
    return {
        "name": "player",
        "display_name": character["name"],
        "is_player": True,
        "hp_current": character["hp_current"],
        "hp_max": character["hp_max"],
        "ac": ac,
        "alive": True,
        "init": 15,
        "init_bonus": 2,
        "second_wind_used": False,
    }


def _combat_state(character: dict, message: str, *, goblin_hp: int = 20, parsed=None) -> dict:
    """A live encounter: the hero acts first (init 15), the goblin after (init 5)."""
    return {
        "player_input": message,
        "session_id": 1,
        "campaign_id": 1,
        "messages": [],
        "character": dict(character),
        "world_flags": {},
        "npcs": [],
        "adventure_collections": [],
        "parsed_input": parsed,
        "combat_state": {
            "id": 7,
            "round": 1,
            "ended": False,
            "combatants": [_hero(character), _foe(hp=goblin_hp)],
        },
    }


def _foe_after(out: dict, name: str = "Goblin") -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _purposes(adj: AdjudicationResult) -> list[str]:
    return [d.get("purpose", "") for d in adj.dice_log]


def _cast(text: str, check: str = "spell_attack") -> ParsedInput:
    return ParsedInput(intent="combat", check=check, target="goblin")


# --- The catalog and the slot ledger -------------------------------------------------------


def test_catalog_covers_each_kind_with_provenance() -> None:
    assert {s.name for s in CATALOG} == {
        "Fire Bolt",
        "Magic Missile",
        "Sacred Flame",
        "Cure Wounds",
    }
    assert {s.kind for s in CATALOG} == {"attack", "auto", "save", "heal"}
    assert all(s.source.startswith("Player's Handbook (2014") for s in CATALOG)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I cast Fire Bolt at the goblin", "Fire Bolt"),
        ("I hurl a magic missile at the goblin", "Magic Missile"),
        ("I cast cure wounds on myself", "Cure Wounds"),
        ("I cast Fireball at the goblin", None),
        ("What does Magic Missile do?", None),
        ("I cast my net over the crowd of fishers", None),
        ("I attack the goblin", None),
    ],
)
def test_declared_catalog_spell_is_found_only_with_a_cast(text, expected) -> None:
    spell = find_catalog_spell(text)
    assert (spell.name if spell else None) == expected


def test_two_catalog_spells_in_one_declaration_are_ambiguous_and_neither_runs() -> None:
    assert find_catalog_spell("I cast Fire Bolt and then Magic Missile at the goblin") is None
    assert find_catalog_spell("I cast Fire Bolt at the goblin and then the goblin") is not None


def test_parsed_spell_entity_names_the_spell_when_the_text_names_it() -> None:
    parsed = ParsedInput(intent="combat", entities={"spell": "Magic Missile"})
    assert find_catalog_spell("I unleash it at the goblin", parsed) is None
    assert find_catalog_spell("I cast magic missile", parsed).name == "Magic Missile"


def test_requested_slot_level_is_read_from_the_declaration() -> None:
    assert requested_slot_level("I cast Magic Missile with a 2nd-level slot") == 2
    assert requested_slot_level("I cast Magic Missile using level 3 spell slot") == 3
    assert requested_slot_level("I cast Magic Missile") is None


@pytest.mark.parametrize(
    ("character", "expected"),
    [
        (WIZARD, {1: 2}),
        ({**WIZARD, "level": 3}, {1: 4, 2: 2}),
        ({**WIZARD, "level": 5}, {1: 4, 2: 3, 3: 2}),
        ({**WIZARD, "level": 20}, {1: 4, 2: 3, 3: 3, 4: 3, 5: 3, 6: 2, 7: 2, 8: 1, 9: 1}),
        ({"class": "Paladin", "level": 1}, {}),
        ({"class": "Paladin", "level": 2}, {1: 2}),
        ({"class": "Ranger", "level": 5}, {1: 4, 2: 2}),
        ({"class": "Fighter", "level": 5}, {}),
    ],
)
def test_slot_totals_derive_from_class_and_level(character, expected) -> None:
    assert slot_totals(character) == expected


def test_cantrip_costs_no_slot_and_leveled_spell_spends_the_lowest_slot() -> None:
    plan, refusal = plan_cast(WIZARD, FIRE_BOLT)
    assert refusal is None and plan.slot_level is None and plan.expended is None

    plan, refusal = plan_cast({**WIZARD, "level": 3}, MAGIC_MISSILE)
    assert refusal is None and plan.slot_level == 1 and plan.expended == {"1": 1}

    # Level-1 slots are spent: the lowest free slot at or above 1st is 2nd level.
    spent = _with_slots({**WIZARD, "level": 3}, {"1": 4})
    plan, refusal = plan_cast(spent, MAGIC_MISSILE)
    assert refusal is None and plan.slot_level == 2 and plan.expended == {"1": 4, "2": 1}


def test_refuses_when_no_slot_remains_and_spends_nothing() -> None:
    empty = _with_slots(WIZARD, {"1": 2})
    plan, refusal = plan_cast(empty, MAGIC_MISSILE)
    assert plan is None
    assert "no spell slot of level 1" in refusal and "no spell effect is applied" in refusal
    # A named slot that is spent is not silently replaced by another.
    plan, refusal = plan_cast({**WIZARD, "level": 3}, MAGIC_MISSILE, requested_slot=2)
    assert plan.slot_level == 2
    plan, refusal = plan_cast(_with_slots({**WIZARD, "level": 3}, {"2": 2}), MAGIC_MISSILE, 2)
    assert plan is None and "level 2" in refusal


def test_a_character_without_the_spell_on_its_list_is_refused_without_a_roll() -> None:
    plan, refusal = plan_cast(FIGHTER, MAGIC_MISSILE)
    assert plan is None
    assert "has no access to it" in refusal
    assert cast_request("I cast Magic Missile at the goblin", None, FIGHTER)[2] == refusal


def test_expended_ledger_ignores_malformed_entries() -> None:
    dirty = _with_slots(WIZARD, {"1": "1", "2": -3, "x": 4, "9": 2, "3": "junk"})
    assert expended_slots(dirty) == {"1": 1, "9": 2}


def test_spell_save_dc_is_eight_plus_proficiency_plus_casting_ability() -> None:
    assert spell_save_dc(WIZARD) == 13  # 8 + proficiency 2 + INT 3
    assert spell_save_dc(CLERIC) == 13  # 8 + proficiency 2 + WIS 3
    assert spell_save_dc(FIGHTER) is None


@pytest.mark.parametrize(("level", "count"), [(1, 1), (5, 2), (11, 3), (17, 4)])
def test_cantrip_dice_scale_with_character_level(level: int, count: int) -> None:
    character = {**WIZARD, "level": level}
    plan, _ = plan_cast(character, FIRE_BOLT)
    # Target AC 1: any roll above 1 hits, so the damage dice are always rolled.
    faces = [10] + [1] * count
    result = resolve_spell(character, plan, target_ac=1, rng=ScriptedDice(faces))
    assert result.attack.success
    assert result.dice_log[-1]["expression"] == f"{count}d10"
    assert result.damage == count  # every die rolled 1


def test_upcast_magic_missile_adds_a_dart_per_slot_level() -> None:
    plan, _ = plan_cast({**WIZARD, "level": 5}, MAGIC_MISSILE, requested_slot=3)
    result = resolve_spell(WIZARD, plan, rng=ScriptedDice([1, 1, 1, 1, 1]))
    assert result.darts == 5
    assert result.dice_log[-1]["expression"] == "5d4+5"
    assert result.damage == 10


# --- Combat: the round resolves the declared spell through the same helper -----------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("faces", "hit", "damage_dice", "goblin_hp", "expression"),
    [
        ([15, 7, 2], True, "1d10", 13, None),  # 15 + 5 = 20 hits AC 12; 1d10 = 7
        ([5, 2], False, None, 20, None),  # 5 + 5 = 10 misses AC 12
        ([20, 6, 4, 2], True, "2d10", 10, "2d10"),  # natural 20 doubles the dice
    ],
)
async def test_catalog_spell_attack_hit_miss_and_crit(
    fake_llm_schema, fake_retrieval, faces, hit, damage_dice, goblin_hp, expression
) -> None:
    """Fire Bolt is a spell attack: a real attack roll, engine damage on a hit, no class die."""
    state = _combat_state(WIZARD, "I cast Fire Bolt at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice(faces)):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert _foe_after(out)["hp_current"] == goblin_hp
    assert adj.roll_expression == "1d20+5" and adj.against == "AC" and adj.dc == 12
    assert adj.success is hit
    if hit:
        assert "Fire Bolt hits Goblin" in adj.mechanical_summary
        damage = next(d for d in adj.dice_log if d.get("purpose") == "Fire Bolt damage")
        assert damage["expression"] == (expression or damage_dice)
    else:
        assert "Fire Bolt misses Goblin" in adj.mechanical_summary
        assert not any(p == "Fire Bolt damage" for p in _purposes(adj))
    # A cantrip costs no slot; the enemy's own roll (its attack) is the only other HP-affecting roll.
    assert adj.character_update.spell_slots_expended is None
    assert adj.character_update.hp_delta == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "mode"), [("restrained", "advantage"), ("prone", "disadvantage")]
)
async def test_fire_bolt_uses_the_engine_roll_mode_from_foe_effects(
    fake_llm_schema, fake_retrieval, effect, mode
) -> None:
    """Fire Bolt is a ranged spell attack: a restrained foe grants advantage, a prone one disadvantage."""
    state = _combat_state(WIZARD, "I cast Fire Bolt at the goblin", parsed=_cast("I cast"))
    foe = next(c for c in state["combat_state"]["combatants"] if not c["is_player"])
    foe["effects"] = [{"kind": effect, "source": "hands", "applied_round": 1}]
    with use_rng(ScriptedDice([3, 18, 7, 2, 2])):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    attack = next(d for d in adj.dice_log if d.get("purpose") == "spell_attack check")
    assert attack.get("roll_mode") == mode and attack["rolls"] == [3, 18]
    assert out["engine_roll_mode"] == mode


@pytest.mark.asyncio
async def test_magic_missile_auto_hits_and_spends_a_first_level_slot(
    fake_llm_schema, fake_retrieval
) -> None:
    state = _combat_state(
        WIZARD, "I cast Magic Missile at the goblin", goblin_hp=30, parsed=_cast("I cast")
    )
    with use_rng(ScriptedDice([2, 3, 4, 2])):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    darts = next(d for d in adj.dice_log if d.get("purpose") == "Magic Missile darts")
    assert darts["expression"] == "3d4+3" and darts["total"] == 12
    assert _foe_after(out)["hp_current"] == 30 - 12
    assert adj.damage == 12
    assert "spell attack" not in adj.mechanical_summary  # no attack roll
    assert adj.character_update.spell_slots_expended == {"1": 1}


@pytest.mark.asyncio
async def test_magic_missile_upcast_by_named_slot_spends_that_slot(
    fake_llm_schema, fake_retrieval
) -> None:
    character = {**WIZARD, "level": 3}
    state = _combat_state(
        character,
        "I cast Magic Missile at the goblin with a 2nd-level slot",
        goblin_hp=30,
        parsed=_cast("I cast"),
    )
    with use_rng(ScriptedDice([1, 1, 1, 1, 2])):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    darts = next(d for d in adj.dice_log if d.get("purpose") == "Magic Missile darts")
    assert darts["expression"] == "4d4+4" and darts["total"] == 8
    assert _foe_after(out)["hp_current"] == 22
    assert adj.character_update.spell_slots_expended == {"2": 1}


@pytest.mark.asyncio
async def test_sacred_flame_save_failure_deals_damage_and_success_does_not(
    fake_llm_schema, fake_retrieval
) -> None:
    """The goblin's DEX save is d20 + its initiative bonus against the caster's DC 13."""
    failed = _combat_state(CLERIC, "I cast Sacred Flame at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([5, 6, 2])):  # 5 + 1 = 6 fails DC 13; 1d8 = 6
        out = await combat_round_node(failed)
    adj = out["adjudication_result"]
    assert _foe_after(out)["hp_current"] == 14
    assert "fails the DEX save against Sacred Flame (DC 13)" in adj.mechanical_summary
    save = next(d for d in adj.dice_log if d.get("purpose") == "DEX save")
    assert save["expression"] == "1d20+1" and save["total"] == 6

    passed = _combat_state(CLERIC, "I cast Sacred Flame at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([15, 2])):  # 15 + 1 = 16 succeeds: no damage die is rolled
        out = await combat_round_node(passed)
    adj = out["adjudication_result"]
    assert _foe_after(out)["hp_current"] == 20
    assert "succeeds on the DEX save" in adj.mechanical_summary
    assert not any(d.get("purpose") == "Sacred Flame damage" for d in adj.dice_log)


@pytest.mark.asyncio
async def test_cure_wounds_heals_the_caster_by_one_die_plus_wisdom(
    fake_llm_schema, fake_retrieval
) -> None:
    hurt = {**CLERIC, "hp_current": 2}
    state = _combat_state(hurt, "I cast Cure Wounds on myself", parsed=_cast("I cast", check=None))
    with use_rng(ScriptedDice([6, 2])):  # 1d8 = 6, plus WIS +3 = 9; goblin misses AC 14
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert adj.character_update.hp_delta == 9
    assert adj.character_update.spell_slots_expended == {"1": 1}
    assert "Cure Wounds restores 9 HP to Dara" in adj.mechanical_summary
    assert not any(d.get("purpose") == "Goblin damage" for d in adj.dice_log)


@pytest.mark.asyncio
async def test_no_slot_left_refuses_the_leveled_spell_without_a_round(
    fake_llm_schema, fake_retrieval
) -> None:
    """A refusal rolls nothing, advances no round and changes no HP."""
    empty = _with_slots(WIZARD, {"1": 2})
    state = _combat_state(empty, "I cast Magic Missile at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([])):  # any roll fails the test
        out = await combat_round_node(state)
    assert out["adjudication_result"].dice_log == []
    assert "no spell slot of level 1" in out["narration"]
    assert out["combat_state"]["round"] == 1
    assert _foe_after(out)["hp_current"] == 20
    assert not any(schema is AdjudicationResult for schema, _ in fake_llm_schema.calls)


@pytest.mark.asyncio
async def test_cantrip_still_casts_when_every_slot_is_spent(
    fake_llm_schema, fake_retrieval
) -> None:
    empty = _with_slots(WIZARD, {"1": 2})
    state = _combat_state(empty, "I cast Fire Bolt at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([15, 7, 2])):
        out = await combat_round_node(state)
    assert _foe_after(out)["hp_current"] == 13


@pytest.mark.asyncio
async def test_fighter_declaring_a_catalog_spell_is_refused(
    fake_llm_schema, fake_retrieval
) -> None:
    state = _combat_state(FIGHTER, "I cast Magic Missile at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([])):
        out = await combat_round_node(state)
    assert "has no access to it" in out["narration"]
    assert _foe_after(out)["hp_current"] == 20 and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_unlisted_spell_keeps_the_narrative_ruling_and_no_effect(
    fake_llm_schema, fake_retrieval
) -> None:
    state = _combat_state(WIZARD, "I cast Fireball at the goblin", parsed=_cast("I cast"))
    with use_rng(ScriptedDice([2])):  # only the goblin's attack roll; it misses
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert "Narrative ruling" in adj.mechanical_summary
    assert "no engine mechanics" in adj.mechanical_summary
    assert _purposes(adj) == ["Goblin attack"]
    assert adj.character_update.spell_slots_expended is None
    assert _foe_after(out)["hp_current"] == 20


# --- Out of combat: the same helper, no foe tracked --------------------------------------


def _rules_state(character: dict, message: str, parsed: ParsedInput) -> dict:
    return {
        "player_input": message,
        "parsed_input": parsed,
        "character": character,
        "world_flags": {},
        "current_scene_id": "cliff_base",
        "rules_context": [],
    }


@pytest.mark.asyncio
async def test_out_of_combat_magic_missile_rolls_and_ignores_model_damage(fake_llm_schema) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        mechanical_summary="A bolt of force slams a crate to splinters.",
        character_update=CharacterUpdate(hp_delta=-7, damage_dice="3d4"),
    )
    state = _rules_state(
        WIZARD, "I cast Magic Missile at the crate", ParsedInput(intent="exploration")
    )
    with use_rng(ScriptedDice([2, 3, 4])):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.character_update.spell_slots_expended == {"1": 1}
    assert adj.character_update.hp_delta is None and adj.character_update.damage_dice is None
    assert adj.damage is None
    assert "Magic Missile sends 3 darts for 12 force damage." in adj.mechanical_summary
    assert "No foe is tracked outside combat" in adj.mechanical_summary
    assert "splinters" in adj.mechanical_summary  # the model's prose is kept, its numbers are not
    assert any(d.get("purpose") == "Magic Missile darts" for d in adj.dice_log)


@pytest.mark.asyncio
async def test_out_of_combat_cure_wounds_heals_through_the_character_update(
    fake_llm_schema,
) -> None:
    state = _rules_state(
        {**CLERIC, "hp_current": 2},
        "I cast Cure Wounds on myself",
        ParsedInput(intent="exploration"),
    )
    with use_rng(ScriptedDice([6])):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.character_update.hp_delta == 9
    assert adj.character_update.spell_slots_expended == {"1": 1}


@pytest.mark.asyncio
async def test_out_of_combat_refusal_has_no_effect(fake_llm_schema) -> None:
    state = _rules_state(
        FIGHTER, "I cast Magic Missile at the crate", ParsedInput(intent="exploration")
    )
    with use_rng(ScriptedDice([])):
        out = await rules_adjudicator_node(state)
    adj = out["adjudication_result"]
    assert adj.success is False and "has no access to it" in adj.mechanical_summary
    assert adj.character_update is None and adj.dice_log == []
    assert not any(schema is AdjudicationResult for schema, _ in fake_llm_schema.calls)


def test_long_rest_restores_slots_and_no_model_proposal_sets_them() -> None:
    adj = AdjudicationResult(
        success=True, character_update=CharacterUpdate(spell_slots_expended={"1": 9})
    )
    enforce_side_effects(
        adj,
        character=_with_slots(WIZARD, {"1": 2}),
        check_result=None,
        player_input="We make camp and take a long rest.",
    )
    assert adj.character_update.spell_slots_expended == {}

    adj = AdjudicationResult(
        success=True, character_update=CharacterUpdate(spell_slots_expended={"1": 9})
    )
    enforce_side_effects(
        adj,
        character=_with_slots(WIZARD, {"1": 2}),
        check_result=None,
        player_input="We take a short rest.",
    )
    assert adj.character_update.spell_slots_expended is None


def test_attack_damage_helper_never_rolls_a_class_die_for_a_spell() -> None:
    from agent.resolution import CheckResult

    attack = CheckResult(
        check="spell_attack",
        kind="spell_attack",
        ability="INT",
        modifier=5,
        dc=12,
        against="AC",
        roll_expression="1d20+5",
        natural=15,
        total=20,
        success=True,
    )
    with pytest.raises(ValueError, match="agent.spells"):
        roll_attack_damage(WIZARD, attack)


# --- Classification and narration boundary ------------------------------------------------


def test_executed_catalog_spell_is_not_an_unsupported_action() -> None:
    assert classify_unsupported_actions("I cast Magic Missile at the goblin", None, WIZARD) == ()
    assert executed_spell_for_state(
        {"player_input": "I cast Magic Missile at the goblin", "character": WIZARD}
    )
    assert not executed_spell_for_state(
        {"player_input": "I cast Magic Missile at the goblin", "character": FIGHTER}
    )


def test_refused_catalog_spell_still_gets_a_ruling() -> None:
    actions = classify_unsupported_actions("I cast Magic Missile at the goblin", None, FIGHTER)
    assert [a.kind for a in actions] == ["spell"]
    assert "has no access to it" in actions[0].ruling and actions[0].ruling.startswith(
        "Narrative ruling"
    )


def test_executed_spell_narration_survives_the_boundary_with_another_declared_action() -> None:
    actions = classify_unsupported_actions(
        "I cast Magic Missile at the goblin with advantage", None, WIZARD
    )
    assert [a.kind for a in actions] == ["advantage"]
    text = apply_narration_boundary(
        "Magic Missile sends 3 darts for 9 force damage. You gain advantage on the next turn.",
        actions,
        executed_spell=True,
    )
    assert "Magic Missile sends 3 darts for 9 force damage." in text
    assert "gain advantage" not in text


@pytest.mark.parametrize(
    "text",
    [
        "I cast my net over the crowd of fishers",
        "I cast a glance at the door",
        "I take advantage of the distraction to slip past",
    ],
)
def test_idioms_are_not_declared_spells(text: str) -> None:
    assert classify_unsupported_actions(text, None, WIZARD) == ()
    assert cast_request(text, None, WIZARD) is None


@pytest.mark.asyncio
async def test_slot_ledger_write_path_replaces_and_clears_only_when_set(monkeypatch) -> None:
    from types import SimpleNamespace

    from apps.api.services import world_writes

    assignment = SimpleNamespace(
        hp_current=8,
        hp_max=8,
        stats={"conditions": [], "spell_slots_expended": {"1": 1}},
        inventory={"items": []},
    )

    async def load(_db, _character_id, _campaign_id):
        return assignment

    monkeypatch.setattr(world_writes, "load_active_assignment", load)
    await world_writes._apply_character_update(
        None, 1, 1, CharacterUpdate(spell_slots_expended={"1": 2, "2": 0})
    )
    assert assignment.stats["spell_slots_expended"] == {"1": 2}
    await world_writes._apply_character_update(None, 1, 1, CharacterUpdate())
    assert assignment.stats["spell_slots_expended"] == {"1": 2}  # an unset ledger is left alone
    await world_writes._apply_character_update(None, 1, 1, CharacterUpdate(spell_slots_expended={}))
    assert assignment.stats["spell_slots_expended"] == {}
