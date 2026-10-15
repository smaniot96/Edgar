"""EDG-149: code-owned rules tables pinned to the 2014 rulebooks (local PDFs, not in the repo).

Each assertion cites the printed rulebook page as its PDF page (player_handbook.pdf "PDF p.N";
dm_guide.pdf "DMG PDF p.N"; monster_manual.pdf "MM PDF p.N"). The values were read from the book,
not from memory. Regression tests for the discrepancies fixed in EDG-149 are marked "fixed".
"""

import random

import pytest

from agent.class_features import RAGE, max_uses, sneak_attack_roll
from agent.graph_combat import _player_ac
from agent.monster_stats import weapon_damage_type
from agent.resolution import (
    _WEAPONS,
    CASTING_ABILITY,
    HIT_DIE,
    CheckResult,
    ability_mod,
    choose_weapon,
    dc_for,
    heal_from_consumable,
    heal_from_rest,
    healing_dice_for,
    hit_dice_recovered_by_long_rest,
    proficiency_bonus,
    weapon_proficient,
)
from agent.spells import (
    CATALOG,
    FULL_CASTER_SLOTS,
    HALF_CASTER_SLOTS,
    _dice_count,
    slot_totals,
    spell_save_dc,
)

# --------------------------------------------------------------------------------------------
# Ability modifiers, proficiency and DCs
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "modifier"),
    [(1, -5), (2, -4), (3, -4), (4, -3), (5, -3), (6, -2), (7, -2), (8, -1), (9, -1), (10, 0),
     (11, 0), (12, 1), (13, 1), (14, 2), (15, 2), (16, 3), (17, 3), (18, 4), (19, 4), (20, 5),
     (21, 5), (22, 6), (23, 6), (24, 7), (25, 7), (26, 8), (27, 8), (28, 9), (29, 9), (30, 10)],
)  # fmt: skip
def test_ability_modifier_table_matches_the_book(score: int, modifier: int) -> None:
    # PHB "Ability Scores and Modifiers" table, PDF p.174.
    assert ability_mod(score) == modifier


@pytest.mark.parametrize(
    ("level", "bonus"),
    [(1, 2), (4, 2), (5, 3), (8, 3), (9, 4), (12, 4), (13, 5), (16, 5), (17, 6), (20, 6)],
)
def test_proficiency_bonus_by_level(level: int, bonus: int) -> None:
    # PHB Barbarian table ("Proficiency Bonus" column), PDF p.48.
    assert proficiency_bonus(level) == bonus


def test_difficulty_tiers_are_the_book_typical_dcs() -> None:
    # PHB "Typical Difficulty Classes", PDF p.175 (also DMG PDF p.238). The engine offers four
    # tiers: Easy 10, Medium (Moderate) 15, Hard 20, Very hard 25. Very easy 5 and nearly impossible
    # 30 are not offered as tiers.
    assert (dc_for("easy"), dc_for("medium"), dc_for("hard"), dc_for("very_hard")) == (
        10,
        15,
        20,
        25,
    )


# --------------------------------------------------------------------------------------------
# Classes: hit dice, saving throws, casting ability, weapon proficiency
# --------------------------------------------------------------------------------------------


def test_hit_die_by_class_matches_the_class_table() -> None:
    # PHB class table ("Hit Point Die"/"Hit Dice"), PDF p.46 and class sections.
    expected = {
        "barbarian": 12, "fighter": 10, "paladin": 10, "ranger": 10, "bard": 8, "cleric": 8,
        "druid": 8, "monk": 8, "rogue": 8, "warlock": 8, "sorcerer": 6, "wizard": 6,
    }  # fmt: skip
    assert {cls: HIT_DIE[cls] for cls in expected} == expected


def test_spellcasting_abilities_match_the_book() -> None:
    # PHB class sections: "X is your spellcasting ability" (wizard INT, cleric/druid/ranger WIS,
    # bard/paladin/sorcerer/warlock CHA), PDF pp.54, 59, 67, 85, 93, 102, 114. The artificer is not
    # in these books, so it is not checked here.
    book = {
        "wizard": "INT", "cleric": "WIS", "druid": "WIS", "ranger": "WIS", "bard": "CHA",
        "paladin": "CHA", "sorcerer": "CHA", "warlock": "CHA",
    }  # fmt: skip
    assert {cls: CASTING_ABILITY[cls] for cls in book} == book


def test_weapon_table_matches_the_phb_weapons_table() -> None:
    # PHB Weapons table, PDF p.150: name -> (damage dice, finesse, ranged, simple).
    book = {
        "club": ("1d4", False, False, True), "dagger": ("1d4", True, False, True),
        "greatclub": ("1d8", False, False, True), "handaxe": ("1d6", False, False, True),
        "javelin": ("1d6", False, False, True), "light hammer": ("1d4", False, False, True),
        "mace": ("1d6", False, False, True), "quarterstaff": ("1d6", False, False, True),
        "sickle": ("1d4", False, False, True), "spear": ("1d6", False, False, True),
        "light crossbow": ("1d8", False, True, True), "dart": ("1d4", True, True, True),
        "shortbow": ("1d6", False, True, True), "sling": ("1d4", False, True, True),
        "battleaxe": ("1d8", False, False, False), "flail": ("1d8", False, False, False),
        "glaive": ("1d10", False, False, False), "greataxe": ("1d12", False, False, False),
        "greatsword": ("2d6", False, False, False), "halberd": ("1d10", False, False, False),
        "lance": ("1d12", False, False, False), "longsword": ("1d8", False, False, False),
        "maul": ("2d6", False, False, False), "morningstar": ("1d8", False, False, False),
        "pike": ("1d10", False, False, False), "rapier": ("1d8", True, False, False),
        "scimitar": ("1d6", True, False, False), "shortsword": ("1d6", True, False, False),
        "trident": ("1d6", False, False, False), "war pick": ("1d8", False, False, False),
        "warhammer": ("1d8", False, False, False), "whip": ("1d4", True, False, False),
        "blowgun": ("1", False, True, False), "hand crossbow": ("1d6", False, True, False),
        "heavy crossbow": ("1d10", False, True, False), "longbow": ("1d8", False, True, False),
    }  # fmt: skip
    table = {w.name: (w.dice, w.finesse, w.ranged, w.simple) for w in _WEAPONS}
    for name, (dice, finesse, ranged, simple) in book.items():
        assert name in table, f"{name} is missing from the engine table"
        got = table[name]
        assert got[1:] == (finesse, ranged, simple), name
        # The blowgun's flat 1 damage is written 1d1 in the engine, which rolls exactly 1.
        assert got[0] == dice or (name == "blowgun" and got[0] == "1d1"), name


def test_greatclub_deals_its_own_damage_not_the_club_dice() -> None:
    # fixed: "greatclub" contains "club", so it used to match the club's 1d4 (PHB PDF p.150, 1d8).
    sheet = {"class": "fighter", "inventory": {"weapons": ["Greatclub"]}, "stats": {}}
    assert choose_weapon(sheet).name == "greatclub"
    assert choose_weapon(sheet).dice == "1d8"
    assert weapon_damage_type("greatclub") == "bludgeoning"  # PHB PDF p.150
    assert weapon_damage_type("blowgun") == "piercing"


def test_blowgun_is_a_martial_ranged_weapon_of_one_damage() -> None:
    # fixed: the blowgun was missing; PHB PDF p.150 lists it as martial ranged, 1 piercing.
    sheet = {"class": "rogue", "inventory": {"weapons": ["Blowgun"]}, "stats": {}}
    blowgun = choose_weapon(sheet)
    assert (blowgun.name, blowgun.ranged, blowgun.simple) == ("blowgun", True, False)
    assert not weapon_proficient({"class": "rogue"}, blowgun)  # rogues: simple, hand crossbow...


def test_class_weapon_proficiency_for_a_martial_weapon() -> None:
    # PHB class table, "Armor and Weapon Proficiencies", PDF p.46: a wizard has no martial weapons,
    # a fighter has them all.
    longsword = next(w for w in _WEAPONS if w.name == "longsword")
    assert not weapon_proficient({"class": "wizard"}, longsword)
    assert weapon_proficient({"class": "fighter"}, longsword)


# --------------------------------------------------------------------------------------------
# Armor class: armor table, medium DEX cap, shield, unarmored defense
# --------------------------------------------------------------------------------------------


def _armored(cls: str, armor: str, dex: int = 16, con: int = 14, wis: int = 10, shield=False):
    weapons = ["Shield"] if shield else []
    return {
        "class": cls,
        "level": 3,
        "stats": {"DEX": dex, "CON": con, "WIS": wis},
        "inventory": {"armor": armor, "weapons": weapons},
    }


@pytest.mark.parametrize(
    ("armor", "dex", "expected"),
    [
        ("padded", 16, 14),  # 11 + DEX
        ("leather armor", 16, 14),  # 11 + DEX
        ("studded leather", 16, 15),  # 12 + DEX
        ("hide armor", 16, 14),  # 12 + DEX (max 2): fixed, previously unarmored 13
        ("hide armor", 10, 12),  # DEX 10 is +0
        ("chain shirt", 16, 15),  # 13 + DEX (max 2)
        ("scale mail", 20, 16),  # 14 + DEX (max 2)
        ("breastplate", 20, 16),  # 14 + DEX (max 2)
        ("half plate", 20, 17),  # 15 + DEX (max 2)
        ("half plate", 6, 13),  # DEX 6 is -2, which applies in full, unlike the +2 cap
        ("ring mail", 20, 14),  # 14, no DEX
        ("chain mail", 20, 16),  # 16, no DEX
        ("splint", 20, 17),  # 17
        ("plate", 20, 18),  # 18
    ],
)  # fmt: skip
def test_armor_class_from_the_phb_armor_table(armor: str, dex: int, expected: int) -> None:
    # dex is the ability SCORE; the modifier is floor((score - 10) / 2).
    # PHB Armor table, PDF p.146. The medium-armor cap is +2 and light armor takes the full DEX.
    assert _player_ac(_armored("fighter", armor, dex=dex)) == expected


def test_shield_adds_two_to_the_armor_class() -> None:
    assert _player_ac(_armored("fighter", "chain mail", dex=10, shield=True)) == 18  # PDF p.146


def test_unarmored_defense_for_barbarian_and_monk() -> None:
    # Barbarian (PHB PDF p.49): 10 + DEX + CON; a shield still counts.
    assert _player_ac(_armored("barbarian", "", dex=14, con=16)) == 10 + 2 + 3
    assert _player_ac(_armored("barbarian", "", dex=14, con=16, shield=True)) == 10 + 2 + 3 + 2
    # Monk (PHB PDF p.79): 10 + DEX + WIS, with no shield; a shield removes the benefit.
    assert _player_ac(_armored("monk", "", dex=14, wis=16)) == 10 + 2 + 3
    assert _player_ac(_armored("monk", "", dex=14, wis=16, shield=True)) == 10 + 2 + 2
    # Everyone else unarmored: 10 + DEX.
    assert _player_ac(_armored("wizard", "", dex=14, con=16)) == 12


# --------------------------------------------------------------------------------------------
# Spells: catalog values, slot tables, cantrip scaling, DCs
# --------------------------------------------------------------------------------------------


def test_full_caster_slot_table_matches_the_wizard_table() -> None:
    # PHB Wizard table, PDF p.114 (spell slots per spell level, character levels 1 to 20).
    assert FULL_CASTER_SLOTS[0] == (2,)
    assert FULL_CASTER_SLOTS[4] == (4, 3, 2)  # level 5
    assert FULL_CASTER_SLOTS[8] == (4, 3, 3, 3, 1)  # level 9
    assert FULL_CASTER_SLOTS[16] == (4, 3, 3, 3, 2, 1, 1, 1, 1)  # level 17
    assert FULL_CASTER_SLOTS[19] == (4, 3, 3, 3, 3, 2, 2, 1, 1)  # level 20


def test_half_caster_slot_table_matches_the_paladin_table() -> None:
    # PHB Paladin table, PDF p.84 (the ranger uses the same slot progression, PDF p.91).
    assert HALF_CASTER_SLOTS[0] == ()  # level 1: no spells yet
    assert HALF_CASTER_SLOTS[1] == (2,)  # level 2
    assert HALF_CASTER_SLOTS[4] == (4, 2)  # level 5
    assert HALF_CASTER_SLOTS[8] == (4, 3, 2)  # level 9
    assert HALF_CASTER_SLOTS[16] == (4, 3, 3, 3, 1)  # level 17
    assert HALF_CASTER_SLOTS[19] == (4, 3, 3, 3, 2)  # level 20


def test_cantrip_dice_scale_at_levels_5_11_and_17() -> None:
    # PHB cantrip text, e.g. Fire Bolt "increases by 1d10 when you reach 5th, 11th and 17th level",
    # PDF p.243. Sacred Flame (PDF p.273) scales the same way.
    fire_bolt = next(s for s in CATALOG if s.name == "Fire Bolt")
    sacred_flame = next(s for s in CATALOG if s.name == "Sacred Flame")
    assert [_dice_count(fire_bolt, lvl, 0) for lvl in (1, 4, 5, 10, 11, 16, 17, 20)] == [
        1, 1, 2, 2, 3, 3, 4, 4,
    ]  # fmt: skip
    assert _dice_count(sacred_flame, 5, 0) == 2 and _dice_count(sacred_flame, 17, 0) == 4


def test_catalog_spells_match_the_book_dice_and_upcasts() -> None:
    by_name = {s.name: s for s in CATALOG}
    # Magic Missile: 3 darts of 1d4+1 force, one more dart per slot above 1st (PDF p.258).
    mm = by_name["Magic Missile"]
    assert (mm.level, mm.dice, mm.die, mm.per_die_bonus, mm.upcast_dice) == (1, 3, 4, 1, 1)
    # Cure Wounds: 1d8 + spellcasting modifier, +1d8 per slot above 1st (PDF p.231).
    cw = by_name["Cure Wounds"]
    assert (cw.level, cw.dice, cw.die, cw.upcast_dice, cw.ability_bonus) == (1, 1, 8, 1, True)
    # Healing Word: a bonus action, 1d4 + modifier, +1d4 per slot above 1st (PDF p.251).
    hw = by_name["Healing Word"]
    assert (hw.level, hw.dice, hw.die, hw.upcast_dice, hw.action) == (1, 1, 4, 1, "bonus_action")
    # Guiding Bolt: 4d6 radiant, +1d6 per slot above 1st (PDF p.249).
    gb = by_name["Guiding Bolt"]
    assert (gb.level, gb.dice, gb.die, gb.damage_type, gb.upcast_dice) == (1, 4, 6, "radiant", 1)
    # Blindness/Deafness: 2nd level, Constitution save, one minute, not concentration (PDF p.220).
    bd = by_name["Blindness/Deafness"]
    assert (bd.level, bd.save_ability, bd.duration_rounds) == (2, "CON", 10)
    # Sacred Flame: a cantrip, DEX save, 1d8 radiant (PDF p.273).
    sf = by_name["Sacred Flame"]
    assert (sf.level, sf.save_ability, sf.die, sf.damage_type) == (0, "DEX", 8, "radiant")


def test_spell_save_dc_and_attack_formula() -> None:
    # PHB spellcasting: spell save DC = 8 + proficiency + spellcasting modifier (PDF p.54).
    wizard = {"class": "wizard", "level": 5, "stats": {"INT": 16}}
    assert spell_save_dc(wizard) == 8 + 3 + 3
    assert slot_totals(wizard) == {1: 4, 2: 3, 3: 2}


# --------------------------------------------------------------------------------------------
# Consumables and rests
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "dice"),
    [
        ("Potion of healing", "2d4+2"),
        ("greater potion of healing", "4d4+4"),
        ("potion of superior healing", "8d4+8"),
        ("Potion of supreme healing", "10d4+20"),
        ("goodberry", "1d1"),
    ],
)
def test_healing_items_match_the_book(item: str, dice: str) -> None:
    # DMG "Potions of Healing" table, PDF p.188; goodberry: PHB PDF p.247 (1 HP per berry).
    assert healing_dice_for(item) == dice


def test_items_that_are_not_in_the_books_get_no_engine_healing() -> None:
    # fixed: "healing draught" and "healing elixir" were mapped to 2d4+2 without a book entry.
    assert healing_dice_for("healing draught") is None
    assert healing_dice_for("healing elixir") is None
    assert heal_from_consumable({"hp_current": 1}, "healing elixir") is None


def test_short_rest_regains_the_roll_with_no_minimum_of_one() -> None:
    # fixed: a short rest used to floor the healing at 1. PHB PDF p.187: the character regains hit
    # points equal to the total of the die and CON; a total below zero regains nothing.
    weak = {"class": "wizard", "level": 1, "hp_current": 1, "hp_max": 9, "stats": {"CON": 4}}
    for seed in range(40):
        healed = heal_from_rest(weak, "short", random.Random(seed))
        assert healed is not None and healed.dice is not None
        assert healed.amount == max(0, healed.dice.total)
    # 1d6 with CON 4 (-3): roll 1, 2 or 3 gives a total below zero, so it heals nothing.
    assert any(
        heal_from_rest(weak, "short", random.Random(seed)).amount == 0  # type: ignore[union-attr]
        for seed in range(40)
    )


@pytest.mark.parametrize(("level", "recovered"), [(1, 0), (2, 1), (3, 1), (4, 2), (5, 2), (20, 10)])
def test_long_rest_recovers_half_the_hit_dice_rounded_down(level: int, recovered: int) -> None:
    # fixed: a level-1 character used to recover one die. PHB PDF p.187: "up to ... half of the
    # character's total number of them" (no minimum in this book).
    assert hit_dice_recovered_by_long_rest({"level": level}) == recovered


# --------------------------------------------------------------------------------------------
# Class features
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "uses"),
    [(1, 2), (2, 2), (3, 3), (5, 3), (6, 4), (11, 4), (12, 5), (16, 5), (17, 6), (20, 6)],
)
def test_rage_uses_per_long_rest_by_level(level: int, uses: int) -> None:
    # PHB Barbarian table, "Rages" column, PDF p.48.
    assert max_uses({"class": "barbarian", "level": level}, RAGE) == uses


@pytest.mark.parametrize(("level", "uses"), [(1, 0), (2, 1), (16, 1), (17, 2), (20, 2)])
def test_action_surge_uses_by_level(level: int, uses: int) -> None:
    # PHB Fighter table, "Action Surge" column: one use from 2nd level, two from 17th (PDF p.72).
    assert max_uses({"class": "fighter", "level": level}, "action_surge") == uses


@pytest.mark.parametrize(("level", "dice"), [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (9, 5),
                                             (11, 6), (17, 9), (19, 10), (20, 10)])  # fmt: skip
def test_sneak_attack_dice_by_level(level: int, dice: int) -> None:
    # PHB Rogue table, "Sneak Attack" column, PDF p.96: 1d6 per two levels, rounded up.
    rogue = {"class": "rogue", "level": level, "inventory": {"weapons": ["Rapier"]}, "stats": {}}
    hit = CheckResult(
        check="attack", kind="attack", ability="DEX", modifier=5, dc=12, against="AC",
        roll_expression="1d20+5", natural=15, total=20, success=True,
        weapon=choose_weapon(rogue, "rapier"),
    )  # fmt: skip
    outcome = sneak_attack_roll(rogue, hit, mode="advantage", used=False, rng=random.Random(4))
    assert outcome is not None and outcome.expression == f"{dice}d6"


def test_restrained_foe_makes_its_dex_save_at_disadvantage() -> None:
    # fixed: a restrained foe's Dexterity saves were rolled normally. PHB Appendix A, PDF p.293:
    # "The creature has disadvantage on Dexterity saving throws."
    from agent.spells import plan_cast, resolve_spell

    cleric = {"name": "Ilse", "class": "cleric", "level": 5, "stats": {"WIS": 16}}
    sacred_flame = next(s for s in CATALOG if s.name == "Sacred Flame")
    plan, refusal = plan_cast(cleric, sacred_flame)
    assert refusal is None and plan is not None
    for seed in range(20):
        result = resolve_spell(
            cleric, plan, target_save_bonus=1, save_mode="disadvantage", rng=random.Random(seed)
        )
        log = result.dice_log[0]
        assert log["roll_mode"] == "disadvantage" and len(log["rolls"]) == 2
        assert log["kept"] == min(log["rolls"]) and log["total"] == log["kept"] + 1
        assert result.save_success == (log["total"] >= result.save_dc)
