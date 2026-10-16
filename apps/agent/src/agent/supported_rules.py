"""Code-owned contract for the simplified 5e subset this engine simulates (EDG-028).

The contract has three parts, all derived from what the engine actually executes:

  * `SUPPORTED_MECHANICS` - each entry names the implementing function and a regression test
    that must exist. `tests/agent/test_supported_rules.py` checks both references, so a
    "supported" claim cannot outlive its code path or test.
  * `UNSUPPORTED_MECHANICS` - rules the engine does not execute. A player who declares one of
    them receives a transparent narrative ruling instead of an engine effect.
  * `SOLO_ADAPTATIONS` - deliberate departures from table play for a single hero.

Runtime enforcement uses the same vocabulary:

  * `classify_unsupported_actions` maps a declared action (player text + parser output) to
    unsupported kinds. A catalog spell (agent.spells) is executed by the engine and is not
    unsupported; any other declared spell, or a catalog spell the character cannot cast, is.
  * `apply_adjudication_boundary` strips LLM-proposed conditions, flags and damage that claim
    an unsupported effect, and states the ruling in the engine's mechanical summary.
  * `apply_narration_boundary` removes narration sentences that claim an unsupported effect and
    appends the ruling, so a fake or misbehaving provider cannot present one as executed.

The engine-executed facts of a turn travel as one `EngineApplied` value, built once from the
state by `engine_applied_for_state`. Adding a new engine-executed fact means:
  1. a typed effect: the combat round records its id in `engine_effects`, and its claim is
     exempted where it is matched (`_claim_words`, `_claim_pattern`, `_executed_economy_phrase`);
     or a new fact: add one field to `EngineApplied` and read it in `engine_applied_for_state`;
  2. nothing else: do not add a keyword argument to the classification or boundary functions.

Engine-owned dice and numbers are unchanged: this module never rolls and never sets a number.
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.class_features import (
    FEATURE_EFFECTS,
    IMPLEMENTED_FEATURES,
    NARRATION_CLAIMS,
    requested_features,
    ruling as feature_ruling,
)
from agent.combatants import classify_combat_maneuver
from agent.resolution import normalise_check
from agent.spells import CATALOG, cast_request

CONTRACT_ID = "edgar.supported-5e-subset"
CONTRACT_VERSION = 1

# EDG-147: the classifier reads declarations in English first. Italian and Spanish words are read for a
# few casting, advantage, cover, lethality and action terms only; the engine's spell resolution needs the
# English spell name in the declaration. Other languages rely on the parser's own entities.
DECLARATION_LANGUAGE = (
    "Declared actions are read in English. Italian and Spanish are read only for a few casting, "
    "advantage, cover, lethality and action words. A catalog spell resolves only when its English name "
    "is in the declaration; a spell the parser names from another language is ruled as not resolved. "
    "Other languages are not classified by these words and rely on the parser's own entities."
)

EDITION_BASIS = (
    "Engine numbers follow the 2014 core rules (d20 checks, DC tiers 10/15/20/25, ability "
    "modifiers, proficiency progression, weapon and armor tables). A campaign's rules_edition "
    "(2014 or 2024) selects which source excerpts are retrieved; it does not switch the engine "
    "to a different rule set, and no 2024-only mechanic is implemented."
)


@dataclass(frozen=True)
class Mechanic:
    id: str
    title: str
    scope: str
    implementation: str  # "module:attribute" that must import
    test: str  # "relative/path.py::test_name" that must exist


@dataclass(frozen=True)
class UnsupportedMechanic:
    id: str
    title: str
    boundary: str  # what the engine does instead, player-facing
    owner: str  # register story that owns the feature, or "unassigned"


SUPPORTED_MECHANICS: tuple[Mechanic, ...] = (
    Mechanic(
        id="d20_checks",
        title="d20 ability, skill, saving-throw and tool checks",
        scope=(
            "d20 plus the ability modifier, plus the proficiency bonus when the sheet (or the "
            "class default) makes the skill, save or tool proficient, against a DC of 10 (easy), "
            "15 (medium, default), 20 (hard) or 25 (very hard), the PHB and DMG typical DCs for "
            "those tiers; the very easy (5) and nearly impossible (30) DCs are not offered as tiers. "
            "Advantage and disadvantage apply only from engine-owned sources (see "
            "advantage_disadvantage)."
        ),
        implementation="agent.resolution:resolve_check",
        test="tests/agent/test_supported_rules.py::test_supported_d20_checks_use_dc_tiers",
    ),
    Mechanic(
        id="advantage_disadvantage",
        title="Advantage and disadvantage",
        scope=(
            "A check or attack rolls two engine d20s and keeps the higher (advantage) or the "
            "lower (disadvantage); both faces are logged. Advantage with disadvantage cancels to "
            "a normal roll. A mode comes only from engine state: an attack against an unconscious target has "
            "advantage, and the restrained or prone effects of a supported combat maneuver set "
            "advantage or disadvantage on attacks (see contested_maneuvers), and a hero who takes the "
            "Dodge action gives foes disadvantage on attacks until the hero's next turn (see "
            "action_economy). A player's or the model's claim of advantage is never a source and is not "
            "applied."
        ),
        implementation="agent.resolution:resolve_check",
        test="tests/agent/test_supported_rules.py::test_supported_advantage_and_disadvantage",
    ),
    Mechanic(
        id="modifiers_and_proficiency",
        title="Ability modifiers and proficiency progression",
        scope=(
            "Ability modifier is floor((score - 10) / 2). Proficiency bonus is +2 at level 1, "
            "rising by one every four levels. It is added only where proficiency applies."
        ),
        implementation="agent.resolution:proficiency_bonus",
        test=(
            "tests/agent/test_supported_rules.py::test_supported_ability_modifiers_and_proficiency"
        ),
    ),
    Mechanic(
        id="weapon_attacks",
        title="Weapon attack rolls and damage",
        scope=(
            "d20 plus attack ability (STR, or DEX for finesse and ranged weapons) plus "
            "proficiency against target AC. A natural 20 always hits and doubles the weapon's "
            "dice; a natural 1 always misses. Only weapons in the engine table are recognised "
            "(others use a generic d8). The proficiency bonus is added only when the class or "
            "sheet makes the weapon, or its simple or martial category, proficient; unarmed and "
            "unrecognised attacks keep it. A versatile weapon (quarterstaff, spear, trident, "
            "battleaxe, longsword, warhammer) deals its two-handed damage (PHB, PDF p.148) when the "
            "player declares both hands and the sheet lists no shield."
        ),
        implementation="agent.resolution:resolve_attack",
        test="tests/agent/test_supported_rules.py::test_supported_weapon_attack_crit_and_fumble",
    ),
    Mechanic(
        id="armor_class",
        title="Armor class from armor, DEX and shield",
        scope=(
            "AC from the armor table (PHB, 2014), DEX modifier as capped by the armor type (medium "
            "armor at most +2), plus 2 for a shield. Unarmored characters use 10 plus DEX, except a "
            "barbarian (10 plus DEX plus CON, a shield still counts) and a monk without a shield (10 "
            "plus DEX plus WIS). Armor is matched by name; heavy-armor Strength requirements and "
            "stealth are not applied."
        ),
        implementation="agent.graph_combat:_player_ac",
        test="tests/agent/test_supported_rules.py::test_supported_armor_class_from_table",
    ),
    Mechanic(
        id="initiative",
        title="Initiative",
        scope="d20 plus DEX modifier for the player and each enemy; the roster sorts highest first.",
        implementation="agent.resolution:initiative_bonus",
        test="tests/agent/test_supported_rules.py::test_supported_initiative_uses_dex",
    ),
    Mechanic(
        id="combat_rounds",
        title="Combat rounds in initiative order",
        scope=(
            "One round per player turn. Enemies act in initiative order: attack roll d20 plus "
            "bonus against the player's AC, and engine-rolled damage on a hit. HP changes only "
            "from engine rolls. The encounter ends in victory when every enemy reaches 0 HP, in "
            "defeat when the player does, in flight when the player leaves, or at the round cap."
        ),
        implementation="agent.graph_combat:combat_round_node",
        test="tests/agent/test_supported_rules.py::test_supported_combat_round_victory",
    ),
    Mechanic(
        id="second_wind",
        title="Fighter Second Wind (one use, restored by rests)",
        scope=(
            "Fighter only, one use (PHB, 2014, PDF p.73). The player's action spends it, in or out of combat: the engine "
            "rolls 1d10 plus fighter level and caps the result at max HP. A request with no use left "
            "is refused without a roll. Uses are stored on the character sheet, so they carry over "
            "between encounters, and a short or long rest restores the use. There is no automatic "
            "use at 0 HP. It is a bonus action: the hero may use it in the same turn as an action, "
            "such as an attack, and not twice in one turn (see action_economy). 2024 campaigns follow "
            "this same single-use rule; the 2024 class table (more uses at higher levels) is not "
            "implemented."
        ),
        implementation="agent.resolution:spend_second_wind",
        test="tests/agent/test_supported_rules.py::test_supported_second_wind_uses_and_rest_recovery",
    ),
    Mechanic(
        id="class_feature_action_surge",
        title="Fighter Action Surge (one more attack, restored by rests)",
        scope=(
            "Fighter level 2 and up; one use, or two from level 17 (PHB, PDF p.72-73: the book grants "
            "one additional action, and the engine executes only an extra weapon attack). Declared "
            "with an attack on a foe in combat, a use adds one more weapon attack against that foe in "
            "the same round, with its "
            "own engine roll and damage. A request with no use left, or with no attack, is refused "
            "without a roll and spends nothing. Uses are stored on the character sheet, and a short or "
            "long rest restores them. Outside combat it has no effect."
        ),
        implementation="agent.class_features:spend_use",
        test="tests/agent/test_class_features.py::test_action_surge_adds_one_attack_and_recharges",
    ),
    Mechanic(
        id="class_feature_sneak_attack",
        title="Rogue Sneak Attack (once per turn, with engine advantage)",
        scope=(
            "Rogue. A hit with a finesse or ranged weapon, on an attack that has advantage from an "
            "engine source (see advantage_disadvantage), adds 1d6 per two rogue levels, rounded up "
            "(PHB, 2014, PDF p.96), once per turn. A critical hit doubles these dice. The book's other "
            "trigger, an ally within 5 feet of the target that is not incapacitated, is not tracked, "
            "so an attack without engine advantage gets no Sneak Attack. It has no uses to restore."
        ),
        implementation="agent.class_features:sneak_attack_roll",
        test=(
            "tests/agent/test_class_features.py::"
            "test_sneak_attack_needs_engine_advantage_and_finesse_or_ranged"
        ),
    ),
    Mechanic(
        id="class_feature_rage",
        title="Barbarian Rage (damage bonus; uses restored by a long rest)",
        scope=(
            "Barbarian (PHB, PDF pp.48-49). Uses per long rest: 2 at level 1, 3 at level 3, 4 at level 6, "
            "5 at level 12 and 6 at level 17. Declared in combat, rage starts without using the round's attack (bonus "
            "actions are not tracked) and adds +2 damage, or +3 from level 9 and +4 from level 16, to each "
            "hit with a melee weapon attack that uses Strength. It lasts until the encounter ends; the "
            "1-minute limit is not tracked. A long rest restores uses; a short rest does not. Damage "
            "resistance and the Strength advantage are not applied."
        ),
        implementation="agent.class_features:rage_damage_bonus",
        test=(
            "tests/agent/test_class_features.py::"
            "test_rage_adds_damage_uses_recharge_and_ends_with_the_encounter"
        ),
    ),
    Mechanic(
        id="hazard_damage",
        title="Hazard damage after a failed check",
        scope=(
            "The adjudicator may propose damage dice for a hazard. The engine rolls them, caps "
            "them by level, and applies them only when this turn's check failed. A bare HP number "
            "from the model is never applied as damage."
        ),
        implementation="agent.resolution:enforce_side_effects",
        test="tests/agent/test_supported_rules.py::test_supported_hazard_damage_needs_failed_check",
    ),
    Mechanic(
        id="consumable_healing",
        title="Healing consumables from inventory",
        scope=(
            "A healing potion heals only when the sheet's inventory actually holds it, and the "
            "item is then removed. Dice: healing 2d4+2, greater 4d4+4, superior 8d4+8, supreme "
            "10d4+20; a goodberry restores 1 HP."
        ),
        implementation="agent.resolution:heal_from_consumable",
        test="tests/agent/test_supported_rules.py::test_supported_consumable_healing_needs_inventory",
    ),
    Mechanic(
        id="supported_spells",
        title=(
            "Catalog spells: Fire Bolt, Magic Missile, Sacred Flame, Cure Wounds, Healing Word, "
            "Guiding Bolt, Blindness/Deafness and Hold Person"
        ),
        scope=(
            "Only these declared spells resolve, and only when the declaration names the spell in English. "
            "Fire Bolt is a spell attack (1d10 fire, two dice "
            "at 5th level and up, more at 11th and 17th; a critical doubles the dice). Magic Missile "
            "is three automatic-hit darts of 1d4+1 force, one more dart per slot above 1st. Sacred "
            "Flame is a DEX save by the target against the caster's spell save DC, with 1d8 radiant "
            "damage on a failure only. Cure Wounds restores 1d8 plus the spellcasting modifier to the "
            "caster, one die more per slot above 1st; it cannot target anyone else. Healing Word is a "
            "bonus action: 1d4 plus the spellcasting modifier to the caster, one die more per slot above "
            "1st, and the same turn may also attack. Guiding Bolt is a spell attack for 4d6 radiant, one "
            "die more per slot above 1st, and a hit marks the foe (see condition_effects). "
            "Blindness/Deafness is a Constitution save against the spell save DC, with no damage; it "
            "casts blindness only, at one foe (a declaration naming deafness is refused), and does not "
            "scale to extra targets. Hold Person is a Wisdom save against the spell save DC that paralyzes "
            "one humanoid foe for up to a minute, is held by concentration (see concentration) and does not "
            "scale to extra targets. Cantrips cost no slot. A leveled spell spends the named slot or the "
            "lowest one available; slot totals follow class and level for the full and half casters, and "
            "a long rest restores them. Other spells get a narrative ruling."
        ),
        implementation="agent.spells:resolve_spell",
        test="tests/agent/test_spells.py::test_catalog_spell_attack_hit_miss_and_crit",
    ),
    Mechanic(
        id="condition_effects",
        title="Blinded and marked foes from catalog spells (engine-applied conditions)",
        scope=(
            "Blindness/Deafness blinds a foe that fails its Constitution save, for one minute (10 "
            "rounds). A blinded foe's own attacks have disadvantage and attacks against it have "
            "advantage. At the end of each of its turns the foe repeats the Constitution save, and a "
            "success ends the effect. Guiding Bolt marks a foe it hits: the next attack roll against "
            "that foe has advantage, the roll spends the mark, and an unspent mark ends at the end of "
            "the caster's next turn. Hold Person paralyzes a humanoid foe that fails its Wisdom save: it "
            "cannot act but stays in the fight, attacks against it have advantage, a melee hit from within "
            "5 feet is a critical hit (the engine does not track distance for a reach weapon, so none is "
            "applied for one), and it repeats the save at the end of each of its turns. A foe whose record "
            "gives no humanoid type is refused before any roll. The condition is stored on the foe, shown in "
            "the combat roster and given to the narrator; it ends with the foe's defeat or the encounter. "
            "No other condition has an engine effect on a foe."
        ),
        implementation="agent.conditions:apply_spell_condition",
        test="tests/agent/test_concentration.py::test_hold_person_paralyzes_a_humanoid_until_its_repeat_save",
    ),
    Mechanic(
        id="concentration",
        title="Concentration on one spell, with its damage save",
        scope=(
            "The caster holds at most one concentration spell (Hold Person). Casting another such spell "
            "ends the first, and the first spell's effects on foes end with it. Concentration ends when "
            "the spell's minute passes, when no foe still carries its effect, and when the caster falls to "
            "0 HP. Taking damage while concentrating requires a Constitution save, engine-rolled with the "
            "sheet's save modifier (proficiency included when the sheet lists it): DC 10 or half the damage, "
            "whichever is higher, rounded down; a failure ends the spell. A spell with no effect on a foe "
            "does not start concentration."
        ),
        implementation="agent.conditions:concentration_save",
        test="tests/agent/test_concentration.py::test_damage_breaks_concentration_only_when_the_save_fails",
    ),
    Mechanic(
        id="monster_riders",
        title="Poisoned rider on an exact monster's hit",
        scope=(
            "An exact record whose attack reads exactly as 'and the target must succeed on a DC N "
            "Constitution saving throw or be poisoned for 1 minute, and it can repeat the save at the end "
            "of each of its turns' is executed on a hit. The hero makes the engine-rolled save with the "
            "sheet's Constitution modifier; a failure poisons the hero, which gives its own attack rolls "
            "disadvantage (ability checks are not penalised). The hero repeats the save at the end of its own "
            "turn, and a success ends the effect. Any other rider clause, such as extra poison damage or "
            "unconsciousness, stays declared and is not executed."
        ),
        implementation="agent.conditions:rider_save",
        test="tests/agent/test_monster_riders.py::test_poisoned_rider_on_a_hit_saves_and_the_hero_repeats_it",
    ),
    Mechanic(
        id="monster_recharge",
        title="Recharge X-6 attacks of exact records",
        scope=(
            "An exact record's attack named 'Recharge X-6' (or 'Recharge 6') with an exactly readable attack "
            "text is used once, then waits: at the start of each of the monster's turns it rolls a d6 and "
            "regains the attack on X or higher. A ready recharge attack is used before the creature's other "
            "attack. Breath and other area or save-based recharge abilities stay declared."
        ),
        implementation="agent.monster_stats:recharge_turn_start",
        test="tests/agent/test_monster_riders.py::test_recharge_attack_waits_for_its_d6_roll",
    ),
    Mechanic(
        id="long_rest",
        title="Long rest healing and Hit Dice recovery",
        scope=(
            "A long rest (sleep or camp-for-the-night wording) restores HP to the maximum, every "
            "expended spell slot, a fighter's Second Wind and Action Surge uses, and a barbarian's "
            "Rage uses. It also regains spent Hit Dice up to half the character's total, rounded "
            "down (PHB, 2014, PDF p.187), so a level-1 character regains none. 2024 campaigns follow "
            "the same Hit Dice rule in this engine. The book's minimum rest length, the once-per-24-hour "
            "limit and the at-least-1-HP start condition are not checked. No other long-rest resource "
            "is reset by the engine."
        ),
        implementation="agent.resolution:heal_from_rest",
        test="tests/agent/test_supported_rules.py::test_supported_long_rest_restores_hp",
    ),
    Mechanic(
        id="short_rest",
        title="Short rest healing spends one Hit Die",
        scope=(
            "A short rest (rest-for-an-hour wording) spends one Hit Die of the class and rolls it "
            "plus the CON modifier; the character regains that total (PHB, 2014, PDF p.187; no "
            "minimum), capped at the missing HP. With no Hit Dice left it is refused: nothing is "
            "rolled and nothing is healed until a long rest. The rules allow one or more dice; a "
            "declared count of Hit Dice is not parsed, so one die is spent. A short rest also "
            "restores a fighter's Second Wind and Action Surge uses."
        ),
        implementation="agent.resolution:heal_from_rest",
        test="tests/agent/test_supported_rules.py::test_supported_short_rest_spends_one_hit_die",
    ),
    Mechanic(
        id="hit_dice",
        title="Hit Dice ledger on the character sheet",
        scope=(
            "Each character has Hit Dice equal to its level, of the class die (PHB class table: d6 "
            "sorcerer or wizard, d8 bard, cleric, druid, monk, rogue or warlock, d10 fighter, paladin "
            "or ranger, d12 barbarian; the artificer's d8 is not in the local 2014 books). The spent "
            "count is an engine-owned sheet resource, "
            "shown with the remaining count after each rest. A manual sheet edit cannot change it."
        ),
        implementation="agent.resolution:hit_dice_status",
        test="tests/test_hit_dice_persistence.py::test_supported_hit_dice_ledger_persists_and_is_protected",
    ),
    Mechanic(
        id="official_conditions",
        title="Official condition names as narrative tags",
        scope=(
            "Only the 15 official condition names are stored; common aliases are normalised. The "
            "stored name is a narrative tag: it has no engine effect on a player character. The "
            "engine applies blinded and marked to foes only, from catalog spells (see condition_effects)."
        ),
        implementation="agent.resolution:normalise_conditions",
        test="tests/agent/test_supported_rules.py::test_supported_conditions_are_tags_only",
    ),
    Mechanic(
        id="campaign_completion",
        title="Campaign completion needs confirmation",
        scope=(
            "A proposed ending is pending until the player confirms it. From combat it is accepted "
            "only after a victory over a boss-flagged enemy."
        ),
        implementation="agent.resolution:decide_completion",
        test="tests/agent/test_supported_rules.py::test_supported_completion_needs_confirmation",
    ),
    Mechanic(
        id="exact_monster_records",
        title="Exact creature records: stat block read by code, not by the encounter builder",
        scope=(
            "A creature identified by its verified record fights with that record's armor class, "
            "hit points, DEX modifier for initiative, saving-throw bonuses (a spell's save uses the "
            "stated bonus), attacks and traits. The encounter builder chooses which exact creatures "
            "take part and never supplies their numbers. The solo budget does not rescale an exact "
            "record: it limits only how many foes take part, and notes a record that exceeds a "
            "per-foe band. A record whose stat block cannot be read exactly blocks the encounter."
        ),
        implementation="agent.monster_stats:parse_monster_stats",
        test="tests/agent/test_monster_stats.py::test_troll_record_survives_every_stage",
    ),
    Mechanic(
        id="monster_attacks",
        title="Monster attacks: listed attacks and multiattack",
        scope=(
            "Each attack listed under Actions is an engine d20 roll plus the record's bonus against "
            "the player's AC; a hit rolls the record's damage dice, and a natural 20 doubles them. A "
            "Multiattack trait whose counts are stated in its text makes one attack roll per listed "
            "attack in a round. Without a readable Multiattack, the first listed attack is made and "
            "the trait is declared. Damage riders, spell attacks, recharge abilities and a "
            "Multiattack's disadvantage on a later attack are declared, not executed."
        ),
        implementation="agent.monster_stats:attack_plan",
        test="tests/agent/test_monster_stats.py::test_multiattack_rolls_one_attack_per_listed_attack",
    ),
    Mechanic(
        id="monster_regeneration",
        title="Monster regeneration with its stated suppression",
        scope=(
            "A creature whose record states regeneration regains the stated HP at the start of its "
            "turn, up to its maximum. When the record names the damage types that suppress it, "
            "regeneration does not happen at that turn if the creature took one of them since its "
            "last turn. A creature reduced to 0 HP is defeated at once: the record's clause about "
            "regenerating from 0 HP is declared, not executed."
        ),
        implementation="agent.monster_stats:regenerate",
        test=(
            "tests/agent/test_monster_stats.py::"
            "test_regeneration_is_suppressed_by_fire_or_acid_taken_since_the_last_turn"
        ),
    ),
    Mechanic(
        id="monster_damage_types",
        title="Resistance, immunity and vulnerability by damage type",
        scope=(
            "Player damage to an exact creature takes the damage type of the engine weapon (standard "
            "weapon table) or of the catalog spell. Resistance halves it, immunity blocks it and "
            "vulnerability doubles it. A resistance written for nonmagical attacks applies to "
            'weapons, not to spells. A "nonmagical" resistance that also excludes silvered or '
            "adamantine weapons is applied as written for the hero's plain weapons, and the exception "
            "is declared. When the damage type is unknown no rule applies, and the round says so."
        ),
        implementation="agent.monster_stats:adjust_monster_damage",
        test=(
            "tests/agent/test_monster_stats.py::"
            "test_resistance_halves_immunity_blocks_and_vulnerability_doubles"
        ),
    ),
    Mechanic(
        id="contested_maneuvers",
        title="Grapple, pin and restrain; shove prone (in combat, contested)",
        scope=(
            "In combat, against one foe, with rope or bare hands. The player's Athletics check is "
            "contested by the foe's Dexterity check (Acrobatics without proficiency); the higher "
            "total wins and a tie fails. Success restrains a grappled or pinned foe: its attacks have "
            "disadvantage, attacks against it have advantage and its Dexterity saves (a spell's, such as "
            "Sacred Flame's) have disadvantage. Success on a shove prone knocks the "
            "foe prone: its attacks have disadvantage, and attacks against it have advantage from "
            "within 5 feet (the melee band) and disadvantage from farther away (PHB, PDF p.293), so a "
            "ranged attack made within 5 feet has advantage that its own disadvantage cancels. The "
            "maneuver replaces that turn's attack. A "
            "restraint ends when the player declares a release, when the foe is defeated, or when the "
            "encounter ends. A prone foe does not stand up, and a restrained foe makes no escape "
            "contest; both are fixed for the rest of the encounter."
        ),
        implementation="agent.combatants:add_effect",
        test="tests/agent/test_supported_rules.py::test_supported_contested_maneuver_applies_effect",
    ),
    Mechanic(
        id="action_economy",
        title="Action, bonus action, Dodge, Dash and Disengage in one player turn",
        scope=(
            "Each player turn has one action and one bonus action. The engine classifies every "
            "declared action from a fixed table: attack, contested maneuver, item use, a catalog "
            "action spell, Dodge, Dash and Disengage use the action; Second Wind and Healing Word use "
            "the bonus action. A second action, or a second bonus action, in the same turn is refused "
            "with no roll and no round. Dodge (PHB, PDF p.193) gives attacks against the hero "
            "disadvantage until the hero's next turn begins. The book's other two Dodge effects are "
            "not applied: the attacker must be seen for the disadvantage, and Dexterity saves get "
            "advantage. No engine effect rolls a save for the hero, and sight is not tracked. Dash "
            "allows a second move in the turn and Disengage stops the opportunity attacks that moves "
            "would provoke (see positioning). The turn's spending is shown in the combat HUD and the "
            "narration context. Help is not modelled."
        ),
        implementation="agent.action_economy:refusal_for",
        test="tests/agent/test_action_economy.py::test_second_action_is_refused_before_any_roll",
    ),
    Mechanic(
        id="positioning",
        title="Coarse positioning: melee or ranged band, and one move per turn (Dash for two)",
        scope=(
            "Each foe stands within 5 feet of the hero (melee) or beyond it (ranged). A foe starts "
            "at range only when every attack its record lists is ranged; otherwise it starts in melee. "
            "The hero moves by declaration ('I close in on the archer', 'I back away'), which is free "
            "and is not an action: one band change per turn at normal speed, and two with the Dash "
            "action. A ranged-only foe at range stays there; a melee-only foe at range closes in on its "
            "turn and then attacks. A melee-only attack or grapple cannot reach a foe at range and is "
            "refused with no roll. A ranged attack made within 5 feet of a foe has disadvantage. The "
            "bands are shown in the combat HUD and the narration context; rows from before this rule "
            "carry no band and are not rewritten. Exact distances are not tracked."
        ),
        implementation="agent.positioning:plan_moves",
        test="tests/agent/test_positioning.py::test_hero_moves_one_band_per_turn_and_dash_allows_two",
    ),
    Mechanic(
        id="opportunity_attacks",
        title="Opportunity attacks (the reaction): leaving a foe's melee reach",
        scope=(
            "When the hero moves from a foe's melee band to range without the Disengage action, that "
            "foe may make one melee attack against the hero with its reaction: the record's first melee "
            "attack, rolled and damaged by the engine. A creature has one reaction per round, which comes "
            "back at the start of its own turn. A foe with no melee attack makes none. No other reaction "
            "is executed."
        ),
        implementation="agent.graph_combat:combat_round_node",
        test="tests/agent/test_positioning.py::test_leaving_melee_provokes_one_opportunity_attack",
    ),
    Mechanic(
        id="captive_lifecycle",
        title="Captives after a fight: release, declared kill, or a ruling",
        scope=(
            "A living captive won by a nonlethal victory can be released by the player's declared "
            "release: it becomes released, is neutral and is never an enemy again. It can be killed "
            "only by an explicit lethal declaration in the player's own words; the captive then "
            "becomes dead. Attacking a captive without a declared kill starts no combat and makes no "
            "roll: the engine asks whether the player releases, questions or kills them. Questioning "
            "a captive is narrated as before. Each outcome is recorded in the session story ledger "
            "with its combat identity. No dice are rolled for these actions."
        ),
        implementation="agent.captives:captive_turn",
        test=(
            "tests/agent/test_captive_lifecycle.py::"
            "test_release_turn_returns_engine_outcome_without_dice_or_combat"
        ),
    ),
)

UNSUPPORTED_MECHANICS: tuple[UnsupportedMechanic, ...] = (
    UnsupportedMechanic(
        id="unsupported_maneuvers",
        title="Shove away, shove back, grappling several foes, escape contests and other maneuvers",
        boundary=(
            "Any check rolled is an ordinary ability check. Its success records no restraint, "
            "prone state, pin or position, and no maneuver is applied outside a combat round or when "
            "its contest is lost."
        ),
        owner="EDG-044",
    ),
    UnsupportedMechanic(
        id="declared_spells",
        title="Other declared spells and spell effects",
        boundary=(
            "A declared spell outside the supported list has no engine effect: no damage, healing, "
            "saving throw, condition, slot, concentration or duration. It is not resolved as a "
            "weapon or class attack."
        ),
        owner="EDG-043",
    ),
    UnsupportedMechanic(
        id="other_conditions",
        title=(
            "Mechanical effects of other conditions (frightened, stunned, charmed and the rest; poisoned "
            "and paralyzed beyond the effects named in the supported list)"
        ),
        boundary=(
            "Such a condition has no engine effect: it does not penalise rolls, speed, actions or senses, "
            "and no foe is frightened, stunned or charmed by the engine. A poisoned creature's ability "
            "checks are not penalised, and a frightened creature's movement and line-of-sight limits are "
            "not tracked."
        ),
        owner="EDG-043",
    ),
    UnsupportedMechanic(
        id="positioning_cover",
        title="Cover, high ground, flanking, difficult terrain and exact distances",
        boundary=(
            "No cover, elevation, flanking or difficult-terrain modifier is applied. Only the melee and "
            "ranged bands are recorded: no exact distance in feet is tracked and no movement speed is spent."
        ),
        owner="EDG-044",
    ),
    UnsupportedMechanic(
        id="action_economy_remainder",
        title="Help, reactions other than opportunity attacks, and other bonus actions",
        boundary=(
            "Each turn allows one action and one bonus action, and only Second Wind and Healing Word "
            "use the bonus action. Help, any reaction other than an opportunity attack, and any other "
            "bonus action have no engine effect."
        ),
        owner="EDG-044",
    ),
    UnsupportedMechanic(
        id="class_features",
        title="Class features other than Second Wind, Action Surge, Sneak Attack and Rage",
        boundary=(
            "Divine Smite, Lay on Hands, Bardic Inspiration, Channel Divinity, Wild Shape, sorcery "
            "points, ki points, Flurry of Blows, Stunning Strike, Superiority Dice and Martial Arts have "
            "no engine effect, and neither do Rage's damage resistance and Strength advantage. Their "
            "effects, uses and recharge are not applied."
        ),
        owner="unassigned",
    ),
    UnsupportedMechanic(
        id="monster_traits",
        title=(
            "Other monster traits: speed and movement, legendary, lair and reaction actions, "
            "spellcasting, breath and other save-based recharge abilities, and unlisted traits"
        ),
        boundary=(
            "Each such trait is declared, not executed, in the combat start message. It has no engine "
            "effect: a monster never moves, casts, uses a breath or area recharge ability, or takes a "
            "legendary, lair or reaction action, and its passive traits are not applied. An attack-style "
            "Recharge attack is executed as stated in the supported list."
        ),
        owner="EDG-043",
    ),
    UnsupportedMechanic(
        id="exploding_dice",
        title="Exploding dice",
        boundary=(
            "Not implemented. Exploding dice are not a standard 5e requirement and are not part "
            "of this contract."
        ),
        owner="unassigned",
    ),
)

SOLO_ADAPTATIONS: tuple[str, ...] = (
    (
        "Defeat is subdued, not death: when the player reaches 0 HP in combat, the engine sets "
        "HP to 1 and the story continues, often with the hero captured or at the enemy's mercy."
    ),
    (
        "Encounters are fitted to a solo hero by an engine-enforced budget (agent.encounter_budget) "
        "derived from hero level, applied when a new roster is built. At level 1 it allows at most "
        "2 foes, a boss of 18-30 HP, other foes of 5-11 HP, and a total enemy HP of 41. Higher "
        "levels scale those numbers up and allow more foes. A foe without an exact record is "
        "clamped into range or left out, never renamed. An exact creature is never rescaled: its "
        "record's numbers apply, the budget limits only how many foes take part, and a record that "
        "exceeds a per-foe band is noted as an unscaled exact creature. The combat start notes any "
        "adaptation, and the combat state records it."
    ),
)


# --------------------------------------------------------------------------------------------
# Unsupported declared actions (runtime classification)
# --------------------------------------------------------------------------------------------

SPELL = "spell"
FEATURE = "feature"
MANEUVER = "maneuver"
CONDITION = "condition"
ADVANTAGE = "advantage"
ACTION_ECONOMY = "action_economy"
POSITIONING = "positioning"

_KIND_ORDER = (SPELL, FEATURE, MANEUVER, CONDITION, ADVANTAGE, ACTION_ECONOMY, POSITIONING)

_KIND_MECHANIC = {
    SPELL: "declared_spells",
    FEATURE: "class_features",
    MANEUVER: "unsupported_maneuvers",
    CONDITION: "other_conditions",
    ADVANTAGE: "advantage_disadvantage",
    ACTION_ECONOMY: "action_economy_remainder",
    POSITIONING: "positioning_cover",
}

_RULINGS = {
    SPELL: (
        "Narrative ruling: the declared spell has no engine mechanics here. No spell effect, "
        "damage, healing, saving throw, condition, slot or concentration is applied, and the "
        "spell is not resolved as a weapon or class attack."
    ),
    FEATURE: (
        "Narrative ruling: the named class feature has no engine mechanics here. Its effects, "
        "uses and recharge are not applied."
    ),
    MANEUVER: (
        "Narrative ruling: this maneuver has no engine effect this turn. Any check rolled is an "
        "ordinary ability check, and the engine records no effect from it. No position is tracked."
    ),
    CONDITION: (
        "Narrative ruling: the condition has no engine effect. No penalty, speed change, "
        "incapacitation or saving-throw change is applied."
    ),
    ADVANTAGE: (
        "Narrative ruling: advantage was not applied: no supported source applies here, so the "
        "roll uses the plain d20 and the sheet modifier."
    ),
    ACTION_ECONOMY: (
        "Narrative ruling: this action is not modelled. Help, reactions other than opportunity attacks "
        "and other bonus actions have no engine effect."
    ),
    POSITIONING: (
        "Narrative ruling: cover, high ground, flanking and difficult terrain are not modelled. They "
        "give no modifier, and no position beyond the melee and ranged bands is recorded."
    ),
}

_SPELL_NAMES = (
    "magic missile|fire bolt|eldritch blast|cure wounds|healing word|hold person|thunderwave|"
    "burning hands|mage armor|misty step|guiding bolt|sacred flame|detect magic|faerie fire|"
    "feather fall|fireball|lightning bolt|color spray|witch bolt|ray of frost|chill touch|"
    "shocking grasp|acid splash|vicious mockery|dissonant whispers|spiritual weapon|hunter's mark|"
    "blindness/deafness"
)
# Features the engine does not execute. Action Surge, Sneak Attack and Rage are implemented
# (agent.class_features) and are ruled on by their own helper, not by these names.
_FEATURE_NAMES = (
    "divine smite|lay on hands|bardic inspiration|channel divinity|wild shape|sorcery points?|"
    "ki points?|flurry of blows|stunning strike|superiority dice|martial arts"
)
# Condition claims other than blinded. Blinded is engine-applied from Blindness/Deafness and is
# exempt only when the engine actually applied it this turn (see `_claim_pattern`).
# Condition words, by condition. A condition the engine really recorded this turn is exempt from its
# claim words (see `_condition_claim`), so the narration of a real effect is never stripped.
_CLAIM_CONDITION_WORDS = {
    "blinded": "blinded",
    "stunned": "stunned",
    "poisoned": "poisoned",
    "paralyzed": "paralyzed",
    "petrified": "petrified",
    "charmed": "charmed",
    "frightened": "frightened",
    "deafened": "deafened",
    "incapacitated": "incapacitated",
}
_TEXT_CONDITION_WORDS = {
    "blinded": "blind|blinds|blinded|blinding",
    "stunned": "stun|stuns|stunned|stunning",
    "poisoned": "poison|poisons|poisoned|poisoning",
    "paralyzed": "paralyze|paralyzes|paralyzed|paralyse|paralysed",
    "petrified": "petrify|petrifies|petrified",
    "charmed": "charm|charms|charmed",
    "frightened": "frighten|frightens|frightened",
    "deafened": "deafen|deafened",
    "incapacitated": "incapacitate|incapacitated",
}
# Conditions the engine records on a creature this module reads (see `_claim_pattern`).
_RECORDED_CONDITIONS = frozenset({"blinded", "paralyzed", "poisoned"})


def _condition_claim(exclude: frozenset[str] = frozenset()) -> str:
    words = "|".join(w for k, w in _CLAIM_CONDITION_WORDS.items() if k not in exclude)
    return rf"\b(?:{words})\b"


def _text_condition_pattern(exclude: frozenset[str] = frozenset()) -> re.Pattern[str]:
    words = "|".join(ws for k, ws in _TEXT_CONDITION_WORDS.items() if k not in exclude)
    return re.compile(rf"\b(?:{words})\b", re.IGNORECASE)


_BONUS_ACTION_CLAIM = r"bonus[\s-]+action"
_DODGE_CLAIM = r"dodg\w*"
# Each economy phrase, keyed by the typed effect the engine records when it executes it. Help has no
# effect in this engine, so its phrase is always a claim.
_ECONOMY_PARTS = (
    ("dodge", _DODGE_CLAIM),
    ("bonus_action", _BONUS_ACTION_CLAIM),
    ("dash", r"dash\s+action"),
    ("disengage", r"disengag\w*"),
    ("opportunity_attack", r"opportunity\s+attacks?"),
    ("help", r"help\s+action"),
)
_ACTION_ECONOMY_CLAIM = r"\b(?:" + "|".join(p for _, p in _ECONOMY_PARTS) + r")\b"
_UNSUPPORTED_FEATURE = re.compile(r"\b(?:" + _FEATURE_NAMES + r")\b", re.IGNORECASE)
# A shove or pin aimed at an inanimate object ("shove the table against the door") is not a combat
# maneuver, so the object is excluded by lookahead.
_INANIMATE = (
    r"table|chair|crate|box|barrel|cart|door|desk|bench|stool|wardrobe|cabinet|rock|boulder|stone|"
    r"bag|bucket|chest|tray|bookcase|shelf|bed|sofa|notice|note|map|poster|sign|letter|paper|card|"
    r"flyer|badge|cloak|scroll"
)
_NOT_OBJECT = (
    r"(?!\s+(?:the\s+|a\s+|an\s+|his\s+|her\s+|its\s+|their\s+|my\s+|your\s+)?(?:"
    + _INANIMATE
    + r")\b)"
)
# Idioms that name no mechanic ("take advantage of", "cast my net", "rage against the machine").
# They are blanked out of the declared text before the word patterns read it. Casting itself is still
# read from the original text by `cast_request`.
_IDIOM = re.compile(
    r"\b(?:take|takes|took|taken|taking|prendo|prendere|prende|aprovecho|aprovechar|sacar|saco|"
    r"tomar|tomo)\s+(?:la\s+|il\s+|de\s+la\s+|de\s+)?(?:advantage|vantaggio|ventaja)\b"
    r"(?:\s+(?:of|dal|de)\b)?"
    r"|\bcast\s+(?:my|your|his|her|their|a)\s+(?:net|line|glance|look|vote|lot)\b"
    r"|\bcast\s+(?:off|aside|about|around|out)\b"
    r"|\bcast\s+(?:an?|one|the|my|your|his|her|their)\s+(?:eyes?|look|glance|gaze)\b"
    r"|\brage\s+against\b"
    r"|\bfrighten\w*\s+(?:\w+\s+){0,2}into\b"
    r"|\btrip(?:s|ped|ping)?\s+over\b",
    re.IGNORECASE,
)
# A parser-named action that is a 5e action economy slot the engine does not model (combat only).
_PARSED_ECONOMY = re.compile(
    r"dash|disengage|\bhelp\b|ready\s+an?\s+action|reaction|bonus|opportunity|extra attack|"
    r"second attack|off-hand",
    re.IGNORECASE,
)

_CLAIM_WORDS = {
    SPELL: (
        r"\b\d+d\d+\b|\bspell\s+slots?\b|\bslots?\b|\bconcentrat\w*|\b(?:"
        + _SPELL_NAMES
        + r")\b|\bspell\s+(?:hits|strikes|lands|takes hold|binds|damages|heals|restores)\b"
    ),
    FEATURE: r"\b(?:" + _FEATURE_NAMES + r")\b",
    MANEUVER: (
        r"\b(?:pinned|pins?\s+(?:him|her|it|them|the\s+\w+)|pinning|restrain\w*|grappl\w*|"
        r"shov\w*"
        + _NOT_OBJECT
        + r"|prone|held\s+(?:fast|down|in\s+place|still)|knocked\s+(?:down|prone|over)|"
        r"(?:can|could)(?:n't|\s+not)\s+(?:move|escape|get\s+up|stand))\b"
    ),
    CONDITION: _condition_claim(),
    ADVANTAGE: r"\b(?:advantage|disadvantage|with an edge)\b",
    ACTION_ECONOMY: _ACTION_ECONOMY_CLAIM,
    POSITIONING: r"\b(?:in\s+cover|take\s+cover|behind\s+cover|high\s+ground|flank\w*)\b",
}

# Spell names the engine does not resolve. A catalog spell with no such name is executed, not declared.
_UNCOVERED_SPELL = re.compile(
    r"\b(?:"
    + "|".join(
        name
        for name in _SPELL_NAMES.split("|")
        if name not in {spell.name.lower() for spell in CATALOG}
    )
    + r")\b",
    re.IGNORECASE,
)

# Condition words in declared text. The blind words are read separately so that a blinded condition the
# engine applied is not stripped, while every other condition word keeps its ruling.

_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        SPELL,
        re.compile(
            r"\b(?:spell|spells|spellcasting|spellcast|cantrips?|rituals?|incantation|"
            r"incantesimo|incantesimi|lancio\s+di\s+(?:un\s+|una\s+)?(?:incantesimo|magia)|"
            r"lanzo\s+(?:un\s+|una\s+)?(?:hechizo|conjuro)|lanzar\s+(?:un\s+|una\s+)?(?:hechizo|conjuro)|"
            r"hechizo|conjuro|incantato|incantata)\b"
            r"|\bcast(?:s|ing)?\b(?!\s+(?:off|aside|doubt|suspicion|about|around|out|your|a\s+"
            r"(?:glance|look|light|shadow|net|line|vote|lot)|my\s+(?:eyes|gaze|net|line))\b)"
            r"|\b(?:" + _SPELL_NAMES + r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        MANEUVER,
        re.compile(
            r"\b(?:(?:pin|pins|pinned|pinning|shove|shoves|shoved|shoving)\b" + _NOT_OBJECT + r"|"
            r"restrain|restrains|"
            r"restrained|restraining|grapple|grapples|grappled|grappling|tackle|tackles|tackled|"
            r"trip|trips|tripped|tripping|knock(?:s|ed|ing)?\s+(?:\w+\s+)?prone|"
            r"hold\s+(?:him|her|it|them)\s+(?:down|in\s+place|still|fast))\b",
            re.IGNORECASE,
        ),
    ),
    (
        CONDITION,
        _text_condition_pattern(),
    ),
    (
        ADVANTAGE,
        re.compile(
            r"(?<!take\s)(?<!takes\s)(?<!taking\s)(?<!took\s)(?<!taken\s)"
            r"\b(?:advantage|disadvantage|with an edge|vantaggio|ventaja)\b(?!\s+of\b)",
            re.IGNORECASE,
        ),
    ),
    (
        ACTION_ECONOMY,
        re.compile(
            # Dodge is a supported action (action_economy), so its phrase is not listed here.
            r"\b(?:(?:dash|help)\s+action|disengag\w*|bonus[\s-]+action|opportunity\s+attacks?|"
            r"ready\s+an?\s+action|extra\s+attack|second\s+attack|off-hand\s+attack|"
            r"as\s+a\s+reaction|my\s+reaction)\b",
            re.IGNORECASE,
        ),
    ),
    (
        POSITIONING,
        re.compile(
            r"\b(?:flank\w*|take\s+cover|in\s+cover|behind\s+cover|high\s+ground|"
            r"a\s+cubierto|copertura|al\s+riparo)\b",
            re.IGNORECASE,
        ),
    ),
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class UnsupportedAction:
    kind: str
    phrase: str
    note: str | None = None  # a specific ruling (a refused catalog spell), else the kind's ruling

    @property
    def ruling(self) -> str:
        return self.note or _RULINGS[self.kind]

    @property
    def mechanic_id(self) -> str:
        return _KIND_MECHANIC[self.kind]


def _parsed_spell(parsed: Any) -> bool:
    if parsed is None:
        return False
    check = getattr(parsed, "check", None)
    norm = normalise_check(check) if check else None
    if norm is not None and norm[1] == "spell_attack":
        return True
    entities = getattr(parsed, "entities", None)
    return isinstance(entities, dict) and bool(entities.get("spell"))


def _unresolved_spell_note(parsed: Any) -> str | None:
    """The parser named a catalog spell but the words do not: the engine resolves a catalog spell only
    when its English name is in the declaration, so the spell is not resolved."""
    entities = getattr(parsed, "entities", None)
    name = str(entities.get("spell") or "").strip().lower() if isinstance(entities, dict) else ""
    if name and name in {spell.name.lower() for spell in CATALOG}:
        return (
            f"Narrative ruling: {name} is not resolved: the engine resolves a catalog spell only "
            "when its English name is in the declaration, so no spell effect is applied."
        )
    return None


def _parsed_feature(parsed: Any) -> str | None:
    """A feature the parser named that is not Second Wind or an implemented class feature."""
    entities = getattr(parsed, "entities", None)
    if not isinstance(entities, dict):
        return None
    feature = str(entities.get("feature") or "").strip()
    if not feature or "second wind" in feature.lower():
        return None
    if requested_features(None, feature):
        return None  # an implemented class feature: handled by its own helper
    return feature[:60]


def parsed_feature_name(parsed: Any) -> str | None:
    entities = getattr(parsed, "entities", None)
    name = str(entities.get("feature") or "").strip() if isinstance(entities, dict) else ""
    return name or None


def _engine_applied_maneuver(text: str, applied: EngineApplied) -> bool:
    """True when the declared maneuver is a supported one whose effect the engine applied."""
    maneuver = classify_combat_maneuver(text)
    return maneuver is not None and maneuver.kind in applied.effects


# Spell conditions the executed spell records on a foe; when the engine recorded none, the narrated
# claim is ruled on (agent.conditions). A marked foe has no claim word in the narration.
_CONDITION_RECORDED = frozenset({"blinded", "paralyzed"})

ENGINE_ROLL_MODE_KEY = "engine_roll_mode"


@dataclass(frozen=True)
class EngineApplied:
    """What the engine really executed this turn. The boundary reads this, never LLM text.

    `roll_mode` is the advantage or disadvantage the engine applied ("normal" when none).
    `effects` are the typed effect ids the combat round recorded this turn. `executed_spell` is
    True when a catalog spell is executed (no refusal). See the module docstring for how to add one.
    """

    roll_mode: str = "normal"
    effects: tuple[str, ...] = ()
    executed_spell: bool = False


def engine_applied_for_state(state: dict[str, Any] | None) -> EngineApplied:
    """The engine-applied facts recorded in this turn's state (missing or unknown: none).

    `engine_effects` is recorded by the combat round; the executed spell is derived from the
    player's cast request, which the engine executes unless the character cannot cast it.
    """
    state = state or {}
    mode = state.get(ENGINE_ROLL_MODE_KEY)
    request = cast_request(
        state.get("player_input"), state.get("parsed_input"), state.get("character")
    )
    return EngineApplied(
        roll_mode=mode if mode in ("advantage", "disadvantage") else "normal",
        effects=tuple(str(kind) for kind in (state.get("engine_effects") or ())),
        executed_spell=request is not None and request[2] is None,
    )


def classify_unsupported_actions(
    player_input: str | None,
    parsed: Any = None,
    character: dict | None = None,
    *,
    applied: EngineApplied = EngineApplied(),
) -> tuple[UnsupportedAction, ...]:
    """Deterministic classification of the declared action against the unsupported list.

    Returns at most one entry per kind, in a stable order. Text is matched in English only.
    A declared catalog spell that the character can cast is executed by the engine and is not
    listed; a catalog spell that is refused (no access, no slot) is listed with that refusal as
    its ruling. Any other declared spell, and a spell attack proposed by the parser, is listed.
    When the engine applied advantage or disadvantage this turn (`applied.roll_mode`), an advantage
    wording describes a real effect and is not an unsupported action. `applied.effects` are the
    typed effects the engine applied this turn (EDG-021): a supported maneuver that really applied
    one is not unsupported, so its narration is not stripped.
    """
    text = str(player_input or "")
    scan = _IDIOM.sub(" ", text)
    effects = applied.effects
    found: dict[str, UnsupportedAction] = {}
    request = cast_request(text, parsed, character)
    if request is not None:
        spell, _plan, refusal = request
        if refusal:
            found[SPELL] = UnsupportedAction(
                SPELL, spell.name.lower(), f"Narrative ruling: {refusal}"
            )
        elif spell.condition in _CONDITION_RECORDED and spell.condition not in effects:
            # The executed spell recorded no condition (the save succeeded, or no foe was in the
            # fight): a narrated blindness would be a claim without an engine effect, so it is ruled on.
            found[CONDITION] = UnsupportedAction(
                CONDITION,
                spell.condition,
                f"{spell.name} recorded no {spell.condition} condition this turn: the target's save "
                "succeeded or no foe was in the fight, so none is applied.",
            )
    elif _parsed_spell(parsed):
        found[SPELL] = UnsupportedAction(SPELL, "spell (parsed)", _unresolved_spell_note(parsed))
    # Implemented class features (EDG-145) the engine executed this turn are not declared. Any
    # other declared one (refused, or not applied this turn) gets its own ruling.
    class_features = requested_features(scan, parsed_feature_name(parsed))
    phrases = [f.replace("_", " ") for f in class_features if f not in effects]
    rulings = [feature_ruling(f, character) for f in class_features if f not in effects]
    unsupported = _UNSUPPORTED_FEATURE.search(scan)
    feature = unsupported.group(0) if unsupported else _parsed_feature(parsed)
    if feature:
        phrases.append(feature.lower()[:60])
        rulings.append(_RULINGS[FEATURE])
    if phrases:
        found[FEATURE] = UnsupportedAction(
            FEATURE, "; ".join(phrases)[:60], " ".join(dict.fromkeys(rulings))
        )
    for kind, pattern in _TEXT_PATTERNS:
        if (
            kind == SPELL
            and request is not None
            and request[2] is None
            and not _UNCOVERED_SPELL.search(text)
        ):
            continue  # the executed catalog spell; its casting words are not a declared spell
        if kind == ADVANTAGE and applied.roll_mode != "normal":
            continue
        recorded = _RECORDED_CONDITIONS & set(effects)
        if kind == CONDITION and recorded:
            pattern = _text_condition_pattern(
                frozenset(recorded)
            )  # a real effect: its word is not a claim
        if kind in found:
            continue
        match = next(
            (
                m
                for m in pattern.finditer(scan)
                if not _executed_economy_phrase(kind, m.group(0), applied, text)
            ),
            None,
        )
        if not match:
            continue
        if kind == MANEUVER and _engine_applied_maneuver(text, applied):
            continue
        found[kind] = UnsupportedAction(kind, match.group(0).lower()[:60])
    economy = _parsed_economy(parsed, text, applied)
    if ACTION_ECONOMY not in found and economy:
        found[ACTION_ECONOMY] = UnsupportedAction(ACTION_ECONOMY, economy)
    return tuple(found[kind] for kind in _KIND_ORDER if kind in found)


def _parsed_economy(parsed: Any, text: str, applied: EngineApplied) -> str | None:
    """A combat action the parser named (Dash, Disengage...) that the engine does not model.

    Read only in combat (the economy slots are combat rules), and never for an action the engine
    executed this turn (Dodge, or a Second Wind / Healing Word bonus action).
    """
    if getattr(parsed, "intent", None) != "combat":
        return None
    entities = getattr(parsed, "entities", None)
    action = str(entities.get("action") or "").strip() if isinstance(entities, dict) else ""
    if not action or re.search(r"dodg", action, re.IGNORECASE):
        return None
    if not _PARSED_ECONOMY.search(action) or _executed_economy_phrase(
        ACTION_ECONOMY, action, applied, text
    ):
        return None
    return action.lower()[:60]


def _executed_economy_phrase(
    kind: str, phrase: str, applied: EngineApplied, text: str = ""
) -> bool:
    """True when the phrase names an action economy slot the engine executed this turn.

    A Dodge (`dodge` effect), Dash, Disengage and an opportunity attack are supported, so their
    wording describes a real effect and is neither declared nor stripped. A declared "bonus action"
    is that executed one only when the player also names Second Wind or Healing Word; "use my bonus
    action to hide" stays unsupported beside a Second Wind.
    """
    if kind != ACTION_ECONOMY:
        return False
    word = phrase.lower()
    if "dodge" in applied.effects and word.startswith("dodg"):
        return True
    if "bonus_action" in applied.effects and word.startswith("bonus"):
        return bool(_EXECUTED_BONUS_SPELL_OR_FEATURE.search(text))
    if "dash" in applied.effects and word.startswith("dash"):
        return True
    if "disengage" in applied.effects and word.startswith("disengag"):
        return True
    return "opportunity_attack" in applied.effects and word.startswith("opportunity")


_EXECUTED_BONUS_SPELL_OR_FEATURE = re.compile(r"second\s+wind|healing\s+word", re.IGNORECASE)


def actions_for_state(state: dict[str, Any]) -> tuple[UnsupportedAction, ...]:
    return classify_unsupported_actions(
        state.get("player_input"),
        state.get("parsed_input"),
        state.get("character"),
        applied=engine_applied_for_state(state),
    )


def has_declared_spell(actions: tuple[UnsupportedAction, ...]) -> bool:
    return any(action.kind == SPELL for action in actions)


def ruling_lines(actions: tuple[UnsupportedAction, ...]) -> list[str]:
    return list(dict.fromkeys(action.ruling for action in actions))


def scope_notice() -> str:
    """Prompt text describing the engine subset, generated from the contract."""
    supported = "; ".join(m.title for m in SUPPORTED_MECHANICS)
    unsupported = "; ".join(m.title for m in UNSUPPORTED_MECHANICS if m.id != "exploding_dice")
    return (
        "Simplified rules scope (engine-enforced; do not claim other mechanics): "
        f"executed by the engine: {supported}. "
        f"Not supported (declared actions get a narrative ruling and no mechanical effect): "
        f"{unsupported}. Defeat for the solo hero is subdued at 1 HP, not death."
    )


def boundary_prompt_block(actions: tuple[UnsupportedAction, ...]) -> str | None:
    if not actions:
        return None
    lines = ["Supported-action boundary for this turn (engine-enforced; do not contradict):"]
    lines += [f"- Declared '{a.phrase}': {a.ruling}" for a in actions]
    lines.append(
        "Describe these attempts only as narrative events. Never state damage, healing, "
        "conditions, positions, advantage, slots, durations or dice that the engine did not produce."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Post-validation
# --------------------------------------------------------------------------------------------


def _claim_words(applied: EngineApplied) -> dict[str, str]:
    """The claim vocabulary, minus the action economy phrases the engine executed this turn."""
    words = dict(_CLAIM_WORDS)
    remaining = [claim for effect, claim in _ECONOMY_PARTS if effect not in applied.effects]
    if len(remaining) < len(_ECONOMY_PARTS):
        # Only the claims the engine did not execute remain (Help always does).
        words[ACTION_ECONOMY] = r"\b(?:" + "|".join(remaining) + r")\b"
    return words


def _claim_pattern(
    actions: tuple[UnsupportedAction, ...], applied: EngineApplied = EngineApplied()
) -> re.Pattern[str] | None:
    """Claims of every unsupported mechanic, once any unsupported action is declared.

    The union is deliberate: a fake or confused provider may describe a different unsupported
    effect than the one the player declared (a spell turn narrating a pin, say), and neither the
    narrator nor a flag can be trusted to name its own mechanic. Three exceptions describe engine
    facts: a catalog spell the engine executed (its name, dice and slots), an advantage or
    disadvantage the engine applied this turn, a restraint or prone effect really applied, and an
    implemented class feature the engine executed (`applied.effects`, EDG-145); those claims are
    not applied to that turn's text.
    """
    if not actions:
        return None
    maneuver_applied = bool({"restrained", "prone"} & set(applied.effects))
    patterns = []
    for kind, p in _claim_words(applied).items():
        if applied.executed_spell and kind == SPELL:
            continue
        if kind == ADVANTAGE and applied.roll_mode != "normal":
            continue
        if kind == MANEUVER and maneuver_applied:
            continue
        recorded = _RECORDED_CONDITIONS & set(applied.effects)
        if kind == CONDITION and recorded:
            p = _condition_claim(frozenset(recorded))  # the engine's real condition is not a claim
        patterns.append(p)
    # An implemented class feature is a claim only while the engine has not executed it this turn.
    executed = set(applied.effects) & FEATURE_EFFECTS
    patterns += [NARRATION_CLAIMS[f] for f in IMPLEMENTED_FEATURES if f not in executed]
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def apply_adjudication_boundary(
    adj: Any,
    actions: tuple[UnsupportedAction, ...],
    *,
    applied: EngineApplied = EngineApplied(),
) -> None:
    """Remove LLM-proposed effects that claim an unsupported mechanic. Mutates `adj`.

    Conditions are never stored for an unsupported turn. Damage is removed for spell, feature and
    maneuver turns because the engine has no verified effect for them. Flags whose key or value
    claims an unsupported effect are dropped. The ruling is stated in the mechanical summary.
    `applied` holds the facts the engine executed this turn (see `_claim_pattern`).
    """
    if not actions:
        return
    kinds = {a.kind for a in actions}
    adj.conditions = []
    cu = getattr(adj, "character_update", None)
    if cu is not None:
        cu.add_conditions = []
        cu.remove_conditions = []
        if kinds & {SPELL, FEATURE, MANEUVER}:
            cu.damage_dice = None
    if kinds & {SPELL, FEATURE, MANEUVER}:
        adj.damage = None
    claim = _claim_pattern(actions, applied)
    if claim is not None:
        adj.flags_set = [
            fu
            for fu in adj.flags_set
            if not claim.search(f"{str(fu.key).replace('_', ' ')} {fu.value}")
        ]
    rulings = " ".join(ruling_lines(actions))
    summary = _strip_claim_sentences(adj.mechanical_summary, claim, keep=ENGINE_ROLL_LINE)
    adj.mechanical_summary = f"{summary} {rulings}".strip()


# Engine roll lines are engine facts and a summary keeps them whatever they say. They are recognised by
# the engine's own formats (`resolution.Roll.describe`, the contested-roll line, the attack line), never by
# dice notation alone: "hits the goblin for 3d4" is model prose and is still a claim.
ENGINE_ROLL_LINE = re.compile(
    r"\((?:d20 rolled|advantage:|disadvantage:)|\bcontest:|\bvs\s+(?:AC|DC)\s+\d+"
    r"|->\s*(?:success|failure|critical hit|automatic miss)"
    r"|\b\d+d\d+(?:[+-]\d+)?\s*=\s*\d+\s+vs\s+(?:AC|DC)\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(?:no|not|nothing|never|without|fails?|failed|doesn't|does\s+not|didn't|did\s+not)\b|n't\b",
    re.IGNORECASE,
)
_NEGATABLE = re.compile(
    r"(?:"
    + _SPELL_NAMES
    + r")|spell\s+(?:hits|strikes|lands|takes\s+hold|binds|damages|heals|restores)",
    re.IGNORECASE,
)


def _claims_effect(sentence: str, claim: re.Pattern[str] | None) -> bool:
    """True when the sentence asserts an unsupported effect. A negated spell name or a negated
    "spell takes hold" ("no spell takes hold") states the ruling, so it is not a claim."""
    if claim is None:
        return False
    hits = [m.group(0) for m in claim.finditer(sentence)]
    if not hits:
        return False
    return not (_NEGATION.search(sentence) and all(_NEGATABLE.fullmatch(h) for h in hits))


def _strip_claim_sentences(
    text: str, claim: re.Pattern[str] | None, keep: re.Pattern[str] | None = None
) -> str:
    """Keep line structure; drop each sentence that claims an unsupported effect (`keep` exempts)."""
    kept_lines: list[str] = []
    for line in str(text or "").splitlines():
        sentences = [s for s in _SENTENCE_END.split(line.strip()) if s.strip()]
        survivors = [
            s
            for s in sentences
            if not _claims_effect(s, claim) or (keep is not None and keep.search(s))
        ]
        if survivors:
            kept_lines.append(" ".join(survivors))
    return "\n\n".join(kept_lines).strip()


def apply_narration_boundary(
    narration: str,
    actions: tuple[UnsupportedAction, ...],
    *,
    applied: EngineApplied = EngineApplied(),
) -> str:
    """Drop narration sentences that claim an unsupported effect and state the ruling.

    Line structure is kept. When any unsupported action was declared, the ruling is appended, so
    the player always sees that the declared effect is narrative only.
    """
    if not actions:
        return narration
    body = _strip_claim_sentences(narration, _claim_pattern(actions, applied))
    rulings = " ".join(ruling_lines(actions))
    if not body:
        return rulings
    return f"{body}\n\n*{rulings}*"


# --------------------------------------------------------------------------------------------
# Public payload and contract self-check
# --------------------------------------------------------------------------------------------


def contract_payload() -> dict[str, Any]:
    """Read-only description for players and the API. Internal references are omitted."""
    return {
        "contract_id": CONTRACT_ID,
        "version": CONTRACT_VERSION,
        "edition_basis": EDITION_BASIS,
        "summary": scope_notice(),
        "engine_executed": [
            {"id": m.id, "title": m.title, "scope": m.scope} for m in SUPPORTED_MECHANICS
        ],
        "not_supported": [
            {"id": m.id, "title": m.title, "boundary": m.boundary} for m in UNSUPPORTED_MECHANICS
        ],
        "solo_adaptations": list(SOLO_ADAPTATIONS),
        "declaration_language": DECLARATION_LANGUAGE,
        "runtime_boundary": (
            "A declared action that falls in the not-supported list receives a narrative ruling. "
            "Its narration cannot state an engine effect, and no spell, maneuver, condition or "
            "advantage is applied. A supported combat maneuver is applied only when the engine "
            "rolls its contest and records the effect."
        ),
    }


def verify_references(root: Path) -> list[str]:
    """Return human-readable problems with the contract's code and test references."""
    problems: list[str] = []
    for mechanic in SUPPORTED_MECHANICS:
        module_name, _, attr = mechanic.implementation.partition(":")
        try:
            module = importlib.import_module(module_name)
            if not hasattr(module, attr):
                problems.append(f"{mechanic.id}: {mechanic.implementation} missing")
        except ImportError as exc:  # pragma: no cover - reported to the caller
            problems.append(f"{mechanic.id}: cannot import {module_name} ({exc})")
        test_path, _, test_name = mechanic.test.partition("::")
        source = root / test_path
        if not source.is_file():
            problems.append(f"{mechanic.id}: test file {test_path} missing")
            continue
        defined = {
            node.name
            for node in ast.parse(source.read_text(encoding="utf-8")).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if test_name not in defined:
            problems.append(f"{mechanic.id}: {test_name} not defined in {test_path}")
    return problems
