"""Monster spellcasting stays inside the parsed catalog and persists exact combat effects."""

import json

import pytest

from agent.combatants import add_effect, attack_roll_mode, effects_of
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import parse_monster_stats
from agent.spells import CATALOG
from tests.agent.test_spells import CLERIC, ScriptedDice, _combat_state, _foe
from tools.dice import use_rng

SPELLCASTER = """Priest
Medium humanoid (human), neutral good
Armor Class 13
Hit Points 30
STR 10 (+0) DEX 14 (+2) CON 14 (+2) INT 10 (+0) WIS 16 (+3) CHA 12 (+1)
Spellcasting. The priest is a 3rd-level spellcaster. Its spellcasting ability is Wisdom
(spell save DC 13; +5 to hit with spell attacks). The priest has the following cleric spells prepared:
Cantrips (at will): Sacred Flame, Fire Bolt
1st level (4 slots): Guiding Bolt, Healing Word
2nd level (2 slots): Blindness/Deafness, Hold Person.
"""
SINGLE_BOLT = (
    SPELLCASTER.replace("Cantrips (at will): Sacred Flame, Fire Bolt", "Cantrips (at will): Fire Bolt")
    .replace("1st level (4 slots): Guiding Bolt, Healing Word", "1st level (1 slot): Guiding Bolt")
    .replace("2nd level (2 slots): Blindness/Deafness, Hold Person.\n", "")
)
CANTRIP_AND_HEAL = (
    SPELLCASTER.replace("Cantrips (at will): Sacred Flame, Fire Bolt", "Cantrips (at will): Sacred Flame")
    .replace("1st level (4 slots): Guiding Bolt, Healing Word", "1st level (1 slot): Healing Word")
    .replace("2nd level (2 slots): Blindness/Deafness, Hold Person.\n", "")
)


WAIT = ParsedInput(intent="other")


def _priest(*, init: int = 5, hp: int = 30, spells: str = SPELLCASTER) -> dict:
    stats = parse_monster_stats(spells, name="Priest")
    foe = _foe("Priest", hp=stats.hp_max, ac=stats.ac)
    foe.update(
        hp_current=hp,
        init=init,
        band="ranged",
        monster_stats=stats.to_dict(),
    )
    return foe


def _state(message: str, parsed: ParsedInput, foe: dict) -> dict:
    state = _combat_state(CLERIC, message, parsed=parsed)
    state["combat_state"]["combatants"] = [state["combat_state"]["combatants"][0], foe]
    return state


def _actor(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _summary(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_monster_guiding_bolt_mark_is_consumed_by_its_next_spell_attack_after_reload(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    priest = _priest(init=20, spells=SINGLE_BOLT)
    state = _state("I wait", WAIT, priest)
    with use_rng(ScriptedDice([10, 1, 1, 1, 1])):
        first = await combat_round_node(state)
    assert _actor(first, "Priest")["monster_spell_slots_expended"] == {"1": 1}
    assert any(effect["kind"] == "marked" for effect in effects_of(_actor(first, "player")))

    # Round-trip the combatant state as the API does. Guiding Bolt is unavailable on the used slot,
    # so the priest's next eligible spell is its Fire Bolt cantrip.
    saved = json.loads(json.dumps(first["combat_state"]["combatants"]))
    follow = _state("I wait", WAIT, _priest(init=20, spells=SINGLE_BOLT))
    follow["combat_state"]["combatants"] = saved
    follow["combat_state"]["round"] = 2
    with use_rng(ScriptedDice([15, 2, 2])):
        second = await combat_round_node(follow)
    attack = next(d for d in second["adjudication_result"].dice_log if d.get("purpose") == "Fire Bolt spell attack")
    assert attack["roll_mode"] == "advantage"
    assert not any(effect["kind"] == "marked" for effect in effects_of(_actor(second, "player")))


@pytest.mark.asyncio
async def test_monster_hold_person_ends_hero_concentration_and_persists_repeat_save(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    priest = _priest(init=20, spells=SPELLCASTER.replace("Blindness/Deafness", "Sacred Flame"))
    state = _state("I wait", WAIT, priest)
    _actor(state, "player")["concentration"] = {"spell": "Bless", "source_id": "hero:bless"}
    with use_rng(ScriptedDice([1, 1])):  # fail Hold Person and the end-of-turn repeat save
        first = await combat_round_node(state)
    hero = _actor(first, "player")
    foe = _actor(first, "Priest")
    assert not hero.get("concentration")
    assert first["telemetry_events"][0]["payload"]["player_action"] == {
        "action": "none",
        "result": "paralyzed",
    }
    assert any(effect["kind"] == "paralyzed" for effect in effects_of(hero))
    assert foe["concentration"]["spell"] == "Hold Person"
    assert "is paralyzed and cannot act" in _summary(first)

    saved = json.loads(json.dumps(first["combat_state"]["combatants"]))
    follow = _state("I wait", WAIT, _priest(init=20))
    follow["combat_state"]["combatants"] = saved
    follow["combat_state"]["round"] = 2
    # The priest spends its remaining leveled attack spell before the hero's repeat save.
    with use_rng(ScriptedDice([1, 2, 10])):  # Guiding Bolt misses; the repeat save succeeds
        second = await combat_round_node(follow)
    assert not any(effect["kind"] == "paralyzed" for effect in effects_of(_actor(second, "player")))
    assert not _actor(second, "Priest").get("concentration")
    assert "Priest's concentration on Hold Person ends" in _summary(second)


@pytest.mark.asyncio
async def test_monster_blindness_disadvantages_hero_attack_and_bonus_spell_costs_slot(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    priest = _priest(init=20, spells=SPELLCASTER.replace("Hold Person", "Sacred Flame"))
    priest["band"] = "melee"
    state = _state("I attack the priest", ParsedInput(intent="combat", check="attack", target="Priest"), priest)
    with use_rng(ScriptedDice([1, 13, 3, 1])):  # CON fail, attack disadvantage, failed repeat save
        blinded = await combat_round_node(state)
    attack = next(d for d in blinded["adjudication_result"].dice_log if d.get("purpose") == "attack check")
    assert attack["roll_mode"] == "disadvantage"
    assert any(effect["kind"] == "blinded" for effect in effects_of(_actor(blinded, "player")))

    # A cantrip action may accompany the bonus-action Healing Word; only the leveled spell spends a slot.
    both = _priest(init=5, hp=1, spells=CANTRIP_AND_HEAL)
    state = _state("I wait", WAIT, both)
    with use_rng(ScriptedDice([1, 2, 4, 3])):  # DEX save, Sacred Flame damage, Healing Word
        cast = await combat_round_node(state)
    priest_after = _actor(cast, "Priest")
    assert priest_after["hp_current"] == 8
    assert priest_after["monster_spell_slots_expended"] == {"1": 1}
    assert "Priest casts Healing Word and regains 7 HP" in _summary(cast)


def test_incapped_or_blinded_target_does_not_impose_close_ranged_disadvantage() -> None:
    for kind in ("paralyzed", "stunned", "blinded"):
        target = {"effects": [{"kind": kind}]}
        assert attack_roll_mode({}, target, ranged=True, within_five_feet=True) == "advantage"


@pytest.mark.asyncio
@pytest.mark.parametrize("band", ["ranged", "melee"])
async def test_source_ray_of_frost_casts_scaled_cold_damage_and_declares_omitted_speed_rider(fake_llm_schema, fake_retrieval, band):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    text = """Mage
Medium humanoid (human), neutral evil
Armor Class 12
Hit Points 55
INT 16 (+3)
Spellcasting. The mage is a 7th level spellcaster. Its spellcasting ability is Intelligence
(spell save DC 14, spell attacks +6 to hit). It has the following spells prepared:
Cantrips (at will): Ray of Frost, Mending, Prestidigitation, Message.
First level (four slots): Expeditious Retreat, Jump, Thunderwave.
Second level (three slots): Enlarge/Reduce, Levitate.
Third level (three slots): Haste, Slow.
Fourth level (one slot): Polymorph.
Actions
Dagger. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 4 (1d4 + 2) piercing damage.
"""
    stats = parse_monster_stats(text, name="Mage")
    assert stats.spellcasting.spells == ((0, "Ray of Frost"),)
    assert "11 unsupported spell list entries" in " ".join(stats.declared)
    assert "10-foot speed reduction" in " ".join(stats.declared)
    foe = _foe("Mage", hp=55, ac=12)
    foe.update(init=20, band=band, monster_stats=stats.to_dict())
    state = _state("I wait", WAIT, foe)
    with use_rng(ScriptedDice([15, 3, 4] if band == "ranged" else [15, 2])):
        out = await combat_round_node(state)
    dice = out["adjudication_result"].dice_log
    attack = next(d for d in dice if d["purpose"] == "Ray of Frost spell attack")
    if band == "ranged":
        assert attack["total"] == 21
        damage = next(d for d in dice if d["purpose"] == "Ray of Frost damage")
        assert damage["expression"] == "2d8" and damage["total"] == 7
        assert "cold damage" in _summary(out)
    else:
        assert attack["roll_mode"] == "disadvantage" and attack["total"] == 8
        assert not any(d["purpose"] == "Ray of Frost damage" for d in dice)
    assert _actor(out, "player")["hp_current"] == CLERIC["hp_current"] - (7 if band == "ranged" else 0)
    assert _actor(out, "Mage")["monster_spell_slots_expended"] == {}
    assert "speed reduction" in _summary(out) and "not simulated" in _summary(out)
    assert not any(d.get("purpose", "").startswith("Mage Dagger") for d in dice)
    assert not effects_of(_actor(out, "player"))


@pytest.mark.asyncio
async def test_melee_hit_crit_against_paralyzed_hero_does_not_turn_a_miss_into_a_hit(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    text = """Bandit
Medium humanoid, neutral evil
Armor Class 15
Hit Points 20
STR 12 (+1) DEX 14 (+2) CON 12 (+1) INT 10 (+0) WIS 10 (+0) CHA 10 (+0)
Actions
Scimitar. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage.
"""
    stats = parse_monster_stats(text, name="Bandit")
    bandit = _foe("Bandit", hp=20, ac=stats.ac)
    bandit.update(band="melee", monster_stats=stats.to_dict())
    state = _state("I wait", WAIT, bandit)
    add_effect(_actor(state, "player"), "paralyzed", source="Hold Person", round_applied=1)
    with use_rng(ScriptedDice([3, 4])):  # lowest total 8 vs AC 14: miss, no damage roll
        out = await combat_round_node(state)
    assert _actor(out, "player")["hp_current"] == CLERIC["hp_current"]
    assert "Bandit's Scimitar misses" in _summary(out)
    assert "Critical hit" not in _summary(out)


def test_monster_spell_catalog_is_explicit_and_private_names_stay_out_of_public_declarations() -> None:
    stats = parse_monster_stats(
        SPELLCASTER.replace("Fire Bolt", "SENTINEL_SECRET_SPELL"), name="Priest"
    )
    assert stats.spellcasting is not None
    assert "SENTINEL_SECRET_SPELL" not in " ".join(stats.declared)
    assert "unsupported spell list entry not executed" in " ".join(stats.declared)
    assert {"Guiding Bolt", "Sacred Flame", "Hold Person"} <= {
        spell for _level, spell in stats.spellcasting.spells
    }
    assert "Fire Bolt" in {spell.name for spell in CATALOG}


def test_spell_list_leading_period_does_not_drop_the_first_spell_or_unsupported_names() -> None:
    text = SPELLCASTER.replace(
        "Cantrips (at will): Sacred Flame, Fire Bolt",
        "Cantrips (at will):.fire bolt, Unknown Spark",
    )
    stats = parse_monster_stats(text, name="Priest")

    assert stats.spellcasting is not None
    assert (0, "Fire Bolt") in stats.spellcasting.spells
    assert "1 unsupported spell list entry not executed" in " ".join(stats.declared)
    assert "Unknown Spark" not in " ".join(stats.declared)
