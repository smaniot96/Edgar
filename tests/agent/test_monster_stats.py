"""Exact creature records as typed, executed stats (EDG-004).

The Troll record below is a synthetic, Troll-shaped stat block with generated numbers and prose
(there is no committed Troll text fixture). It carries every feature the parser must keep:
armor class and hit points, a DEX modifier, a Multiattack line, two attacks with bonus and damage,
Regeneration with an acid/fire suppression clause, and an unsupported trait that must be declared.
"""

import copy
import json
import random

import pytest

from agent.combatants import narrator_roster
from agent.encounter_budget import apply_solo_budget
from agent.graph_combat import EncounterSetup, EnemyStat, _build_enemy_roster, combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    MonsterStats,
    MonsterStatsError,
    adjust_monster_damage,
    attack_plan,
    combat_fields,
    declared_line,
    narrator_note,
    parse_monster_stats,
    regenerate,
    stats_of,
    weapon_damage_type,
)
from tests.agent.test_combat import FIGHTER, _player, _state
from tools.dice import use_rng

TROLL_TEXT = """Troll
Large giant, chaotic evil
Armor Class 14 (natural armor)
Hit Points 73 (7d10 + 35)
Speed 30 ft.
STR DEX CON INT WIS CHA
15 (+2) 12 (+1) 18 (+4) 7 (-2) 9 (-1) 6 (-2)
Saving Throws Dex +3
Skills Perception +3
Senses darkvision 60 ft., passive Perception 13
Languages Giant
Challenge 4 (1,100 XP)
Keen Smell. The troll has advantage on Wisdom (Perception) checks that rely on smell.
Regeneration. The troll regains 9 hit points at the start of its turn. If the troll takes acid or fire damage, this trait doesn't function at the start of the troll's next turn. The troll dies only if it starts its turn with 0 hit points and doesn't regenerate.
Actions
Multiattack. The troll makes three attacks: one with its bite and two with its claws.
Bite. Melee Weapon Attack: +6 to hit, reach 5 ft., one target. Hit: 7 (1d6 + 4) piercing damage.
Claw. Melee Weapon Attack: +6 to hit, reach 10 ft., one target. Hit: 11 (2d6 + 4) slashing damage.
"""  # fmt: skip

WYRMLING_TEXT = """Stone Wyrmling
Medium dragon, neutral
Armor Class 13
Hit Points 30 (4d8 + 12)
Speed 30 ft., fly 60 ft.
STR DEX CON INT WIS CHA
14 (+2) 10 (+0) 16 (+3) 8 (-1) 10 (+0) 9 (-1)
Damage Resistances slashing from nonmagical attacks; cold
Damage Immunities poison
Damage Vulnerabilities thunder
Senses blindsight 10 ft.
Challenge 2 (450 XP)
Breath Weapon (Recharge 5-6). The wyrmling exhales poison in a cone.
Actions
Claw. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 5 (1d6 + 2) slashing damage plus 3 (1d6) poison damage.
Legendary Actions
The wyrmling can take 1 legendary action.
Tail Swipe. The wyrmling makes a tail attack.
"""  # fmt: skip

UNREADABLE_MULTIATTACK_TEXT = """Ogre Brute
Large giant, chaotic evil
Armor Class 11
Hit Points 59 (7d10 + 21)
DEX 8 (-1)
Actions
Multiattack. The brute makes two attacks with its fists and one with its boot.
Greatclub. Melee Weapon Attack: +6 to hit, reach 5 ft., one target. Hit: 13 (2d8 + 4) bludgeoning damage.
"""


def troll_identity() -> dict:
    return {
        "creature_id": "f" * 64,
        "creature_name": "Troll",
        "creature_key": "troll",
        "text": TROLL_TEXT,
        "source": "Synthetic Manual",
        "collection": "rules_monster_manual",
        "page": 167,
        "source_pages": [167],
        "rules_edition": "2014",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "source_version": "version1",
        "index_version": "index1",
    }


def troll_combatant(init: int = 20, *, stats_override: dict | None = None) -> dict:
    """A roster entry built the way `_build_enemy_roster` builds one, from the record."""
    stats = parse_monster_stats(TROLL_TEXT, name="Troll")
    raw = stats.to_dict()
    raw.update(stats_override or {})
    return {
        "name": "Troll",
        "creature_id": "f" * 64,
        **combat_fields(stats),
        "monster_stats": raw,
        "is_boss": False,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": init,
        "init_bonus": 1,
        "initiative_bonus": 1,
    }


def _round_state(combatants: list[dict], message: str, target: str = "Troll") -> dict:
    state = _state(combatants, message)
    state["character"] = {**FIGHTER, "hp_current": 200, "hp_max": 200}
    state["parsed_input"] = ParsedInput(intent="combat", check="attack", target=target)
    return state


def install_troll(monkeypatch, text: str = TROLL_TEXT):
    identity = {**troll_identity(), "text": text}
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *a, **k: identity)
    return identity


# --------------------------------------------------------------------------------------------
# Parsing: the exact record's stat block, read by code
# --------------------------------------------------------------------------------------------


def test_troll_record_parses_every_supported_field() -> None:
    stats = parse_monster_stats(TROLL_TEXT, name="Troll")
    assert (stats.hp_max, stats.ac, stats.speed) == (73, 14, "30 ft.")
    assert stats.abilities == {"STR": 2, "DEX": 1, "CON": 4, "INT": -2, "WIS": -1, "CHA": -2}
    assert stats.initiative_bonus == 1 and stats.save_bonus("DEX") == 3
    assert [(a.name, a.bonus, a.damage_dice, a.damage_type) for a in stats.attacks] == [
        ("Bite", 6, "1d6+4", "piercing"),
        ("Claw", 6, "2d6+4", "slashing"),
    ]
    assert stats.multiattack == (("Bite", 1), ("Claw", 2))
    assert stats.regeneration is not None
    assert stats.regeneration.amount == 9
    assert stats.regeneration.suppressed_by == ("acid", "fire")
    assert not stats.resistances and not stats.immunities and not stats.vulnerabilities


def test_unsupported_traits_are_declared_never_implied() -> None:
    stats = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling")
    declared = " | ".join(stats.declared)
    assert "Breath Weapon (Recharge 5-6): recharge ability not executed" in declared
    assert "Legendary Actions" not in declared  # the section heading is not an entry
    assert "Tail Swipe: legendary action not executed" in declared
    assert "speed 30 ft., fly 60 ft.: movement is not simulated" in declared
    assert "Claw: damage rider or extra effect not executed" in declared
    assert stats.multiattack == ()
    assert [a.name for a in stats.attacks] == ["Claw"]


def test_resistances_immunities_and_vulnerabilities_are_typed() -> None:
    stats = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling")
    assert [(r.damage_type, r.nonmagical_only) for r in stats.resistances] == [
        ("slashing", True),
        ("cold", False),
    ]
    assert [r.damage_type for r in stats.immunities] == ["poison"]
    assert [r.damage_type for r in stats.vulnerabilities] == ["thunder"]


def test_unreadable_multiattack_is_declared_and_only_one_attack_is_used() -> None:
    stats = parse_monster_stats(UNREADABLE_MULTIATTACK_TEXT, name="Ogre Brute")
    assert stats.multiattack == ()
    assert any("Multiattack: counts could not be read exactly" in d for d in stats.declared)
    assert [a.name for a in attack_plan(stats)] == ["Greatclub"]


def test_record_without_armor_class_or_hit_points_is_rejected() -> None:
    with pytest.raises(MonsterStatsError):
        parse_monster_stats("Goblin\nArmor Class 15\nSpeed 30 ft.", name="Goblin")


def test_stats_survive_a_json_round_trip_exactly() -> None:
    stats = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling")
    reloaded = MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict())))
    assert reloaded == stats


def test_malformed_stored_stats_are_rejected_not_guessed() -> None:
    with pytest.raises(MonsterStatsError):
        MonsterStats.from_dict({"name": "Troll", "hp_max": "many", "ac": 14})


# --------------------------------------------------------------------------------------------
# Pure combat helpers
# --------------------------------------------------------------------------------------------


def test_multiattack_expands_to_one_attack_per_listed_count() -> None:
    stats = parse_monster_stats(TROLL_TEXT, name="Troll")
    assert [a.name for a in attack_plan(stats)] == ["Bite", "Claw", "Claw"]


def test_regeneration_heals_at_turn_start_up_to_the_maximum() -> None:
    combatant = troll_combatant()
    combatant["hp_current"] = 50
    healed, note = regenerate(combatant)
    assert healed == 9 and combatant["hp_current"] == 59
    assert note == "Troll regenerates 9 HP."
    combatant["hp_current"] = 70
    assert regenerate(combatant) == (3, "Troll regenerates 3 HP.")
    assert combatant["hp_current"] == 73  # the record's maximum


def test_regeneration_is_suppressed_by_fire_or_acid_taken_since_the_last_turn() -> None:
    combatant = troll_combatant()
    combatant["hp_current"] = 50
    combatant["damage_taken_types"] = ["fire"]
    healed, note = regenerate(combatant)
    assert healed == 0 and combatant["hp_current"] == 50
    assert note == "Troll does not regenerate: it took fire damage since its last turn."
    assert combatant["damage_taken_types"] == []  # the window resets at its turn


def test_regeneration_ignores_other_damage_and_full_health() -> None:
    combatant = troll_combatant()
    combatant["hp_current"] = 50
    combatant["damage_taken_types"] = ["slashing"]
    assert regenerate(combatant)[0] == 9
    combatant["hp_current"] = 73
    assert regenerate(combatant) == (0, None)


def test_legacy_combatant_has_no_regeneration_or_stats() -> None:
    legacy = {"name": "Goblin", "hp_current": 5, "hp_max": 7, "attack_bonus": 3}
    assert stats_of(legacy) is None
    assert regenerate(legacy) == (0, None)
    assert narrator_note(legacy) == ""
    assert declared_line(legacy) is None


def test_resistance_halves_immunity_blocks_and_vulnerability_doubles() -> None:
    wyrm = troll_combatant()
    wyrm["monster_stats"] = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling").to_dict()
    assert adjust_monster_damage(wyrm, 9, "slashing", magical=False) == (
        4,
        " (slashing damage halved: resistant)",
    )
    # A spell is magical, so a "from nonmagical attacks" resistance does not apply to it.
    assert adjust_monster_damage(wyrm, 9, "slashing", magical=True) == (9, "")
    assert adjust_monster_damage(wyrm, 9, "poison", magical=True)[0] == 0
    assert adjust_monster_damage(wyrm, 9, "thunder", magical=True) == (
        18,
        " (thunder damage doubled: vulnerable)",
    )
    assert adjust_monster_damage(wyrm, 9, "cold", magical=True)[0] == 4


def test_unknown_damage_type_applies_no_rule_and_says_so() -> None:
    wyrm = troll_combatant()
    wyrm["monster_stats"] = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling").to_dict()
    assert adjust_monster_damage(wyrm, 9, None, magical=False) == (
        9,
        " (damage type unknown: resistances and immunities not applied)",
    )
    troll = troll_combatant()
    assert adjust_monster_damage(troll, 9, None, magical=False) == (9, "")


def test_weapon_damage_types_come_from_the_engine_table() -> None:
    assert weapon_damage_type("longsword") == "slashing"
    assert weapon_damage_type("dagger") == "piercing"
    assert weapon_damage_type("club") == "bludgeoning"
    assert weapon_damage_type("weapon") is None  # the generic weapon is untyped


def test_narrator_roster_states_the_engine_traits_additively() -> None:
    troll = troll_combatant()
    text = narrator_roster([troll], [])
    assert 'id "Troll", name "Troll": active, can act' in text
    assert "multiattack: Bite x1, Claw x2" in text
    assert "regenerates 9 HP at the start of its turn unless it took acid or fire damage" in text
    assert "Combat roster (authoritative" in text  # the existing contract is unchanged


def test_declared_line_names_what_the_engine_does_not_run() -> None:
    wyrm = troll_combatant()
    wyrm["monster_stats"] = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling").to_dict()
    line = declared_line(wyrm)
    assert line.startswith("Declared, not executed for Stone Wyrmling: ")
    assert "Tail Swipe: legendary action not executed" in line


# --------------------------------------------------------------------------------------------
# Exact records through encounter initialisation (the reproduction of the gap)
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builder_numbers_never_replace_the_exact_record_numbers(fake_llm_schema, monkeypatch):
    install_troll(monkeypatch)
    # The builder proposes numbers for the Troll that disagree with its record.
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Troll", hp_max=8, ac=5, attack_bonus=9, damage_dice="4d12+5")]
    )
    roster = await _build_enemy_roster({"player_input": "I attack the troll"}, ["Troll"])
    (troll,) = roster
    assert (troll["hp_max"], troll["ac"], troll["attack_bonus"]) == (73, 14, 6)
    assert troll["damage_dice"] == "1d6+4"  # the first listed attack, from the record
    assert troll["monster_stats"] == parse_monster_stats(TROLL_TEXT, name="Troll").to_dict()
    assert troll["stat_adaptation"] == "exact_record"


@pytest.mark.asyncio
async def test_troll_record_survives_every_stage(fake_llm_schema, monkeypatch):
    """Record -> roster -> JSON round trip (persisted combatants) -> round -> persisted state."""
    install_troll(monkeypatch)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Troll", hp_max=8)]
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    roster = await _build_enemy_roster({"player_input": "I attack the troll"}, ["Troll"])
    expected = parse_monster_stats(TROLL_TEXT, name="Troll")
    persisted = json.loads(json.dumps(roster))  # the combatants JSONB column
    assert stats_of(persisted[0]) == expected

    player = _player(init=5, ac=18)
    player["name"] = "player"
    troll = {**persisted[0], "init": 20, "init_bonus": 1}
    state = _round_state([player, troll], "I attack the troll")
    with use_rng(random.Random(7)):
        out = await combat_round_node(state)
    summary = out["adjudication_result"].mechanical_summary
    attack_rolls = [
        d
        for d in out["adjudication_result"].dice_log
        if d.get("purpose", "").startswith("Troll ") and d["purpose"].endswith("attack")
    ]
    assert len(attack_rolls) == 3  # Bite, Claw, Claw: the record's multiattack
    assert "Troll's Bite" in summary and "Troll's Claw" in summary
    # The persisted combatant still carries the complete record after the round.
    saved = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Troll")
    assert stats_of(saved) == expected
    assert saved["creature_id"] == "f" * 64


@pytest.mark.asyncio
async def test_two_similarly_named_exact_foes_keep_their_own_stats(fake_llm_schema, monkeypatch):
    install_troll(monkeypatch)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Troll", hp_max=8), EnemyStat(name="Troll", hp_max=8)]
    )
    roster = await _build_enemy_roster({"player_input": "I attack the trolls"}, ["Troll"])
    assert [e["name"] for e in roster] == ["Troll", "Troll 2"]
    assert all(e["hp_max"] == 73 and stats_of(e) is not None for e in roster)
    assert roster[0]["monster_stats"] == roster[1]["monster_stats"]


@pytest.mark.asyncio
async def test_unparsable_exact_record_blocks_setup_instead_of_guessing(
    fake_llm_schema, monkeypatch
):
    from db.vector.creatures import CreatureResolutionError

    install_troll(monkeypatch, text="Troll\nArmor Class 14\nSpeed 30 ft.\n")
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Troll", hp_max=8)]
    )
    with pytest.raises(CreatureResolutionError, match="no parseable stat block"):
        await _build_enemy_roster({"player_input": "I attack the troll"}, ["Troll"])


# --------------------------------------------------------------------------------------------
# Rounds: engine dice drive multiattack, regeneration and damage types
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiattack_rolls_one_attack_per_listed_attack(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    troll = troll_combatant(init=20)
    state = _round_state([_player(init=5, ac=18), troll], "I attack the troll")
    with use_rng(random.Random(3)):
        out = await combat_round_node(state)
    rolls = out["adjudication_result"].dice_log
    attacks = [
        d
        for d in rolls
        if d.get("purpose", "").startswith("Troll ") and d["purpose"].endswith(" attack")
    ]
    assert [d["purpose"] for d in attacks] == [
        "Troll Bite attack",
        "Troll Claw attack",
        "Troll Claw attack",
    ]
    assert all(d["expression"].startswith("1d20+6") for d in attacks)


@pytest.mark.asyncio
async def test_multiattack_is_recorded_in_the_combat_round_telemetry(
    fake_llm_schema, fake_retrieval
):
    """EDG-031 round row: one actor entry for the Troll, one outcome per listed attack."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _round_state([_player(init=5, ac=18), troll_combatant(init=20)], "I attack the troll")
    with use_rng(random.Random(3)):
        out = await combat_round_node(state)
    payload = next(e for e in out["telemetry_events"] if e["event_type"] == "combat_round")[
        "payload"
    ]
    assert [a["id"] for a in payload["actors"]] == ["Troll"]
    assert [o["attack"] for o in payload["outcomes"]] == ["Bite", "Claw", "Claw"]
    assert all(o["actor"] == "Troll" and "roll_mode" in o for o in payload["outcomes"])


@pytest.mark.asyncio
async def test_regeneration_is_logged_at_the_start_of_the_monsters_turn(
    fake_llm_schema, fake_retrieval
):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    troll = troll_combatant(init=20)
    troll["hp_current"] = 50
    state = _round_state([_player(init=5, ac=18), troll], "I attack the troll")
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    summary = out["adjudication_result"].mechanical_summary
    assert "Troll regenerates 9 HP." in summary
    assert summary.index("Troll regenerates 9 HP.") < summary.index("Troll's Bite")


@pytest.mark.asyncio
async def test_suppressing_damage_stops_regeneration_in_the_round(fake_llm_schema, fake_retrieval):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    troll = troll_combatant(init=20)
    troll["hp_current"] = 50
    troll["damage_taken_types"] = ["fire"]
    state = _round_state([_player(init=5, ac=18), troll], "I attack the troll")
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    summary = out["adjudication_result"].mechanical_summary
    assert "Troll does not regenerate: it took fire damage since its last turn." in summary
    assert "regenerates 9 HP" not in summary


@pytest.mark.asyncio
async def test_resistance_applies_to_a_known_weapon_type_in_the_round(
    fake_llm_schema, fake_retrieval
):
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    wyrm = troll_combatant(init=1)
    wyrm["monster_stats"] = parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling").to_dict()
    wyrm.update(combat_fields(parse_monster_stats(WYRMLING_TEXT, name="Stone Wyrmling")))
    wyrm["name"] = "Stone Wyrmling"
    checked = 0
    for seed in range(60):
        state = _round_state(
            [_player(init=20, ac=18), copy.deepcopy(wyrm)],
            "I attack the stone wyrmling",
            target="Stone Wyrmling",
        )
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        summary = out["adjudication_result"].mechanical_summary
        if "hits Stone Wyrmling" not in summary:
            continue
        damage = next(
            d for d in out["adjudication_result"].dice_log if d.get("purpose") == "player damage"
        )
        foe = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Stone Wyrmling")
        assert "(slashing damage halved: resistant)" in summary
        assert foe["hp_current"] == max(0, 30 - damage["total"] // 2)
        checked += 1
        if checked == 3:
            break
    assert checked == 3


def test_solo_budget_never_rescales_an_exact_record() -> None:
    troll = troll_combatant()
    out, summary = apply_solo_budget([troll], 1)
    (kept,) = out
    assert kept["hp_max"] == 73 and kept["ac"] == 14
    assert kept["monster_stats"] == troll["monster_stats"]
    assert any(note.startswith("unscaled exact creature: Troll") for note in summary["notes"])
    assert summary["adapted"] is False  # an unscaled record is a note, not a change


@pytest.mark.asyncio
async def test_combat_start_declares_unsupported_traits_and_notes_the_unscaled_record(
    fake_llm_schema, fake_retrieval, monkeypatch
):
    identity = {**troll_identity(), "text": WYRMLING_TEXT, "creature_name": "Stone Wyrmling"}
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *a, **k: identity)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Stone Wyrmling", hp_max=8)]
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _round_state([], "I attack the stone wyrmling", target="Stone Wyrmling")
    state["combat_state"] = {}
    state["character"] = {**FIGHTER, "level": 1}
    with use_rng(random.Random(4)):
        out = await combat_round_node(state)
    summary = out["adjudication_result"].mechanical_summary
    assert "Declared, not executed for Stone Wyrmling:" in summary
    assert "Tail Swipe: legendary action not executed" in summary
    assert "Breath Weapon (Recharge 5-6): recharge ability not executed" in summary
    assert "unscaled exact creature: Stone Wyrmling keeps its record HP 30" in summary
    assert "Encounter adapted" not in summary  # an unscaled record is not an adaptation
    (wyrm,) = [c for c in out["combat_state"]["combatants"] if not c.get("is_player")]
    assert wyrm["hp_max"] == 30 and stats_of(wyrm) is not None
    assert out["combat_state"]["solo_budget"]["adapted"] is False


# --------------------------------------------------------------------------------------------
# EDG-149: real-layout quirks found by reading the Monster Manual stat blocks. The fixtures below
# are synthetic: every number and sentence is written for this test, in the layout the rulebook
# text layer produces ("Melee or Ranged Weapon Attack", "non magical", "(Recharge 5–6)" with an en
# dash, a spaced minus "(- 1)"). No rulebook text is copied.
# --------------------------------------------------------------------------------------------

CAPTAIN_TEXT = """Gutter Captain
Medium humanoid (goblinoid), lawful evil
Armor Class 16 (scale mail, shield)
Hit Points 39 (6d8 + 12)
Speed 30 ft.
STR DEX CON INT WIS CHA
14 (+2) 12 (+1) 15 (+2) 9 (- 1) 11 (+0) 13 (+1)
Saving Throws Dex +3
Senses darkvision 60 ft., passive Perception 10
Languages Common, Goblin
Challenge 2 (450 XP)
Actions
Multiattack. The captain makes two attacks with its cudgel. The second attack has disadvantage.
Cudgel. Melee or Ranged Weapon Attack: +4 to hit, reach 5 ft. or range 20/60 ft., one target. Hit: 6 (1d8 + 2) bludgeoning damage.
Spit Breath (Recharge 5–6). The captain exhales acid in a 15-foot cone. Each creature in that area must make a DC 12 Dexterity saving throw, taking 10 (3d6) acid damage on a failed save, or half as much damage on a successful one.
"""  # fmt: skip

SENTINEL_TEXT = """Stone Sentinel
Large construct, unaligned
Armor Class 15 (natural armor)
Hit Points 45 (6d10 + 12)
Speed 30 ft.
STR DEX CON INT WIS CHA
16 (+3) 10 (+0) 14 (+2) 3 (-4) 10 (+0) 1 (-5)
Damage Resistances bludgeoning, piercing, and slashing from non magical attacks that aren't silvered
Damage Immunities poison
Senses darkvision 60 ft., passive Perception 10
Challenge 3 (700 XP)
Actions
Slam. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 8 (1d10 + 3) bludgeoning damage.
"""  # fmt: skip


def test_text_layer_spacing_and_melee_or_ranged_attacks_are_read() -> None:
    # fixed: "(- 1)" with a space and "Melee or Ranged Weapon Attack" were not read, so the
    # Wisdom modifier was lost and the attack was declared as a trait.
    stats = parse_monster_stats(CAPTAIN_TEXT, name="Gutter Captain")
    assert stats.abilities == {"STR": 2, "DEX": 1, "CON": 2, "INT": -1, "WIS": 0, "CHA": 1}
    cudgel = stats.attacks[0]
    assert (cudgel.name, cudgel.bonus, cudgel.damage_dice, cudgel.damage_type) == (
        "Cudgel",
        4,
        "1d8+2",
        "bludgeoning",
    )
    assert not cudgel.ranged  # reach 5 ft. is the melee reading of "Melee or Ranged"
    assert not any("Cudgel" in d and "not executed" in d for d in stats.declared)


def test_multiattack_that_names_one_attack_counts_it_and_declares_the_disadvantage() -> None:
    # fixed: "makes two attacks with its cudgel" was read as no counts at all.
    stats = parse_monster_stats(CAPTAIN_TEXT, name="Gutter Captain")
    assert stats.multiattack == (("Cudgel", 2),)
    assert [a.name for a in attack_plan(stats, spent=["Spit Breath"])] == ["Cudgel", "Cudgel"]
    assert "Multiattack: the disadvantage on a later attack is not executed" in stats.declared
    assert not any("counts could not be read" in d for d in stats.declared)


def test_recharge_with_an_en_dash_is_read_as_its_own_entry() -> None:
    # fixed: "Spit Breath (Recharge 5–6)" failed the name pattern and was glued onto the previous
    # entry, so it was never declared. Its exact breath text is now executed (EDG-043, see
    # test_monster_save_actions.py).
    stats = parse_monster_stats(CAPTAIN_TEXT, name="Gutter Captain")
    assert [(a.name, a.recharge) for a in stats.attacks] == [("Cudgel", None), ("Spit Breath", 5)]
    assert not any("Spit Breath" in d for d in stats.declared)


def test_non_magical_split_by_the_text_layer_still_gives_the_resistance() -> None:
    # fixed: "non magical" (two words) was read as a magic qualifier, so the resistance was dropped.
    stats = parse_monster_stats(SENTINEL_TEXT, name="Stone Sentinel")
    assert [(r.damage_type, r.nonmagical_only) for r in stats.resistances] == [
        ("bludgeoning", True),
        ("piercing", True),
        ("slashing", True),
    ]
    assert [r.damage_type for r in stats.immunities] == ["poison"]
    assert stats.abilities["INT"] == -4 and stats.abilities["CHA"] == -5


def test_silvered_exception_on_a_nonmagical_resistance_is_declared_not_guessed() -> None:
    # The hero's weapons are plain, so the resistance applies to them; the silvered exception is
    # declared rather than silently dropped.
    stats = parse_monster_stats(SENTINEL_TEXT, name="Stone Sentinel")
    assert any("silvered or adamantine exception not executed" in d for d in stats.declared)
    assert adjust_monster_damage(combat_fields_for(stats), 8, "bludgeoning", magical=False)[0] == 4


def combat_fields_for(stats) -> dict:
    """A roster-shaped dict carrying the parsed stats, for the damage-rule helper."""
    return {"name": stats.name, "monster_stats": stats.to_dict()}


def test_spellcasting_reads_only_catalog_spells_and_persists_the_slot_profile() -> None:
    text = """Mage
Medium humanoid, neutral
Armor Class 12
Hit Points 40
INT 18 (+4)
Spellcasting. The mage is a 4th-level spellcaster. Its spellcasting ability is Intelligence
(spell save DC 14; +6 to hit with spell attacks). The mage has the following wizard spells prepared:
Cantrips (at will): Fire Bolt, mage hand
1st level (4 slots): Magic Missile, shield.
"""
    stats = parse_monster_stats(text, name="Mage")
    assert stats.spellcasting is not None
    assert (
        stats.spellcasting.ability,
        stats.spellcasting.caster_level,
        stats.spellcasting.save_dc,
        stats.spellcasting.attack_bonus,
        stats.spellcasting.spells,
        stats.spellcasting.slots,
    ) == ("INT", 4, 14, 6, ((0, "Fire Bolt"), (1, "Magic Missile")), {"1": 4})
    assert "2 unsupported spell list entries not executed" in " ".join(stats.declared)
    assert "mage hand" not in " ".join(stats.declared).lower()
    assert "shield" not in " ".join(stats.declared).lower()
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats
