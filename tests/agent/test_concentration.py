"""EDG-043 (second slice): concentration and Hold Person.

Hold Person (2nd level, Wisdom save, paralysis for up to one minute, humanoid only) is held by the
caster's concentration. Casting another concentration spell, taking damage with a failed save, falling
to 0 HP, or the spell's minute ending all end it, and the foe's paralysis ends with it. Dice are
scripted where the order matters (`ScriptedDice`) and every expectation is a rules outcome.
"""

import json

import pytest

from agent.combatants import (
    can_act,
    concentration_of,
    effects_of,
    is_standing,
    narrator_roster,
    roster_violations,
)
from agent.conditions import (
    concentration_save,
    drop_concentration,
    humanoid_refusal,
    paralyzed_melee_crit,
    settle_concentration,
    start_concentration,
)
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterStats, parse_monster_stats, stats_of
from agent.spells import CATALOG, cast_request
from agent.supported_rules import (
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from tests.agent.test_spells import CLERIC, ScriptedDice, _combat_state, _foe, _hero
from tools.dice import use_rng

CLERIC_3 = {**CLERIC, "level": 3}  # second-level slots; spell save DC 13 (WIS 16)
HOLD = next(s for s in CATALOG if s.name == "Hold Person")

GOBLIN_TEXT = """Goblin
Small humanoid (goblinoid), neutral evil
Armor Class 15 (leather armor, shield)
Hit Points 7 (2d6)
Speed 30 ft.
STR DEX CON INT WIS CHA
8 (-1) 14 (+2) 10 (+0) 10 (+0) 8 (-1) 8 (-1)
Senses darkvision 60 ft., passive Perception 9
Languages Common, Goblin
Challenge 1/4 (50 XP)
Actions
Scimitar. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.
"""
BEAST_TEXT = GOBLIN_TEXT.replace(
    "Small humanoid (goblinoid), neutral evil", "Small beast, unaligned"
)
UNTYPED_TEXT = GOBLIN_TEXT.replace("Small humanoid (goblinoid), neutral evil\n", "")


WAIT = ParsedInput(intent="other")


def _exact_foe(text: str = GOBLIN_TEXT, name: str = "Goblin", hp: int = 7) -> dict:
    stats = parse_monster_stats(text, name=name)
    return {**_foe(name, hp=hp, ac=stats.ac), "monster_stats": stats.to_dict()}


def _state(message: str, foes: list[dict], parsed: ParsedInput) -> dict:
    state = _combat_state(CLERIC_3, message, parsed=parsed)
    state["combat_state"]["combatants"] = [state["combat_state"]["combatants"][0], *foes]
    return state


def _cast(text: str = "I cast Hold Person at the goblin") -> ParsedInput:
    return ParsedInput(intent="combat", check="spell_attack", target="goblin")


def _foe_in(out: dict, name: str = "Goblin") -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _summary(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


def _next_round(out: dict, message: str, parsed: ParsedInput, *, round_num: int) -> dict:
    """The saved combat row round-tripped through JSON, as the next turn reads it."""
    saved = json.loads(json.dumps(out["combat_state"]["combatants"]))
    follow = _state(message, [], parsed)
    follow["combat_state"]["combatants"] = saved
    follow["combat_state"]["round"] = round_num
    return follow


# --- the catalog, the record, and the humanoid gate -----------------------------------------------


def test_hold_person_is_a_concentration_wisdom_spell_for_humanoids() -> None:
    assert (HOLD.level, HOLD.kind, HOLD.save_ability, HOLD.condition) == (
        2,
        "condition",
        "WIS",
        "paralyzed",
    )
    assert (HOLD.duration_rounds, HOLD.concentration, HOLD.humanoid_only) == (10, True, True)
    _spell, plan, refusal = cast_request("I cast Hold Person at the goblin", None, CLERIC_3)
    assert refusal is None and plan is not None and plan.slot_level == 2


@pytest.mark.parametrize(
    ("text", "creature_type"),
    [
        (GOBLIN_TEXT, "humanoid"),
        (
            GOBLIN_TEXT.replace(
                "Small humanoid (goblinoid), neutral evil",
                "_Medium humanoid (any race), any alignment_",
            ),
            "humanoid",
        ),
        (BEAST_TEXT, "beast"),
        (UNTYPED_TEXT, None),
    ],
)
def test_creature_type_is_read_from_the_type_line(text: str, creature_type: str | None) -> None:
    assert parse_monster_stats(text, name="Goblin").creature_type == creature_type


def test_humanoid_refusal_names_the_type_or_the_missing_type() -> None:
    assert humanoid_refusal("Hold Person", _exact_foe()) is None
    beast = humanoid_refusal("Hold Person", _exact_foe(BEAST_TEXT))
    assert (
        beast
        == "Hold Person is not cast: Goblin is a beast, not a humanoid, so no spell effect is applied."
    )
    untyped = humanoid_refusal("Hold Person", _exact_foe(UNTYPED_TEXT))
    assert untyped is not None and "cannot be read from its record" in untyped
    assert humanoid_refusal("Hold Person", _foe("Goblin")) is not None  # a legacy foe has no record


# --- the combat round ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_person_paralyzes_a_humanoid_until_its_repeat_save(
    fake_llm_schema, fake_retrieval
) -> None:
    """The WIS save fails (5 - 1 vs DC 13) and paralyzes the goblin. It has its turn but cannot act,
    and its repeat save at the end of that turn keeps the effect (13 - 1 = 12). Round 2: the cleric's
    attack has advantage on the paralyzed goblin, the goblin's repeat save (14 - 1 = 13) ends it, and
    the concentration ends with it.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I cast Hold Person at the goblin", [_exact_foe()], _cast())
    with use_rng(ScriptedDice([5, 13])):
        held = await combat_round_node(state)
    goblin = _foe_in(held)
    assert effects_of(goblin) == [
        {
            "kind": "paralyzed",
            "source": "Hold Person",
            "applied_round": 1,
            "expires_after_round": 10,
            "save": {"ability": "WIS", "dc": 13},
        }
    ]
    assert concentration_of(held["combat_state"]["combatants"][0]) == {
        "spell": "Hold Person",
        "round_applied": 1,
        "expires_after_round": 10,
    }
    assert "Goblin is paralyzed: it cannot act" in _summary(held)
    assert (
        "Goblin repeats its WIS save against paralyzed (rolled 12 vs DC 13) and keeps the effect."
        in _summary(held)
    )
    assert _entries(held, "Goblin attack") == []  # a paralyzed foe makes no attack
    assert held["adjudication_result"].character_update.spell_slots_expended == {"2": 1}

    follow = _next_round(
        held,
        "I attack the goblin",
        ParsedInput(intent="combat", check="attack", target="goblin"),
        round_num=2,
    )
    with use_rng(ScriptedDice([2, 3, 14])):
        ended = await combat_round_node(follow)
    assert _entries(ended, "attack check")[0]["roll_mode"] == "advantage"
    assert _foe_in(ended)["effects"] == []
    assert (
        "Goblin repeats its WIS save against paralyzed (rolled 13 vs DC 13) and ends the effect."
        in _summary(ended)
    )
    assert "Dara's concentration on Hold Person ends." in _summary(ended)
    assert "concentration" not in ended["combat_state"]["combatants"][0]


@pytest.mark.asyncio
async def test_a_melee_hit_on_the_paralyzed_goblin_is_a_critical(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 2: the cleric's attack has advantage on the paralyzed goblin (15 kept, 15 + 2 = 17 vs AC
    15): a hit. A critical hit doubles the mace's dice (2d6, not 1d6). The goblin (30 HP) survives and
    its repeat save fails (5 - 1 = 4), so it stays paralyzed.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    held = await _held_goblin(fake_llm_schema, hp=30)
    follow = _next_round(
        held,
        "I attack the goblin",
        ParsedInput(intent="combat", check="attack", target="goblin"),
        round_num=2,
    )
    with use_rng(ScriptedDice([15, 3, 6, 5, 5])):
        out = await combat_round_node(follow)
    assert _entries(out, "attack check")[0]["roll_mode"] == "advantage"
    damage = _entries(out, "player damage")[0]
    assert damage["expression"] == "2d6" and damage["total"] == 11
    assert "Critical hit!" in _summary(out)
    assert [e["kind"] for e in effects_of(_foe_in(out))] == ["paralyzed"]


@pytest.mark.asyncio
async def test_hold_person_is_refused_for_a_beast_without_a_slot_or_a_roll(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I cast Hold Person at the goblin", [_exact_foe(BEAST_TEXT)], _cast())
    with use_rng(ScriptedDice([])):  # no roll at all
        out = await combat_round_node(state)
    assert "is a beast, not a humanoid" in _summary(out)
    assert out["adjudication_result"].character_update is None  # no slot spent
    assert effects_of(_foe_in(out)) == []
    assert concentration_of(out["combat_state"]["combatants"][0]) is None


@pytest.mark.asyncio
async def test_casting_hold_person_again_ends_the_first_and_its_paralysis(
    fake_llm_schema, fake_retrieval
) -> None:
    """A second concentration spell ends the first: the first effect ends at the cast, and the new
    save decides the new one. Its failure (2 - 1 vs DC 13) paralyzes the goblin again from round 2.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    held = await _held_goblin(fake_llm_schema)
    follow = _next_round(held, "I cast Hold Person at the goblin", _cast(), round_num=2)
    with use_rng(ScriptedDice([2, 13])):
        out = await combat_round_node(follow)
    assert "Dara's concentration on Hold Person ends." in _summary(out)
    assert effects_of(_foe_in(out)) == [
        {
            "kind": "paralyzed",
            "source": "Hold Person",
            "applied_round": 2,
            "expires_after_round": 11,
            "save": {"ability": "WIS", "dc": 13},
        }
    ]
    assert concentration_of(out["combat_state"]["combatants"][0])["round_applied"] == 2


async def _held_goblin(fake_llm_schema, hp: int = 7) -> dict:
    state = _state("I cast Hold Person at the goblin", [_exact_foe(hp=hp)], _cast())
    with use_rng(ScriptedDice([5, 13])):
        return await combat_round_node(state)


# --- concentration rules, with no round ------------------------------------------------------------


def test_damage_breaks_concentration_only_when_the_save_fails() -> None:
    """DC is 10 or half the damage, rounded down. The cleric's CON modifier is +2 (no proficiency)."""
    hero = {
        "name": "player",
        "concentration": {"spell": "Hold Person", "round_applied": 1, "expires_after_round": 10},
    }
    foe = {
        "name": "Goblin",
        "effects": [{"kind": "paralyzed", "source": "Hold Person", "applied_round": 1}],
    }
    with use_rng(ScriptedDice([8])):  # 8 + 2 = 10 vs DC 10 (damage 4): holds
        kept = concentration_save(hero, [foe], CLERIC_3, 4)
    assert kept is not None and kept.kept and kept.dc == 10
    assert concentration_of(hero) is not None and effects_of(foe)

    with use_rng(ScriptedDice([7])):  # 7 + 2 = 9 vs DC 15 (damage 30 halved): breaks
        broken = concentration_save(hero, [foe], CLERIC_3, 30)
    assert broken is not None and not broken.kept and broken.dc == 15
    assert concentration_of(hero) is None and effects_of(foe) == []


def test_damage_with_no_concentration_rolls_nothing() -> None:
    with use_rng(ScriptedDice([])):
        assert concentration_save({"name": "player"}, [], CLERIC_3, 9) is None
        assert (
            concentration_save(
                {"name": "player", "concentration": {"spell": "Hold Person"}}, [], CLERIC_3, 0
            )
            is None
        )


def test_the_saves_dc_is_at_least_ten_for_small_damage() -> None:
    hero = {"name": "player", "concentration": {"spell": "Hold Person"}}
    with use_rng(ScriptedDice([9])):  # 9 + 2 = 11 vs DC 10 (damage 1 halves to 0, so 10)
        assert concentration_save(hero, [], CLERIC_3, 1).dc == 10


def test_a_held_foe_that_no_longer_carries_the_effect_ends_the_concentration() -> None:
    hero = {"name": "player"}
    foe = {"name": "Goblin", "effects": []}
    start_concentration(hero, "Hold Person", round_applied=1, duration_rounds=10)
    assert settle_concentration(hero, [foe], round_num=2) == "Hold Person"
    assert concentration_of(hero) is None


def test_a_minute_passing_ends_the_concentration_and_the_effect() -> None:
    hero = {"name": "player"}
    foe = {"name": "Goblin", "effects": []}
    start_concentration(hero, "Hold Person", round_applied=1, duration_rounds=10)
    foe["effects"] = [
        {
            "kind": "paralyzed",
            "source": "Hold Person",
            "applied_round": 1,
            "expires_after_round": 10,
        }
    ]
    assert settle_concentration(hero, [foe], round_num=9) is None
    assert settle_concentration(hero, [foe], round_num=10) == "Hold Person"


def test_dropping_concentration_releases_only_that_spells_effects() -> None:
    hero = {"name": "player"}
    held = {"name": "Goblin", "effects": [{"kind": "paralyzed", "source": "Hold Person"}]}
    blind = {"name": "Bandit", "effects": [{"kind": "blinded", "source": "Blindness/Deafness"}]}
    start_concentration(hero, "Hold Person", round_applied=1, duration_rounds=10)
    assert drop_concentration(hero, [held, blind]) == "Hold Person"
    assert effects_of(held) == [] and [e["kind"] for e in effects_of(blind)] == ["blinded"]
    assert drop_concentration(hero, [held, blind]) is None


def test_legacy_and_reloaded_rows_read_honestly() -> None:
    assert concentration_of({"name": "player"}) is None
    assert concentration_of({"name": "player", "concentration": "Hold Person"}) is None
    legacy = {"name": "Goblin", "monster_stats": {"name": "Goblin", "hp_max": 7, "ac": 15}}
    assert stats_of(legacy) is not None and stats_of(legacy).creature_type is None
    stats = parse_monster_stats(GOBLIN_TEXT, name="Goblin")
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats


# --- paralysis is not death, and the false positives --------------------------------------------


def test_a_paralyzed_foe_is_still_in_the_fight_but_cannot_act() -> None:
    foe = _exact_foe()
    foe["effects"] = [{"kind": "paralyzed", "source": "Hold Person", "applied_round": 1}]
    assert is_standing(foe) and not can_act(foe)
    roster = narrator_roster([_hero(CLERIC_3), foe], [])
    assert (
        "active, cannot act (paralyzed)" in roster and "Combatants able to act now: none." in roster
    )
    assert roster_violations("The Goblin swings its scimitar at you.", [_hero(CLERIC_3), foe])


def test_only_a_melee_hit_from_five_feet_is_critical_against_the_paralyzed() -> None:
    held = {"name": "Goblin", "effects": [{"kind": "paralyzed", "source": "Hold Person"}]}
    assert paralyzed_melee_crit(held, weapon_name="mace", ranged=False, spell=False)
    assert not paralyzed_melee_crit(held, weapon_name="mace", ranged=True, spell=False)
    assert not paralyzed_melee_crit(held, weapon_name="mace", ranged=False, spell=True)
    assert not paralyzed_melee_crit(held, weapon_name="glaive", ranged=False, spell=False)
    assert not paralyzed_melee_crit(
        {"name": "Goblin"}, weapon_name="mace", ranged=False, spell=False
    )


@pytest.mark.parametrize(
    "text",
    [
        "I concentrate on the door",
        "I try to concentrate harder on the lock",
        "I hold the goblin in place while I concentrate",
    ],
)
def test_concentration_idioms_are_not_spells_or_claims(text: str) -> None:
    assert cast_request(text, None, CLERIC_3) is None
    assert classify_unsupported_actions(text, None, CLERIC_3, applied=EngineApplied()) == ()


def test_narrated_paralysis_survives_only_when_the_engine_paralyzed_the_foe() -> None:
    text = "I cast Hold Person at the goblin"
    narration = "The goblin is paralyzed and cannot move. What do you do next?"
    unrecorded = classify_unsupported_actions(text, None, CLERIC_3)
    assert [a.kind for a in unrecorded] == ["condition"]
    cleaned = apply_narration_boundary(narration, unrecorded)
    assert "The goblin is paralyzed" not in cleaned and "What do you do next?" in cleaned
    assert "recorded no paralyzed condition" in cleaned

    recorded = classify_unsupported_actions(
        text, None, CLERIC_3, applied=EngineApplied(effects=("paralyzed",))
    )
    assert recorded == ()
    assert apply_narration_boundary(narration, recorded) == narration


def test_roster_names_the_hero_concentration_for_the_narrator() -> None:
    hero = {
        **_hero(CLERIC_3),
        "concentration": {"spell": "Hold Person", "round_applied": 1, "expires_after_round": 10},
    }
    roster = narrator_roster([hero, _exact_foe()], [])
    assert "Dara is concentrating on Hold Person." in roster
    assert "concentrating" not in narrator_roster([_hero(CLERIC_3), _exact_foe()], [])


def test_public_combat_payload_names_the_concentration_additively() -> None:
    from apps.api.services.turn_runner import combat_state_public

    held = {"name": "player", "is_player": True, "concentration": {"spell": "Hold Person"}}
    public = combat_state_public({"round": 2, "combatants": [held, _exact_foe()]})
    assert public is not None and public["concentration"] == "Hold Person"
    assert (
        combat_state_public({"round": 2, "combatants": [_hero(CLERIC_3)]})["concentration"] is None
    )


@pytest.mark.asyncio
async def test_falling_to_zero_hp_ends_concentration_without_a_save(
    fake_llm_schema, fake_retrieval
) -> None:
    """The cleric (3 HP) concentrates on Hold Person over a paralyzed bandit. A goblin's hit (17 vs AC
    14) for 6 drops it to 0: the concentration ends on the fall, with no Constitution save rolled (the
    script has no die left for one), and the bandit is no longer paralyzed.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    bandit = _foe("Bandit", hp=20)
    bandit["effects"] = [
        {
            "kind": "paralyzed",
            "source": "Hold Person",
            "applied_round": 1,
            "expires_after_round": 10,
        }
    ]
    hero = {
        **_hero(CLERIC_3),
        "hp_current": 3,
        "concentration": {"spell": "Hold Person", "round_applied": 1, "expires_after_round": 10},
    }
    state = _combat_state(CLERIC_3, "I wait and watch", parsed=WAIT)
    state["combat_state"]["combatants"] = [hero, bandit, _exact_foe()]
    state["character"] = {**state["character"], "hp_current": 3}
    with use_rng(ScriptedDice([17, 6])):
        out = await combat_round_node(state)
    assert _entries(out, "Dara Constitution save (concentration)") == []  # no save at 0 HP
    assert "Dara falls and loses concentration on Hold Person." in _summary(out)
    assert concentration_of(out["combat_state"]["combatants"][0]) is None
    assert effects_of(_foe_in(out, "Bandit")) == []


@pytest.mark.asyncio
async def test_a_spell_paralyzes_only_the_named_one_of_two_similar_foes(
    fake_llm_schema, fake_retrieval
) -> None:
    """Two goblins, 'Goblin' and 'Goblin 2'. The cast names 'Goblin 2' and only that foe is held."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        "I cast Hold Person at Goblin 2",
        [_exact_foe(), _exact_foe(name="Goblin 2")],
        ParsedInput(intent="combat", check="spell_attack", target="Goblin 2"),
    )
    # The cast save fails (5 - 1); the unheld Goblin misses (2 + 4); the held Goblin 2 keeps it (13 - 1).
    with use_rng(ScriptedDice([5, 2, 13])):
        out = await combat_round_node(state)
    assert effects_of(_foe_in(out, "Goblin")) == []
    assert [e["kind"] for e in effects_of(_foe_in(out, "Goblin 2"))] == ["paralyzed"]
    assert "Goblin 2 is paralyzed" in _summary(out)


@pytest.mark.asyncio
async def test_a_hit_that_breaks_concentration_releases_the_held_foe(
    fake_llm_schema, fake_retrieval
) -> None:
    """A goblin hits the concentrating cleric (17 vs AC 14) for 8 (6 + 2). The CON save is DC 10 (half
    the damage is 4, so 10 is higher) and rolls 3 + 2 = 5: concentration breaks, and the bandit held by
    Hold Person is released.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    bandit = _foe("Bandit", hp=20)
    bandit["effects"] = [
        {
            "kind": "paralyzed",
            "source": "Hold Person",
            "applied_round": 1,
            "expires_after_round": 10,
        }
    ]
    hero = {
        **_hero(CLERIC_3),
        "concentration": {"spell": "Hold Person", "round_applied": 1, "expires_after_round": 10},
    }
    state = _combat_state(CLERIC_3, "I wait and watch", parsed=WAIT)
    state["combat_state"]["combatants"] = [hero, bandit, _exact_foe()]
    with use_rng(ScriptedDice([17, 6, 3])):
        out = await combat_round_node(state)
    save = _entries(out, "Dara Constitution save (concentration)")[0]
    assert save["rolls"] == [3] and save["total"] == 5
    assert "Dara loses concentration on Hold Person (rolled 5 vs DC 10)." in _summary(out)
    assert concentration_of(out["combat_state"]["combatants"][0]) is None
    assert effects_of(_foe_in(out, "Bandit")) == []
