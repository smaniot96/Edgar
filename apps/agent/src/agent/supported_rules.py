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

Engine-owned dice and numbers are unchanged: this module never rolls and never sets a number.
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.combatants import classify_combat_maneuver
from agent.resolution import normalise_check
from agent.spells import CATALOG, cast_request

CONTRACT_ID = "edgar.supported-5e-subset"
CONTRACT_VERSION = 1

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
            "15 (medium, default), 20 (hard) or 25 (very hard). Advantage and disadvantage apply "
            "only from engine-owned sources (see advantage_disadvantage)."
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
            "advantage or disadvantage on attacks (see contested_maneuvers). A player's or the model's claim of "
            "advantage is never a source and is not applied."
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
            "unrecognised attacks keep it."
        ),
        implementation="agent.resolution:resolve_attack",
        test="tests/agent/test_supported_rules.py::test_supported_weapon_attack_crit_and_fumble",
    ),
    Mechanic(
        id="armor_class",
        title="Armor class from armor, DEX and shield",
        scope=(
            "AC from the armor table, DEX modifier as capped by the armor type, plus 2 for a "
            "shield; unarmored characters use 10 plus DEX. Armor is matched by name."
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
            "Fighter only, one use. The player's action spends it, in or out of combat: the engine "
            "rolls 1d10 plus fighter level and caps the result at max HP. A request with no use left "
            "is refused without a roll. Uses are stored on the character sheet, so they carry over "
            "between encounters, and a short or long rest restores the use. There is no automatic "
            "use at 0 HP. Any action economy (bonus action) is not tracked. 2024 campaigns follow "
            "this same single-use rule; the 2024 class table (more uses at higher levels) is not "
            "implemented."
        ),
        implementation="agent.resolution:spend_second_wind",
        test="tests/agent/test_supported_rules.py::test_supported_second_wind_uses_and_rest_recovery",
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
        title="Catalog spells: Fire Bolt, Magic Missile, Sacred Flame and Cure Wounds",
        scope=(
            "Only these declared spells resolve. Fire Bolt is a spell attack (1d10 fire, two dice "
            "at 5th level and up, more at 11th and 17th; a critical doubles the dice). Magic Missile "
            "is three automatic-hit darts of 1d4+1 force, one more dart per slot above 1st. Sacred "
            "Flame is a DEX save by the target against the caster's spell save DC, with 1d8 radiant "
            "damage on a failure only. Cure Wounds restores 1d8 plus the spellcasting modifier to the "
            "caster, one die more per slot above 1st; it cannot target anyone else. Cantrips cost no "
            "slot. A leveled spell spends the named slot or the lowest one available; slot totals "
            "follow class and level for the full and half casters, and a long rest restores them. "
            "Concentration and advantage are not tracked. Other spells get a narrative ruling."
        ),
        implementation="agent.spells:resolve_spell",
        test="tests/agent/test_spells.py::test_catalog_spell_attack_hit_miss_and_crit",
    ),
    Mechanic(
        id="long_rest",
        title="Long rest healing and Hit Dice recovery",
        scope=(
            "A long rest (sleep or camp-for-the-night wording) restores HP to the maximum, every "
            "expended spell slot and a fighter's Second Wind use. It also regains half the "
            "character's level in spent Hit Dice, minimum one. 2024 campaigns follow the same "
            "Hit Dice rule in this engine. No other long-rest resource is reset by the engine."
        ),
        implementation="agent.resolution:heal_from_rest",
        test="tests/agent/test_supported_rules.py::test_supported_long_rest_restores_hp",
    ),
    Mechanic(
        id="short_rest",
        title="Short rest healing spends one Hit Die",
        scope=(
            "A short rest (rest-for-an-hour wording) spends one Hit Die of the class and rolls it "
            "plus the CON modifier, minimum 1 HP, healing no more than the missing HP. With no Hit "
            "Dice left it is refused: nothing is rolled and nothing is healed until a long rest. A "
            "declared count of Hit Dice is not parsed, so one die is spent. A short rest also "
            "restores a fighter's Second Wind use."
        ),
        implementation="agent.resolution:heal_from_rest",
        test="tests/agent/test_supported_rules.py::test_supported_short_rest_spends_one_hit_die",
    ),
    Mechanic(
        id="hit_dice",
        title="Hit Dice ledger on the character sheet",
        scope=(
            "Each character has Hit Dice equal to its level, of the class die (d6 sorcerer or "
            "wizard, d8 bard, cleric, druid, monk, rogue, warlock or artificer, d10 fighter, "
            "paladin or ranger, d12 barbarian). The spent count is an engine-owned sheet resource, "
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
            "stored name is a narrative tag: it has no engine effect."
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
        id="contested_maneuvers",
        title="Grapple, pin and restrain; shove prone (in combat, contested)",
        scope=(
            "In combat, against one foe, with rope or bare hands. The player's Athletics check is "
            "contested by the foe's Dexterity check (Acrobatics without proficiency); the higher "
            "total wins and a tie fails. Success restrains a grappled or pinned foe: its attacks have "
            "disadvantage and attacks against it have advantage. Success on a shove prone knocks the "
            "foe prone: its attacks have disadvantage, and melee attacks against it have advantage "
            "while ranged attacks have disadvantage. The maneuver replaces that turn's attack. A "
            "restraint ends when the player declares a release, when the foe is defeated, or when the "
            "encounter ends. A prone foe does not stand up, and a restrained foe makes no escape "
            "contest; both are fixed for the rest of the encounter."
        ),
        implementation="agent.combatants:add_effect",
        test="tests/agent/test_supported_rules.py::test_supported_contested_maneuver_applies_effect",
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
        id="condition_effects",
        title="Mechanical effects of conditions",
        boundary="Stored condition names do not penalise rolls, speed, actions or senses.",
        owner="EDG-043",
    ),
    UnsupportedMechanic(
        id="positioning_cover",
        title="Positioning, cover, high ground and flanking",
        boundary="No cover, elevation or flanking modifier is applied and no position is tracked.",
        owner="EDG-044",
    ),
    UnsupportedMechanic(
        id="action_economy",
        title="Bonus actions, reactions, Dodge, Dash, Disengage, Help and opportunity attacks",
        boundary="Each round allows one action; these named actions have no engine effect.",
        owner="EDG-044",
    ),
    UnsupportedMechanic(
        id="class_features",
        title="Class features other than Second Wind",
        boundary="Action Surge, Sneak Attack, Rage, Divine Smite and similar features have no engine effect.",
        owner="unassigned",
    ),
    UnsupportedMechanic(
        id="monster_traits",
        title="Monster traits, multiattack and special actions",
        boundary="Enemies attack once per round with the single attack recorded in their roster.",
        owner="EDG-004",
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
        "levels scale those numbers up and allow more foes. Over-budget foes are clamped into range "
        "or left out, never renamed. Exact creature identities and source records are kept. The "
        "combat start notes any adaptation, and the combat state records it."
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
    CONDITION: "condition_effects",
    ADVANTAGE: "advantage_disadvantage",
    ACTION_ECONOMY: "action_economy",
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
        "Narrative ruling: action economy is not tracked. Bonus actions, reactions, Dodge, Dash, "
        "Disengage, Help and opportunity attacks have no engine effect."
    ),
    POSITIONING: (
        "Narrative ruling: positioning is not tracked. Cover, high ground and flanking give no "
        "modifier and no position is recorded."
    ),
}

_SPELL_NAMES = (
    "magic missile|fire bolt|eldritch blast|cure wounds|healing word|hold person|thunderwave|"
    "burning hands|mage armor|misty step|guiding bolt|sacred flame|detect magic|faerie fire|"
    "feather fall|fireball|lightning bolt|color spray|witch bolt|ray of frost|chill touch|"
    "shocking grasp|acid splash|vicious mockery|dissonant whispers|spiritual weapon|hunter's mark"
)
_FEATURE_NAMES = (
    "action surge|sneak attack|divine smite|lay on hands|barbarian rage|enter a rage|"
    "bardic inspiration|channel divinity|wild shape|sorcery points?|ki points?|flurry of blows|"
    "stunning strike|superiority dice|martial arts"
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
        r"shov\w*|prone|held\s+(?:fast|down|in\s+place|still)|knocked\s+(?:down|prone|over)|"
        r"(?:can|could)(?:n't|\s+not)\s+(?:move|escape|get\s+up|stand))\b"
    ),
    CONDITION: (
        r"\b(?:stunned|blinded|poisoned|paralyzed|petrified|charmed|frightened|deafened|"
        r"incapacitated)\b"
    ),
    ADVANTAGE: r"\b(?:advantage|disadvantage|with an edge)\b",
    ACTION_ECONOMY: (
        r"\b(?:(?:dodge|dash|disengage|help)\s+action|bonus\s+action|opportunity\s+attacks?|"
        r"dodg\w*|disengag\w*)\b"
    ),
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

_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        SPELL,
        re.compile(
            r"\b(?:spell|spells|spellcasting|spellcast|cantrips?|rituals?|incantation|"
            r"incantesimo|incantesimi|lancio|lanciare|incantato|incantata)\b"
            r"|\bcast(?:s|ing)?\b(?!\s+(?:off|aside|doubt|suspicion|about|around|out|your|a\s+"
            r"(?:glance|look|light|shadow|net|line|vote|lot)|my\s+(?:eyes|gaze|net|line))\b)"
            r"|\b(?:" + _SPELL_NAMES + r")\b",
            re.IGNORECASE,
        ),
    ),
    (
        FEATURE,
        re.compile(r"\b(?:" + _FEATURE_NAMES + r")\b|\benter(?:s)?\s+a\s+rage\b", re.IGNORECASE),
    ),
    (
        MANEUVER,
        re.compile(
            r"\b(?:pin|pins|pinned|pinning|shove|shoves|shoved|shoving|restrain|restrains|"
            r"restrained|restraining|grapple|grapples|grappled|grappling|tackle|tackles|tackled|"
            r"trip|trips|tripped|tripping|knock(?:s|ed|ing)?\s+(?:\w+\s+)?prone|"
            r"hold\s+(?:him|her|it|them)\s+(?:down|in\s+place|still|fast))\b",
            re.IGNORECASE,
        ),
    ),
    (
        CONDITION,
        re.compile(
            r"\b(?:stun|stuns|stunned|stunning|blind|blinds|blinded|blinding|poison|poisons|"
            r"poisoned|poisoning|paralyze|paralyzes|paralyzed|paralyse|paralysed|petrify|"
            r"petrifies|petrified|charm|charms|charmed|frighten|frightens|frightened|deafen|"
            r"deafened|incapacitate|incapacitated)\b",
            re.IGNORECASE,
        ),
    ),
    (
        ADVANTAGE,
        re.compile(
            r"(?<!take\s)(?<!takes\s)(?<!taking\s)(?<!took\s)(?<!taken\s)"
            r"\b(?:advantage|disadvantage|with an edge)\b(?!\s+of\b)",
            re.IGNORECASE,
        ),
    ),
    (
        ACTION_ECONOMY,
        re.compile(
            r"\b(?:(?:dodge|dash|disengage|help)\s+action|bonus\s+action|opportunity\s+attacks?|"
            r"ready\s+an?\s+action|extra\s+attack|second\s+attack|off-hand\s+attack|"
            r"as\s+a\s+reaction|my\s+reaction)\b",
            re.IGNORECASE,
        ),
    ),
    (
        POSITIONING,
        re.compile(
            r"\b(?:flank\w*|take\s+cover|in\s+cover|behind\s+cover|high\s+ground|"
            r"blocks?\s+the\s+(?:door|doorway|exit|passage|corridor|stairs?|way)|"
            r"block\s+the\s+(?:door|doorway|exit|passage|corridor|stairs?|way))\b",
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


def _parsed_feature(parsed: Any) -> str | None:
    entities = getattr(parsed, "entities", None)
    if not isinstance(entities, dict):
        return None
    feature = str(entities.get("feature") or "").strip()
    if not feature or "second wind" in feature.lower():
        return None
    return feature[:60]


def _engine_applied_maneuver(text: str, engine_effects: tuple[str, ...]) -> bool:
    """True when the declared maneuver is a supported one whose effect the engine applied."""
    maneuver = classify_combat_maneuver(text)
    return maneuver is not None and maneuver.kind in engine_effects


ENGINE_ROLL_MODE_KEY = "engine_roll_mode"


def engine_roll_mode(state: dict[str, Any] | None) -> str:
    """The roll mode the engine applied this turn ("normal" when none or unknown)."""
    mode = (state or {}).get(ENGINE_ROLL_MODE_KEY)
    return mode if mode in ("advantage", "disadvantage") else "normal"


def classify_unsupported_actions(
    player_input: str | None,
    parsed: Any = None,
    character: dict | None = None,
    *,
    engine_roll_mode: str = "normal",
    engine_effects: tuple[str, ...] = (),
) -> tuple[UnsupportedAction, ...]:
    """Deterministic classification of the declared action against the unsupported list.

    Returns at most one entry per kind, in a stable order. Text is matched in English only.
    A declared catalog spell that the character can cast is executed by the engine and is not
    listed; a catalog spell that is refused (no access, no slot) is listed with that refusal as
    its ruling. Any other declared spell, and a spell attack proposed by the parser, is listed.
    When the engine applied advantage or disadvantage this turn, an advantage wording describes a
    real effect and is not an unsupported action. `engine_effects` are the typed effects the
    engine applied this turn (EDG-021): a supported maneuver that really applied one is not
    unsupported, so its narration is not stripped.
    """
    text = str(player_input or "")
    found: dict[str, UnsupportedAction] = {}
    request = cast_request(text, parsed, character)
    if request is not None:
        spell, _plan, refusal = request
        if refusal:
            found[SPELL] = UnsupportedAction(
                SPELL, spell.name.lower(), f"Narrative ruling: {refusal}"
            )
    elif _parsed_spell(parsed):
        found[SPELL] = UnsupportedAction(SPELL, "spell (parsed)")
    feature = _parsed_feature(parsed)
    if feature:
        found.setdefault(FEATURE, UnsupportedAction(FEATURE, feature.lower()))
    for kind, pattern in _TEXT_PATTERNS:
        if (
            kind == SPELL
            and request is not None
            and request[2] is None
            and not _UNCOVERED_SPELL.search(text)
        ):
            continue  # the executed catalog spell; its casting words are not a declared spell
        if kind == ADVANTAGE and engine_roll_mode != "normal":
            continue
        match = pattern.search(text)
        if not match or kind in found:
            continue
        if kind == MANEUVER and _engine_applied_maneuver(text, engine_effects):
            continue
        found[kind] = UnsupportedAction(kind, match.group(0).lower()[:60])
    return tuple(found[kind] for kind in _KIND_ORDER if kind in found)


def engine_effects_for_state(state: dict[str, Any]) -> tuple[str, ...]:
    """Typed effects the engine applied in this turn, as recorded by the combat round."""
    return tuple(str(kind) for kind in (state.get("engine_effects") or ()))


def actions_for_state(state: dict[str, Any]) -> tuple[UnsupportedAction, ...]:
    return classify_unsupported_actions(
        state.get("player_input"),
        state.get("parsed_input"),
        state.get("character"),
        engine_roll_mode=engine_roll_mode(state),
        engine_effects=engine_effects_for_state(state),
    )


def executed_spell_for_state(state: dict[str, Any]) -> bool:
    """True when this turn casts a catalog spell the engine executes (no refusal)."""
    request = cast_request(
        state.get("player_input"), state.get("parsed_input"), state.get("character")
    )
    return request is not None and request[2] is None


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


def _claim_pattern(
    actions: tuple[UnsupportedAction, ...],
    *,
    engine_roll_mode: str = "normal",
    executed_spell: bool = False,
    engine_effects: tuple[str, ...] = (),
) -> re.Pattern[str] | None:
    """Claims of every unsupported mechanic, once any unsupported action is declared.

    The union is deliberate: a fake or confused provider may describe a different unsupported
    effect than the one the player declared (a spell turn narrating a pin, say), and neither the
    narrator nor a flag can be trusted to name its own mechanic. Three exceptions describe engine
    facts: a catalog spell the engine executed (its name, dice and slots), an advantage or
    disadvantage the engine applied this turn, and a restraint or prone effect really applied; those
    claims are not applied to that turn's text.
    """
    if not actions:
        return None
    patterns = [
        p
        for kind, p in _CLAIM_WORDS.items()
        if not (executed_spell and kind == SPELL)
        and not (kind == ADVANTAGE and engine_roll_mode != "normal")
        and not (kind == MANEUVER and engine_effects)
    ]
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def apply_adjudication_boundary(
    adj: Any,
    actions: tuple[UnsupportedAction, ...],
    *,
    engine_roll_mode: str = "normal",
    executed_spell: bool = False,
    engine_effects: tuple[str, ...] = (),
) -> None:
    """Remove LLM-proposed effects that claim an unsupported mechanic. Mutates `adj`.

    Conditions are never stored for an unsupported turn. Damage is removed for spell, feature and
    maneuver turns because the engine has no verified effect for them. Flags whose key or value
    claims an unsupported effect are dropped. The ruling is stated in the mechanical summary.
    `executed_spell` marks a turn whose catalog spell the engine executes (see `_claim_pattern`).
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
    claim = _claim_pattern(
        actions,
        engine_roll_mode=engine_roll_mode,
        executed_spell=executed_spell,
        engine_effects=engine_effects,
    )
    if claim is not None:
        adj.flags_set = [
            fu
            for fu in adj.flags_set
            if not claim.search(f"{str(fu.key).replace('_', ' ')} {fu.value}")
        ]
    rulings = " ".join(ruling_lines(actions))
    summary = _strip_claim_sentences(adj.mechanical_summary, claim)
    adj.mechanical_summary = f"{summary} {rulings}".strip()


def _strip_claim_sentences(text: str, claim: re.Pattern[str] | None) -> str:
    """Keep line structure; drop each sentence that claims an unsupported effect."""
    kept_lines: list[str] = []
    for line in str(text or "").splitlines():
        sentences = [s for s in _SENTENCE_END.split(line.strip()) if s.strip()]
        survivors = [s for s in sentences if claim is None or not claim.search(s)]
        if survivors:
            kept_lines.append(" ".join(survivors))
    return "\n\n".join(kept_lines).strip()


def apply_narration_boundary(
    narration: str,
    actions: tuple[UnsupportedAction, ...],
    *,
    engine_roll_mode: str = "normal",
    executed_spell: bool = False,
    engine_effects: tuple[str, ...] = (),
) -> str:
    """Drop narration sentences that claim an unsupported effect and state the ruling.

    Line structure is kept. When any unsupported action was declared, the ruling is appended, so
    the player always sees that the declared effect is narrative only.
    """
    if not actions:
        return narration
    body = _strip_claim_sentences(
        narration,
        _claim_pattern(
            actions,
            engine_roll_mode=engine_roll_mode,
            executed_spell=executed_spell,
            engine_effects=engine_effects,
        ),
    )
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
