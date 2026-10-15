"""Engine-owned catalog of the declared spells the engine resolves (EDG-027).

Only the spells below have a verified effect. Each entry is data: level, kind, dice, ability and
slot scaling, class list and rulebook provenance. The engine rolls every die and sets every
number; neither the parser nor the narrator supplies one. A declared spell that is not listed
keeps the narrative ruling from `agent.supported_rules` and has no effect.

Kinds:
  attack     spell attack roll (`resolve_attack(..., spell=True)`); damage dice on a hit, doubled on a crit
  auto       automatic-hit darts (Magic Missile): no roll
  save       the target's saving throw against the caster's spell save DC; damage on a failed save only
  heal       restores dice plus the spellcasting ability modifier to the caster
  condition  the target's saving throw against the spell save DC; no damage, and the spell's
             condition applies on a failed save (Blindness/Deafness)

A spell with `condition` set applies it on a hit (attack) or a failed save (condition). Its
duration and the condition itself are recorded by `agent.conditions`, never by the parser.

Slots: a cantrip (level 0) costs no slot. A leveled spell spends the slot the player names
("2nd-level slot") or else the lowest slot available at or above its level. Upcasting adds
`upcast_dice` per slot level above the spell's level. Cantrip dice scale with character level
(5th, 11th and 17th). Expended counts are stored in the sheet's `stats["spell_slots_expended"]`;
the totals are derived from class and level, and a long rest clears the expended count.

Casting time: `action` is "action" (the default) or "bonus_action" (Healing Word). A bonus-action
spell uses the turn's bonus action slot (agent.action_economy), so the same turn may also attack.

Deliberately absent: concentration spells (no concentration tracking exists), Burning Hands (an
area effect) and Hold Person (concentration, humanoid-only). Blindness/Deafness is cast here as
blindness only, at one target: a declaration that names deafness alone is refused, and the upcast
extra targets are not modelled. Healing Word heals only its caster, as Cure Wounds does; a creature
other than the hero is not a target. A spell is added here only with its effect, cost and
targeting implemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent.action_economy import ACTION, BONUS_ACTION
from agent.resolution import (
    CASTING_ABILITY,
    CheckResult,
    RollMode,
    ability_modifier,
    character_class,
    proficiency_bonus,
    resolve_attack,
    resolve_check,
)
from tools.dice import roll

_PHB = "Player's Handbook (2014 core rules)"


@dataclass(frozen=True)
class SpellSpec:
    name: str
    level: int  # 0 = cantrip
    kind: str  # "attack" | "auto" | "save" | "heal"
    dice: int  # base number of dice
    die: int  # sides per die
    damage_type: str | None
    classes: tuple[str, ...]
    upcast_dice: int = 0  # extra dice per slot level above `level`
    per_die_bonus: int = 0  # flat bonus added to each die (Magic Missile's +1)
    ability_bonus: bool = False  # heal: add the spellcasting modifier once
    save_ability: str | None = None  # save and condition: the ability the target saves with
    condition: str | None = None  # the condition the spell applies (agent.conditions)
    duration_rounds: int = 0  # rounds the condition lasts, counting the round it is applied
    aliases: tuple[str, ...] = ()  # other names the player may use for the spell
    action: str = ACTION  # the turn slot the cast uses: ACTION or BONUS_ACTION
    source: str = ""


CATALOG: tuple[SpellSpec, ...] = (
    SpellSpec(
        name="Fire Bolt",
        level=0,
        kind="attack",
        dice=1,
        die=10,
        damage_type="fire",
        classes=("sorcerer", "wizard"),
        source=f"{_PHB}, Fire Bolt: ranged spell attack, 1d10 fire damage",
    ),
    SpellSpec(
        name="Magic Missile",
        level=1,
        kind="auto",
        dice=3,
        die=4,
        damage_type="force",
        classes=("sorcerer", "wizard"),
        upcast_dice=1,
        per_die_bonus=1,
        source=f"{_PHB}, Magic Missile: three darts of 1d4+1 force, automatic hits",
    ),
    SpellSpec(
        name="Sacred Flame",
        level=0,
        kind="save",
        dice=1,
        die=8,
        damage_type="radiant",
        classes=("cleric",),
        save_ability="DEX",
        source=f"{_PHB}, Sacred Flame: DEX save against the spell save DC, 1d8 radiant on a failure",
    ),
    SpellSpec(
        name="Cure Wounds",
        level=1,
        kind="heal",
        dice=1,
        die=8,
        damage_type=None,
        classes=("bard", "cleric", "druid", "paladin", "ranger"),
        upcast_dice=1,
        ability_bonus=True,
        source=f"{_PHB}, Cure Wounds: 1d8 plus the spellcasting modifier, one die more per slot above 1st",
    ),
    SpellSpec(
        name="Guiding Bolt",
        level=1,
        kind="attack",
        dice=4,
        die=6,
        damage_type="radiant",
        classes=("cleric",),
        upcast_dice=1,
        condition="marked",
        duration_rounds=2,
        source=(
            f"{_PHB}, Guiding Bolt: ranged spell attack, 4d6 radiant on a hit, and the next attack "
            "roll against the target before the end of your next turn has advantage; one die more "
            "per slot above 1st"
        ),
    ),
    SpellSpec(
        name="Blindness/Deafness",
        level=2,
        kind="condition",
        dice=0,  # no damage dice: the spell's effect is the condition
        die=0,
        damage_type=None,
        classes=("bard", "cleric", "sorcerer", "wizard"),
        save_ability="CON",
        condition="blinded",
        duration_rounds=10,
        aliases=("Blindness",),
        source=(
            f"{_PHB}, Blindness/Deafness: the target makes a Constitution save or is blinded for one "
            "minute, and repeats the save at the end of each of its turns"
        ),
    ),
    SpellSpec(
        name="Healing Word",
        level=1,
        kind="heal",
        dice=1,
        die=4,
        damage_type=None,
        classes=("bard", "cleric", "druid"),
        upcast_dice=1,
        ability_bonus=True,
        action=BONUS_ACTION,
        source=f"{_PHB}, Healing Word: bonus action, 1d4 plus the spellcasting modifier, one die more per slot above 1st",
    ),
)

_BY_NAME = {name.lower(): spell for spell in CATALOG for name in (spell.name, *spell.aliases)}
# Longest first, so an alias never shadows the full name it begins.
_NAMES = sorted(_BY_NAME, key=len, reverse=True)
_DEAFNESS = re.compile(r"\bdeaf\w*", re.IGNORECASE)
_SPELL_NAME_WORDS = re.compile(r"\bblindness(?:/deafness)?\b", re.IGNORECASE)
_CAST_VERB = re.compile(
    r"\b(?:cast|casts|casting|hurl|hurls|unleash|unleashes|conjure|conjures|invoke|invokes|"
    r"use|uses|using|speak|speaks)\b",
    re.IGNORECASE,
)
_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(name) for name in _NAMES) + r")\b", re.IGNORECASE)
_SLOT_LEVEL_RE = re.compile(
    r"\b([1-9])(?:st|nd|rd|th)[\s-]+level\b|\blevel\s+([1-9])\s+(?:spell\s+)?slots?\b",
    re.IGNORECASE,
)

# Total slots per spell level by character level (index = level - 1), PHB full and half casters.
FULL_CASTER_SLOTS: tuple[tuple[int, ...], ...] = (
    (2,), (3,), (4, 2), (4, 3), (4, 3, 2), (4, 3, 3), (4, 3, 3, 1), (4, 3, 3, 2), (4, 3, 3, 3, 1),
    (4, 3, 3, 3, 2), (4, 3, 3, 3, 2, 1), (4, 3, 3, 3, 2, 1), (4, 3, 3, 3, 2, 1, 1),
    (4, 3, 3, 3, 2, 1, 1), (4, 3, 3, 3, 2, 1, 1, 1), (4, 3, 3, 3, 2, 1, 1, 1),
    (4, 3, 3, 3, 2, 1, 1, 1, 1), (4, 3, 3, 3, 3, 1, 1, 1, 1), (4, 3, 3, 3, 3, 2, 1, 1, 1),
    (4, 3, 3, 3, 3, 2, 2, 1, 1),
)  # fmt: skip
HALF_CASTER_SLOTS: tuple[tuple[int, ...], ...] = (
    (), (2,), (3,), (3,), (4, 2), (4, 2), (4, 3), (4, 3), (4, 3, 2), (4, 3, 2),
    (4, 3, 3), (4, 3, 3), (4, 3, 3, 1), (4, 3, 3, 1), (4, 3, 3, 2), (4, 3, 3, 2),
    (4, 3, 3, 3, 1), (4, 3, 3, 3, 1), (4, 3, 3, 3, 2), (4, 3, 3, 3, 2),
)  # fmt: skip
_FULL_CASTERS = frozenset({"bard", "cleric", "druid", "sorcerer", "wizard"})
_HALF_CASTERS = frozenset({"paladin", "ranger"})


@dataclass(frozen=True)
class CastPlan:
    spell: SpellSpec
    slot_level: int | None  # None for cantrips
    expended: dict[str, int] | None  # the expended count to persist; None when no slot is spent


@dataclass
class SpellResult:
    spec: SpellSpec
    slot_level: int | None
    expended: dict[str, int] | None
    attack: CheckResult | None = None
    save_dc: int | None = None
    save_success: bool | None = None
    darts: int = 0
    damage: int = 0
    healing: int = 0
    condition: str | None = None  # set only when the spell's condition now applies to a foe
    dice_log: list[dict] = field(default_factory=list)


def _level(character: dict | None) -> int:
    try:
        return min(20, max(1, int((character or {}).get("level") or 1)))
    except (TypeError, ValueError):
        return 1


def _parsed_spell_name(parsed: Any) -> str | None:
    entities = getattr(parsed, "entities", None)
    if not isinstance(entities, dict):
        return None
    name = " ".join(str(entities.get("spell") or "").lower().split())
    return name or None


def find_catalog_spell(player_input: str | None, parsed: Any = None) -> SpellSpec | None:
    """The catalog spell the player declares as cast this turn, or None.

    The spell's name must appear in the player's words, with a casting verb ("cast", "hurl") or
    the parser's explicit spell entity. A bare mention ("what does Magic Missile do?") is not a cast.
    Two different catalog spells in one declaration are ambiguous, so neither is executed.
    """
    text = str(player_input or "")
    named = {_BY_NAME[m.lower()] for m in _NAME_RE.findall(text)}
    if len(named) != 1:
        return None
    spell = named.pop()
    if _CAST_VERB.search(text) or _parsed_spell_name(parsed) == spell.name.lower():
        return spell
    return None


def requested_slot_level(text: str | None) -> int | None:
    match = _SLOT_LEVEL_RE.search(str(text or ""))
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def slot_totals(character: dict | None) -> dict[int, int]:
    """Total slots per spell level, derived from class and level (no stored totals)."""
    cls = character_class(character)
    if cls in _FULL_CASTERS:
        table = FULL_CASTER_SLOTS
    elif cls in _HALF_CASTERS:
        table = HALF_CASTER_SLOTS
    else:
        return {}
    return {index + 1: count for index, count in enumerate(table[_level(character) - 1])}


def expended_slots(character: dict | None) -> dict[str, int]:
    """Slots already spent, keyed by spell level as a string (the stored shape)."""
    stats = (character or {}).get("stats")
    raw = stats.get("spell_slots_expended") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        try:
            level, count = str(int(key)), int(value)
        except (TypeError, ValueError):
            continue
        if level in {str(n) for n in range(1, 10)} and count > 0:
            out[level] = count
    return out


def spell_save_dc(character: dict | None) -> int | None:
    ability = CASTING_ABILITY.get(character_class(character))
    if ability is None:
        return None
    return 8 + proficiency_bonus(_level(character)) + ability_modifier(character, ability)


def plan_cast(
    character: dict | None, spell: SpellSpec, requested_slot: int | None = None
) -> tuple[CastPlan | None, str | None]:
    """Decide whether the cast can happen and which slot it spends. Rolls nothing.

    Returns (plan, None) or (None, refusal). A refusal is the transparent engine message: the
    cast has no effect and no state changes.
    """
    hero = str((character or {}).get("name") or "The hero")
    if character_class(character) not in spell.classes:
        return None, (
            f"{spell.name} is not cast: {hero} has no access to it, so no spell effect is applied."
        )
    if spell.level == 0:
        return CastPlan(spell, None, None), None
    totals = slot_totals(character)
    used = expended_slots(character)

    def free(level: int) -> int:
        return totals.get(level, 0) - int(used.get(str(level), 0))

    if requested_slot is not None:
        if requested_slot < spell.level:
            return None, (
                f"{spell.name} is not cast: it needs a slot of level {spell.level} or higher, "
                "so no spell effect is applied."
            )
        level = requested_slot if free(requested_slot) > 0 else None
    else:
        level = next((n for n in range(spell.level, 10) if free(n) > 0), None)
    if level is None:
        needed = requested_slot or spell.level
        return None, (
            f"{spell.name} is not cast: no spell slot of level {needed} is left for {hero}, "
            "so no spell effect is applied."
        )
    expended = dict(used)
    expended[str(level)] = int(used.get(str(level), 0)) + 1
    return CastPlan(spell, level, expended), None


def cast_request(
    player_input: str | None, parsed: Any, character: dict | None
) -> tuple[SpellSpec, CastPlan | None, str | None] | None:
    """(spell, plan, refusal) for a declared catalog spell, or None when no catalog spell is cast."""
    spell = find_catalog_spell(player_input, parsed)
    if spell is None:
        return None
    # The spell's own name contains "Blindness", so only the words around it are read. Only
    # blindness is modelled: a declaration that mentions deafness is refused, never blinded instead.
    rest = _SPELL_NAME_WORDS.sub("", str(player_input or ""))
    if spell.condition == "blinded" and _DEAFNESS.search(rest):
        return (
            spell,
            None,
            (
                f"{spell.name} is not cast: this engine applies only blindness, so no spell effect is applied."
            ),
        )
    plan, refusal = plan_cast(character, spell, requested_slot_level(player_input))
    return spell, plan, refusal


def _cantrip_tier(level: int) -> int:
    return 4 if level >= 17 else 3 if level >= 11 else 2 if level >= 5 else 1


def _dice_count(spec: SpellSpec, character_level: int, slot: int) -> int:
    if spec.level == 0:
        return spec.dice * _cantrip_tier(character_level)
    return spec.dice + spec.upcast_dice * max(0, slot - spec.level)


def _expr(count: int, die: int, flat: int) -> str:
    return f"{count}d{die}{flat:+d}" if flat else f"{count}d{die}"


def resolve_spell(
    character: dict | None,
    plan: CastPlan,
    *,
    target_ac: int | None = None,
    target_save_bonus: int = 0,
    rng: Any = None,
    roll_mode: RollMode = "normal",
) -> SpellResult:
    """Roll and resolve a planned cast. `target_ac` set means a combat target; None means no foe.

    Outside combat no foe is tracked, so an attack uses the medium DC tier that outside-combat
    attacks use and a save target has no modifier. Dice are rolled for the record either way.
    """
    spec = plan.spell
    level = _level(character)
    slot = plan.slot_level or spec.level
    result = SpellResult(spec, plan.slot_level, plan.expended)

    if spec.kind == "attack":
        if target_ac is None:
            check = resolve_check(character, "spell_attack", rng=rng)
        else:
            check = resolve_attack(character, target_ac, spell=True, rng=rng, roll_mode=roll_mode)
        result.attack = check
        result.dice_log.extend(check.dice_log)
        if check.success:
            count = _dice_count(spec, level, slot) * (2 if check.critical else 1)
            outcome = roll(_expr(count, spec.die, 0), rng)
            result.damage = max(1, outcome.total)
            result.dice_log.append(outcome.to_log(f"{spec.name} damage"))
            # The condition needs a combat target; outside combat no foe carries it.
            if spec.condition and target_ac is not None:
                result.condition = spec.condition
    elif spec.kind == "auto":
        darts = _dice_count(spec, level, slot)
        outcome = roll(_expr(darts, spec.die, darts * spec.per_die_bonus), rng)
        result.darts = darts
        result.damage = max(1, outcome.total)
        result.dice_log.append(outcome.to_log(f"{spec.name} darts"))
    elif spec.kind == "save":
        result.save_dc = spell_save_dc(character)
        bonus = int(target_save_bonus)
        save = roll(f"1d20{bonus:+d}" if bonus else "1d20", rng)
        result.dice_log.append(save.to_log(f"{spec.save_ability} save"))
        result.save_success = result.save_dc is not None and save.total >= result.save_dc
        if not result.save_success:
            outcome = roll(_expr(_dice_count(spec, level, slot), spec.die, 0), rng)
            result.damage = max(1, outcome.total)
            result.dice_log.append(outcome.to_log(f"{spec.name} damage"))
    elif spec.kind == "condition":
        if target_ac is None:
            # No foe is tracked outside combat: nothing is rolled and no condition is applied.
            return result
        result.save_dc = spell_save_dc(character)
        bonus = int(target_save_bonus)
        save = roll(f"1d20{bonus:+d}" if bonus else "1d20", rng)
        result.dice_log.append(save.to_log(f"{spec.save_ability} save"))
        result.save_success = result.save_dc is not None and save.total >= result.save_dc
        if not result.save_success and spec.condition:
            result.condition = spec.condition
    elif spec.kind == "heal":
        ability = CASTING_ABILITY.get(character_class(character), "WIS")
        mod = ability_modifier(character, ability) if spec.ability_bonus else 0
        outcome = roll(_expr(_dice_count(spec, level, slot), spec.die, mod), rng)
        result.healing = max(0, outcome.total)
        result.dice_log.append(outcome.to_log(f"{spec.name} healing"))
    return result


def narrate(result: SpellResult, *, hero: str, target: str | None) -> str:
    """The engine's sentence for a resolved cast. Numbers come from the roll, never from prose."""
    spec = result.spec
    at = f" {target}" if target else ""
    if spec.kind == "attack" and result.attack is not None:
        if not result.attack.success:
            return f"{spec.name} misses{at}."
        crit = "Critical hit! " if result.attack.critical else ""
        return f"{crit}{spec.name} hits{at} for {result.damage} {spec.damage_type} damage."
    if spec.kind == "auto":
        return f"{spec.name} sends {result.darts} darts{at} for {result.damage} force damage."
    if spec.kind == "save":
        who = target or "The target"
        if result.save_success:
            return (
                f"{who} succeeds on the {spec.save_ability} save against {spec.name} "
                f"(DC {result.save_dc}) and takes no damage."
            )
        return (
            f"{who} fails the {spec.save_ability} save against {spec.name} (DC {result.save_dc}) "
            f"and takes {result.damage} {spec.damage_type} damage."
        )
    if spec.kind == "heal":
        return f"{spec.name} restores {result.healing} HP to {hero}."
    if spec.kind == "condition":
        if result.save_success is None:
            return f"{spec.name} has no foe to target. {NO_FOE_NOTE}"
        who = target or "The target"
        if result.save_success:
            return (
                f"{who} succeeds on the {spec.save_ability} save against {spec.name} "
                f"(DC {result.save_dc}) and is unaffected."
            )
        return (
            f"{who} fails the {spec.save_ability} save against {spec.name} (DC {result.save_dc})."
        )
    return f"{spec.name} has no effect."


NO_FOE_NOTE = "No foe is tracked outside combat, so no enemy HP changes."
