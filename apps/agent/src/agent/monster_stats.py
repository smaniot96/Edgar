"""Typed stats for exact creature records (EDG-004).

A creature record that resolved through `db.vector.creatures` is parsed here, once, by code. The
encounter builder LLM chooses which exact creatures appear; it never supplies their numbers. The
parsed `MonsterStats` is stored as a plain JSON dict on the combatant (`monster_stats`), so it
survives persistence and reload, and the combat round reads it back.

Executed by the engine: armor class, hit points, the DEX modifier for initiative, saving-throw
bonuses (a spell's save), the attacks listed under Actions (bonus, damage dice and damage type),
a Multiattack trait whose counts are stated in its text, Regeneration with its stated amount and
its acid/fire-style suppression clause, and resistances, immunities and vulnerabilities by damage
type (EDG-043). Also executed: the creature type (its size-and-type line, for humanoid-only spells),
a poisoned rider on a hit whose wording is read exactly (`AttackRider`), a Recharge X-6 attack
(`MonsterAttack.recharge`), rolled at the start of the creature's turns once spent, and a
breath-weapon style save action whose wording is read exactly (`AttackSave`): the hero saves
instead of the action rolling to hit. A save action may also leave the hero stunned (Mind Blast)
or frightened (a dragon's Frightful Presence, used once as the Multiattack opener it is named in).
Legendary actions (EDG-043): the budget the record states and each option whose body is exactly an
attack the creature already lists ("makes a tail attack") or the dragon Wing Attack's save
(`LegendaryOption`), including prone and band-level flight. Exact listed catalog spells use
record-owned casting numbers and slots (`MonsterSpellcasting`). Separately reviewed lair passages
may supply complete red-dragon magma/tremor options (`LairAction`). The engine decides every roll.

Declared, never implied: exact speed and geometry, unsupported lair and reaction actions, legendary
options not read exactly (Detect, movement, spells, conditions), noncatalog/innate casting, save actions whose wording
is not read exactly, spell attacks, other damage riders and every other trait. Each one is listed
in `MonsterStats.declared` and shown in the combat start message and the roster.

Parsing is conservative: when a clause cannot be read exactly it is declared rather than guessed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from agent.spells import SpellSpec, catalog_spell
from tools.dice import DiceOutcome, parse_dice, roll

ABILITIES = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
DAMAGE_TYPES = frozenset(
    {
        "acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic", "piercing",
        "poison", "psychic", "radiant", "slashing", "thunder",
    }
)  # fmt: skip

# Damage type of each engine weapon (`agent.resolution._WEAPONS`), from the standard weapon table.
# A weapon outside this table (the generic weapon) has no known type, so no resistance applies.
WEAPON_DAMAGE_TYPE = {
    "club": "bludgeoning", "greatclub": "bludgeoning", "dagger": "piercing",
    "quarterstaff": "bludgeoning",
    "mace": "bludgeoning", "spear": "piercing", "handaxe": "slashing", "javelin": "piercing",
    "light hammer": "bludgeoning", "sickle": "slashing", "shortsword": "piercing",
    "scimitar": "slashing", "rapier": "piercing", "whip": "slashing", "longsword": "slashing",
    "battleaxe": "slashing", "warhammer": "bludgeoning", "morningstar": "piercing",
    "flail": "bludgeoning", "trident": "piercing", "war pick": "piercing",
    "greatsword": "slashing", "greataxe": "slashing", "maul": "bludgeoning", "glaive": "slashing",
    "halberd": "slashing", "pike": "piercing", "lance": "piercing", "shortbow": "piercing",
    "longbow": "piercing", "blowgun": "piercing", "hand crossbow": "piercing",
    "heavy crossbow": "piercing",
    "light crossbow": "piercing", "crossbow": "piercing", "sling": "bludgeoning", "dart": "piercing",
}  # fmt: skip

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}  # fmt: skip
_SECTION_HEADINGS = {
    "actions": "actions",
    "reactions": "reactions",
    "legendary actions": "legendary",
    "lair actions": "lair",
    "bonus actions": "bonus",
    "mythic actions": "other",
    "villain actions": "other",
}
_STAT_LINE = re.compile(
    r"^(?:Armor Class|Hit Points|Speed|Saving Throws|Skills|Senses|Languages|Challenge|"
    r"Damage (?:Resistances|Immunities|Vulnerabilities)|Condition Immunities|Proficiency Bonus)\b",
    re.IGNORECASE,
)
_ENTRY = re.compile(r"^([A-Z][\w'’ ()/,\u2013\u2014-]{0,70}?)\.\s+(\S.*)$")
_LOWER_ATTACK_ENTRY = re.compile(r"^([a-z][\w'’ ()/,\u2013\u2014-]{0,70}?)\.\s+(\S.*)$")
_ATTACK = re.compile(
    r"^(Melee or Ranged|Melee|Ranged) Weapon Attack:\s*\+(-?\d+) to hit\b.*?\bHit:\s*([0-9lI]+)\s*"
    r"\(([^)]*)\)\s*([A-Za-z]+) damage\b(.*)$",
    re.IGNORECASE,
)
_REGEN_AMOUNT = re.compile(
    r"\bregains?\s+(\d+)\s+hit\s+points?\s+at\s+the\s+start\s+of\s+[\w\s'’]{0,30}?\bturn\b",
    re.IGNORECASE,
)
_REGEN_SUPPRESS = re.compile(
    r"\btakes?\s+([a-z]+(?:\s+(?:or|and)\s+[a-z]+)*)\s+damage,?\s+this\s+trait\s+"
    r"(?:doesn['’]?t|does\s+not)\s+function\b.*?\bnext\s+turn",
    re.IGNORECASE,
)
_SUPPRESS_MAGIC = ("silvered", "adamantine", "magical", "magic")
# Size and type line ("Small humanoid (goblinoid), neutral evil"). Records may wrap it in markdown
# underscores or asterisks. The size word is required, so a heading or a stat line never matches.
_TYPE_LINE = re.compile(
    r"^[_*\s]*(?:Tiny|Small|Medium|Large|Huge|Gargantuan)\s+([A-Za-z][A-Za-z-]*)\b", re.IGNORECASE
)
# The poisoned rider, exactly as the Monster Manual words it on a hit (EDG-043, see AttackRider).
# Any other clause after the damage (extra damage, unconsciousness, a different condition) is not
# matched, so the attack keeps its declared rider.
_POISON_RIDER = re.compile(
    r"^,?\s*and the target must (?:succeed on|make) a DC (\d+) Constitution saving throw or be "
    r"poisoned for 1 minute\.\s*The target can repeat the saving throw at the end of each of its "
    r"turns, ending the effect (?:on itself |on the target |on it )?on a success\.?\s*$",
    re.IGNORECASE,
)
# A frightened or stunned rider that lasts until the end of the attacker's next turn (Scarecrow Claw,
# PDF p.269; Empyrean Maul, PDF p.131). It has no repeat save, being that short.
_TURN_RIDER = re.compile(
    r"^\.\s*If the target is a creature, it must succeed on a DC (\d+) (Wisdom|Constitution) saving "
    r"throw or be (frightened|stunned) until the end of (?:the|its) [^.]+?['’]s next turn\.?\s*$",
    re.IGNORECASE,
)
# "Recharge 5-6" (or "Recharge 6") in an ability name. Recharge after a rest is not a d6 recharge.
_RECHARGE_NAME = re.compile(r"\s*\(Recharge\s*(?:([2-5])\s*[-–]\s*6|(6))\)\s*$", re.IGNORECASE)
# A breath weapon, exactly as the Monster Manual words it (EDG-043, see AttackSave): one save for
# each creature in the area, which is the hero alone (an exact-record fight has no other creature on
# its side). Extra clauses ("provided that ...", a rider after the damage) are not matched, so the
# action stays declared. The mind flayer's Mind Blast is worded the same way with "magically emits".
_SAVE_LEAD = (
    r"^The [^.]+? (?:exhales|magically emits) [^.]+? in an? \d+-foot "
    r"(?:cone|line that is \d+ feet wide)\.\s*Each creature in that (?:area|line) must "
)
_SAVE_ABILITY = r"(Strength|Dexterity|Constitution|Intelligence|Wisdom|Charisma)"
_SAVE_DAMAGE = r"(\d+) \(([^)]*)\) ([a-z]+) damage"
_HALF_SAVE = re.compile(
    rf"{_SAVE_LEAD}make a DC (\d+) {_SAVE_ABILITY} saving throw, taking {_SAVE_DAMAGE} on a failed "
    r"save, or half as much damage on a successful one\.?\s*$",
    re.IGNORECASE,
)
_REPEAT_SAVE = (
    r"A creature can repeat the saving throw at the end of each of its turns, ending the effect on "
    r"itself on a success\."
)
# "... or take N (dice) type damage and be stunned for 1 minute": the failed save's condition lasts a
# minute with the repeat save (Mind Blast, MM PDF p.223). A success avoids the damage and the condition.
_NO_EFFECT_SAVE = re.compile(
    rf"{_SAVE_LEAD}succeed on a DC (\d+) {_SAVE_ABILITY} saving throw or take {_SAVE_DAMAGE}"
    rf"(?: and be (?P<condition>stunned) for 1 minute\.\s*{_REPEAT_SAVE}|\.?)\s*$",
    re.IGNORECASE,
)
# Legendary actions (EDG-043). The budget is stated in the section's lead sentence ("can take 3
# legendary actions"); an option's cost is in its name ("Wing Attack (Costs 2 Actions)"), 1 when absent.
_LEGENDARY_BUDGET = re.compile(r"\bcan take (\w+) legendary actions?\b", re.IGNORECASE)
_LEGENDARY_COST = re.compile(r"\s*\(Costs ([1-9]\d*) Actions?\)\s*$", re.IGNORECASE)
# An option that only names one of the creature's own attacks: "makes a tail attack", "makes one claw
# attack" or "makes one attack with its hooves". Anything else ("one attack", "or uses its ...") is not read.
_LEGENDARY_ATTACK = re.compile(
    r"^The [^.]+? makes (?:an? |one )(?:(\w+) attack|attack with its (\w+))\.?\s*$", re.IGNORECASE
)
# The dragon Wing Attack, exactly as the Monster Manual words it: save, prone rider and flight.
_WING_ATTACK = re.compile(
    rf"^The [^.]+? beats its (?:tattered )?wings\.\s*Each creature within \d+ feet of the [^.]+? must "
    rf"succeed on a DC (\d+) {_SAVE_ABILITY} saving throw or take {_SAVE_DAMAGE} and be knocked "
    r"prone\.\s*(?:The [^.]+? can then|After beating its wings this way, the [^.]+? can) fly up to "
    r"half its flying speed\.?\s*$",
    re.IGNORECASE,
)
# A dragon's Frightful Presence (MM PDF pp.98-99): the hero is the only creature of the dragon's choice
# in range. The 24-hour immunity after a success or the end of the effect makes it one use per fight.
_FRIGHTFUL_PRESENCE = re.compile(
    r"^Each creature of the (?P<who>\w+)['’]s choice that is within 120 feet of the (?P=who) and aware "
    r"of it must succeed on a DC (?P<dc>\d+) Wisdom saving throw or become frightened for 1 minute\.\s*"
    rf"{_REPEAT_SAVE}\s*If a creature['’]s saving throw is successful or the effect ends for it, the "
    r"creature is immune to the (?P=who)['’]s Frightful Presence for the next 24 hours\.?\s*$",
    re.IGNORECASE,
)
# A Multiattack that opens with a once-per-encounter action ("The dragon can use its Frightful Presence.
# It then makes three attacks"): the opener is made first, as the book words it.
_MULTI_OPENER = re.compile(r"^The [^.]+? can use its ([A-Z][A-Za-z ]+?)\. It then makes\b")
# An ability modifier in parentheses: "(+4)", "(-1)", or the PDF text layer's "(- 2)".
_ABILITY_MOD = r"\(\s*([+\-\u2212\u2013]?)\s*(\d+)\s*\)"
# Entry names may carry en or em dashes ("Fire Breath (Recharge 5–6)"), which the name pattern needs.
_ATTACK_LEAD = re.compile(r"^(?:Melee or Ranged|Melee|Ranged) Weapon Attack:", re.IGNORECASE)
_MULTI_WITH = re.compile(r"\battacks?\s+with\s+(?:its|his|her|their|the)\s+([A-Za-z]+)", re.I)


class MonsterStatsError(ValueError):
    """A creature record has no stat block the engine can read exactly."""


@dataclass(frozen=True)
class AttackRider:
    """A condition the target of a hit must save against (EDG-043). Only exact wording is read.

    `poisoned`: a Constitution save at `dc`, failure poisons the target for one minute, and the
    target repeats the save at the end of each of its turns (the repeat ends the effect on success).
    `frightened` or `stunned` with no `duration_rounds`: the condition lasts until the end of the
    attacker's next turn, with no repeat save.
    """

    kind: str
    dc: int
    ability: str
    duration_rounds: int | None


@dataclass(frozen=True)
class AttackSave:
    """The hero's saving throw against a save action (EDG-043), instead of an attack roll.

    A failed save takes the rolled damage (an action without damage dice has none). A success takes
    half of it, rounded down, when `half_on_success`, and none otherwise. A failed save also leaves
    the hero in `condition` for one minute, repeating this save at the end of each of its turns.
    `once` is set when the book makes the hero immune afterwards: the action is made once per fight.
    Frightful Presence's 24-hour immunity is approximated as fight-local; encounters within that
    24-hour period are not linked.
    """

    ability: str
    dc: int
    half_on_success: bool
    condition: str | None = None
    once: bool = False


@dataclass(frozen=True)
class MonsterAttack:
    name: str
    bonus: int  # 0 for a save action, which has no attack roll
    damage_dice: str  # empty for a save action that deals no damage
    damage_type: str
    ranged: bool = False  # a save action is made from either band
    recharge: int | None = None  # "Recharge N-6": the d6 result that restores the attack (N)
    rider: AttackRider | None = None
    save: AttackSave | None = None


@dataclass(frozen=True)
class LegendaryOption:
    """A legendary action option the engine executes (EDG-043): an attack or save action and its cost."""

    name: str
    cost: int
    attack: MonsterAttack
    flight_after: bool = False


@dataclass(frozen=True)
class LairAction:
    """One exact, supported lair save effect from a separately reviewed passage."""

    name: str
    ability: str
    dc: int
    damage_dice: str = ""
    damage_type: str = ""
    half_on_success: bool = False
    condition: str | None = None


@dataclass(frozen=True)
class MonsterSpellcasting:
    """Spellcasting facts read from one exact stat-block entry.

    `spells` stores (slot level, catalog spell name) pairs. Unsupported names stay declared and
    are never silently converted into one of the executable catalog spells.
    """

    ability: str
    caster_level: int
    save_dc: int
    attack_bonus: int
    spells: tuple[tuple[int, str], ...] = ()
    slots: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class DamageRule:
    """A resistance, immunity or vulnerability. `nonmagical_only` is set by "from nonmagical attacks"."""

    damage_type: str
    nonmagical_only: bool = False


@dataclass(frozen=True)
class Regeneration:
    amount: int
    suppressed_by: tuple[str, ...] = ()


def _attack_from(raw: Mapping[str, Any]) -> MonsterAttack:
    """A stored attack. Rows written before riders, recharge and save actions read with none."""
    rider = raw.get("rider")
    save = raw.get("save")
    return MonsterAttack(
        name=str(raw["name"]),
        bonus=int(raw["bonus"]),
        damage_dice=str(raw["damage_dice"]),
        damage_type=str(raw["damage_type"]),
        ranged=bool(raw.get("ranged", False)),
        recharge=None if raw.get("recharge") is None else int(raw["recharge"]),
        rider=None if not isinstance(rider, Mapping) else AttackRider(**rider),
        save=None if not isinstance(save, Mapping) else AttackSave(**save),
    )


@dataclass(frozen=True)
class MonsterStats:
    name: str
    hp_max: int
    ac: int
    speed: str | None = None
    abilities: dict[str, int] = field(default_factory=dict)  # ability modifiers
    saves: dict[str, int] = field(default_factory=dict)  # saving-throw bonuses, when stated
    attacks: tuple[MonsterAttack, ...] = ()
    multiattack: tuple[tuple[str, int], ...] = ()  # (attack name, count), in the order stated
    regeneration: Regeneration | None = None
    resistances: tuple[DamageRule, ...] = ()
    immunities: tuple[DamageRule, ...] = ()
    vulnerabilities: tuple[DamageRule, ...] = ()
    declared: tuple[str, ...] = ()  # traits and actions the engine does not execute
    legendary_actions: int = 0  # the budget per round, 0 for none
    legendary: tuple[LegendaryOption, ...] = ()  # only the options read exactly
    lair_actions: tuple[LairAction, ...] = ()
    spellcasting: MonsterSpellcasting | None = None
    creature_type: str | None = (
        None  # "humanoid", "beast", ... from the type line; None when unread
    )

    @property
    def initiative_bonus(self) -> int:
        return self.abilities.get("DEX", 0)

    def save_bonus(self, ability: str) -> int:
        if ability in self.saves:
            return self.saves[ability]
        return self.abilities.get(ability, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "hp_max": self.hp_max,
            "ac": self.ac,
            "speed": self.speed,
            "abilities": dict(self.abilities),
            "saves": dict(self.saves),
            "attacks": [asdict(a) for a in self.attacks],
            "multiattack": [[name, count] for name, count in self.multiattack],
            "regeneration": None
            if self.regeneration is None
            else {
                "amount": self.regeneration.amount,
                "suppressed_by": list(self.regeneration.suppressed_by),
            },
            "resistances": [asdict(r) for r in self.resistances],
            "immunities": [asdict(r) for r in self.immunities],
            "vulnerabilities": [asdict(r) for r in self.vulnerabilities],
            "declared": list(self.declared),
            "legendary_actions": self.legendary_actions,
            "legendary": [asdict(o) for o in self.legendary],
            "lair_actions": [asdict(o) for o in self.lair_actions],
            "spellcasting": None if self.spellcasting is None else asdict(self.spellcasting),
            "creature_type": self.creature_type,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> MonsterStats:
        try:
            regen = raw.get("regeneration")
            return cls(
                name=str(raw["name"]),
                hp_max=int(raw["hp_max"]),
                ac=int(raw["ac"]),
                speed=raw.get("speed"),
                abilities={str(k): int(v) for k, v in dict(raw.get("abilities") or {}).items()},
                saves={str(k): int(v) for k, v in dict(raw.get("saves") or {}).items()},
                attacks=tuple(_attack_from(a) for a in raw.get("attacks") or ()),
                creature_type=raw.get("creature_type"),
                multiattack=tuple((str(n), int(c)) for n, c in raw.get("multiattack") or ()),
                regeneration=None
                if regen is None
                else Regeneration(int(regen["amount"]), tuple(regen.get("suppressed_by") or ())),
                resistances=tuple(DamageRule(**r) for r in raw.get("resistances") or ()),
                immunities=tuple(DamageRule(**r) for r in raw.get("immunities") or ()),
                vulnerabilities=tuple(DamageRule(**r) for r in raw.get("vulnerabilities") or ()),
                declared=tuple(str(d) for d in raw.get("declared") or ()),
                legendary_actions=int(raw.get("legendary_actions") or 0),
                legendary=tuple(
                    LegendaryOption(
                        str(o["name"]), int(o["cost"]), _attack_from(o["attack"]),
                        bool(o.get("flight_after", False)),
                    )
                    for o in raw.get("legendary") or ()
                ),
                lair_actions=tuple(
                    LairAction(**item)
                    for item in raw.get("lair_actions") or ()
                    if isinstance(item, Mapping)
                ),
                spellcasting=(
                    MonsterSpellcasting(
                        ability=str(raw["spellcasting"]["ability"]),
                        caster_level=int(raw["spellcasting"].get("caster_level", 1)),
                        save_dc=int(raw["spellcasting"]["save_dc"]),
                        attack_bonus=int(raw["spellcasting"]["attack_bonus"]),
                        spells=tuple(
                            (int(level), str(spell))
                            for level, spell in raw["spellcasting"].get("spells") or ()
                        ),
                        slots={
                            str(level): int(count)
                            for level, count in (raw["spellcasting"].get("slots") or {}).items()
                        },
                    )
                    if isinstance(raw.get("spellcasting"), Mapping)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MonsterStatsError("stored monster stats are malformed") from exc


# --------------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------------


def _count(word: str) -> int | None:
    word = word.strip().lower()
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def _norm(name: str) -> str:
    """Compare an attack name with its plural in multiattack text ("claws" -> "claw")."""
    return name.strip().lower().rstrip("s")


def _line_value(text: str, label: str) -> str | None:
    match = re.search(rf"^{label}\s+(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _abilities(text: str, lines: list[str]) -> dict[str, int]:
    labelled = {
        m.group(1).upper(): _signed_mod(m.group(2), m.group(3))
        for m in re.finditer(rf"\b(STR|DEX|CON|INT|WIS|CHA)\s+\d+\s*{_ABILITY_MOD}", text, re.I)
    }
    if len(labelled) == len(ABILITIES):
        return {a: labelled[a] for a in ABILITIES}
    # Unlabelled ability row: the header "STR DEX CON INT WIS CHA" precedes "18 (+4) 13 (+1) ...".
    for index, line in enumerate(lines[:-1]):
        if re.fullmatch(r"STR\s+DEX\s+CON\s+INT\s+WIS\s+CHA", line.strip(), re.IGNORECASE):
            pairs = re.findall(rf"(\d+)\s*{_ABILITY_MOD}", lines[index + 1])
            if len(pairs) == len(ABILITIES):
                return {
                    a: _signed_mod(sign, digits)
                    for a, (_score, sign, digits) in zip(ABILITIES, pairs, strict=True)
                }
    return {}


def _signed_mod(sign: str, digits: str) -> int:
    """An ability modifier from its sign and digits; a minus sign may be a dash in the text layer."""
    value = int(digits)
    return -value if sign in ("-", "\u2212", "\u2013") else value


def _saves(text: str) -> dict[str, int]:
    line = _line_value(text, "Saving Throws")
    if not line:
        return {}
    return {
        a.upper(): int(v)
        for a, v in re.findall(r"\b(STR|DEX|CON|INT|WIS|CHA)\s+([+-]\d+)", line, re.I)
    }


def _damage_rules(text: str, label: str) -> tuple[tuple[DamageRule, ...], list[str]]:
    """Types from a "Damage ..." line. Clauses with an unreadable qualifier are declared instead."""
    line = _line_value(text, label)
    if not line:
        return (), []
    # The PDF text layer splits "nonmagical" into "non magical" (and sometimes "non-magical").
    line = re.sub(r"\bnon[\s-]+magical\b", "nonmagical", line, flags=re.IGNORECASE)
    rules: list[DamageRule] = []
    declared: list[str] = []
    for clause in line.split(";"):
        words = re.findall(r"[a-z]+", clause.lower())
        types = [w for w in words if w in DAMAGE_TYPES]
        if not types:
            continue
        if "nonmagical" in words:
            rules += [DamageRule(t, nonmagical_only=True) for t in types]
            if any(w in words for w in ("silvered", "adamantine")):
                # The hero's weapons are plain, so the rule is applied; the exception is not read.
                declared.append(
                    f"{label.lower()} silvered or adamantine exception not executed "
                    f"(the hero's weapons count as plain): {clause.strip()}"
                )
        elif any(w in words for w in _SUPPRESS_MAGIC):
            declared.append(f"{label.lower()} qualifier not executed: {clause.strip()}")
        else:
            rules += [DamageRule(t) for t in types]
    return tuple(rules), declared


def _regeneration(body: str) -> Regeneration | None:
    amount = _REGEN_AMOUNT.search(body)
    if not amount:
        return None
    suppressed: tuple[str, ...] = ()
    clause = _REGEN_SUPPRESS.search(body)
    if clause:
        types = tuple(t for t in re.split(r"\s+(?:or|and)\s+", clause.group(1).lower()))
        if not all(t in DAMAGE_TYPES for t in types):
            return None
        suppressed = types
    elif re.search(r"\btakes?\b[^.]*\bdamage\b[^.]*\bfunction\b", body, re.IGNORECASE):
        return None  # a suppression clause this parser cannot read exactly
    return Regeneration(int(amount.group(1)), suppressed)


def _exact_damage(average: str, dice_text: str, damage_type: str) -> tuple[str, str] | None:
    """(dice, damage type) when the dice are plain, the type is known and the average agrees."""
    dice = re.sub(r"\s+", "", dice_text)
    damage_type = damage_type.lower()
    if not re.fullmatch(r"\d+d\d+(?:[+-]\d+)?", dice) or damage_type not in DAMAGE_TYPES:
        return None
    parsed = parse_dice(dice)
    expected = math.floor(parsed.count * (parsed.sides + 1) / 2 + parsed.modifier)
    # PDF OCR can render printed 11 as ll. Only accept this digit-confusable
    # average when it agrees with the independently parsed exact damage dice.
    normalized = average.translate(str.maketrans({"l": "1", "I": "1"}))
    return (dice, damage_type) if normalized.isdecimal() and expected == int(normalized) else None


def _save_action(body: str, name: str, recharge: int | None) -> MonsterAttack | None:
    """A breath-weapon style action whose text reads exactly: the hero's save, then the damage."""
    presence = _FRIGHTFUL_PRESENCE.match(body) if recharge is None else None
    if presence:
        save = AttackSave("WIS", int(presence["dc"]), False, "frightened", once=True)
        return MonsterAttack(name, 0, "", "", ranged=True, save=save)
    for half_on_success, pattern in ((True, _HALF_SAVE), (False, _NO_EFFECT_SAVE)):
        match = pattern.match(body)
        if match:
            dc, ability, average, dice_text, damage_type = match.groups()[:5]
            damage = _exact_damage(average, dice_text, damage_type)
            if damage is None:
                return None
            condition = match.groupdict().get("condition")
            save = AttackSave(
                ability[:3].upper(), int(dc), half_on_success, condition and condition.lower()
            )
            return MonsterAttack(name, 0, *damage, ranged=True, recharge=recharge, save=save)
    return None


def _attack(body: str, name: str) -> tuple[MonsterAttack | None, str | None]:
    match = _ATTACK.match(body)
    if not match:
        return None, None
    kind, bonus, average, dice_text, damage_type, rest = match.groups()
    damage = _exact_damage(average, dice_text, damage_type)
    if damage is None:
        return None, None
    dice, damage_type = damage
    rider: AttackRider | None = None
    if poison := _POISON_RIDER.match(rest):
        rider = AttackRider("poisoned", int(poison.group(1)), "CON", duration_rounds=10)
    elif short := _TURN_RIDER.match(rest):
        dc, ability, condition = short.groups()
        rider = AttackRider(condition.lower(), int(dc), ability[:3].upper(), duration_rounds=None)
    if rider:
        return MonsterAttack(
            name, int(bonus), dice, damage_type, kind.lower() == "ranged", None, rider
        ), None
    declared = f"{name}: damage rider or extra effect not executed" if rest.strip(" .") else None
    attack = MonsterAttack(name, int(bonus), dice, damage_type, ranged=kind.lower() == "ranged")
    return attack, declared


def _legendary_option(
    entry_name: str, body: str, attacks: tuple[MonsterAttack, ...]
) -> tuple[LegendaryOption | None, str | None]:
    """An executable legendary option and the declared note for what it leaves out, else (None, None).

    Read exactly: the dragon Wing Attack, or an option that names one plain attack of the creature
    ("makes a tail attack"). The attack must exist once among `attacks` and must not recharge or
    be a save action; otherwise the option stays declared rather than guessed.
    """
    cost = _LEGENDARY_COST.search(entry_name)
    name = entry_name[: cost.start()].strip() if cost else entry_name
    cost_value = int(cost.group(1)) if cost else 1
    wing = _WING_ATTACK.match(body)
    if wing:
        dc, ability, average, dice_text, damage_type = wing.groups()
        damage = _exact_damage(average, dice_text, damage_type)
        if damage is None:
            return None, None
        save = AttackSave(ability[:3].upper(), int(dc), half_on_success=False, condition="prone")
        attack = MonsterAttack(name, 0, *damage, save=save)
        return LegendaryOption(name, cost_value, attack, flight_after=True), None
    named = _LEGENDARY_ATTACK.match(body)
    if named:
        key = _norm(named.group(1) or named.group(2))
        found = [
            a for a in attacks if _norm(a.name) == key and a.recharge is None and a.save is None
        ]
        if len(found) == 1:
            return LegendaryOption(name, cost_value, found[0]), None
    return None, None


def parse_lair_actions(text: str) -> tuple[LairAction, ...]:
    """Read whole MM red-dragon magma/tremor options from a reviewed passage.

    The source lead owns initiative 20, losing ties and no consecutive repetition. A complete
    bullet must match: a recognized saving-throw substring cannot authorize an action with
    extra conditions, persistent damage or other clauses. Other lair options stay unsupported.
    The encounter uses abstract bands, so point placement, radii and grounded terrain are not
    simulated; the selected option targets the hero in the authored encounter.
    """
    raw = str(text or "").replace("’", "'")
    # Undo soft line-wrap hyphenation in this source's prose, retaining numeric compounds.
    raw = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", raw)
    raw = re.split(r"\bREGIONAL\s+EFFECTS\b", raw, maxsplit=1, flags=re.I)[0]
    lead = re.search(
        r"On\s+initiative\s+count\s+20\s*\(losing\s+initiative\s+ties\),\s*"
        r"the\s+dragon\s+takes\s+a\s+lair\s+action\s+to\s+cause\s+one\s+of\s+the\s+"
        r"following\s+effects;\s*the\s+dragon\s+can't\s+use\s+the\s+same\s+effect\s+"
        r"two\s+rounds\s+in\s+a\s+row:",
        raw,
        re.I,
    )
    if lead is None:
        return ()
    options = re.split(r"[•●]|(?:^|\n)\s*[-*]\s+", raw[lead.end():])
    magma = re.compile(
        r"Magma erupts from a point on the ground the dragon can see within 120 feet of it, "
        r"creating a 20-foot-high, 5-foot-radius geyser\. Each creature in the geyser's area "
        r"must make a DC (\d+) Dexterity saving throw, taking (\d+) \(([^)]*)\) fire damage "
        r"on a failed save, or half as much damage on a successful one\.", re.I,
    )
    tremor = re.compile(
        r"A tremor shakes the lair in a 60-foot radius around the dragon\. Each creature other "
        r"than the dragon on the ground in that area must succeed on a DC (\d+) Dexterity "
        r"saving throw or be knocked prone\.", re.I,
    )
    actions: dict[str, LairAction] = {}
    for option in options:
        body = " ".join(option.split()).strip()
        if match := magma.fullmatch(body):
            damage = _exact_damage(match.group(2), match.group(3), "fire")
            if damage is not None:
                actions.setdefault(
                    "Magma Geyser",
                    LairAction("Magma Geyser", "DEX", int(match.group(1)), *damage, True),
                )
        elif match := tremor.fullmatch(body):
            actions.setdefault(
                "Tremor", LairAction("Tremor", "DEX", int(match.group(1)), condition="prone"),
            )
    return tuple(actions.values())


def _multiattack(
    body: str, attacks: tuple[MonsterAttack, ...]
) -> tuple[tuple[str, int], ...] | None:
    total_match = re.search(r"\bmakes?\s+(\w+)\s+(?:melee\s+)?attacks?\b", body, re.IGNORECASE)
    total = _count(total_match.group(1)) if total_match else None
    if not total or not attacks:
        return None
    names = {_norm(a.name): a.name for a in attacks}
    pairs = re.findall(r"\b(\w+)\s+with\s+(?:its|his|her|their|the)?\s*([A-Za-z]+)", body, re.I)
    counts: dict[str, int] = {}
    single = _MULTI_WITH.search(body)
    if single and not any(_count(count_word) is not None for count_word, _ in pairs):
        # "makes two attacks with its scimitar": every attack is that one listed attack.
        key = _norm(single.group(1))
        if key not in names:
            return None
        counts[names[key]] = total
    elif pairs:
        for count_word, attack_word in pairs:
            n = _count(count_word)
            key = _norm(attack_word)
            if n is None or key not in names:
                return None
            counts[names[key]] = counts.get(names[key], 0) + n
    elif len(attacks) == 1:
        counts = {attacks[0].name: total}
    else:
        return None
    if sum(counts.values()) != total:
        return None
    opening = _MULTI_OPENER.match(body)
    opener = next((a for a in attacks if opening and a.name == opening.group(1)), None)
    first = ((opener.name, 1),) if opener and opener.save and opener.save.once else ()
    return first + tuple(counts.items())


_SPELLCAST_ABILITY = re.compile(
    r"spellcasting ability is (Strength|Dexterity|Constitution|Intelligence|Wisdom|Charisma)",
    re.IGNORECASE,
)
_SPELLCAST_LEVEL = re.compile(r"\b(\d+)(?:st|nd|rd|th)-level spellcaster\b", re.IGNORECASE)
_SPELLCAST_DC = re.compile(r"spell save DC\s+(\d+)", re.IGNORECASE)
_SPELLCAST_ATTACK = re.compile(r"([+-]\d+) to hit with spell attacks?", re.IGNORECASE)
_SPELL_LIST_HEADING = re.compile(
    r"\b(Cantrips?)\s*\(at will\)\s*:|"
    r"\b(\d+)(?:st|nd|rd|th)\s+level\s*\((\d+)\s+slots?\)\s*:",
    re.IGNORECASE,
)


def _spellcasting_entry(body: str) -> tuple[MonsterSpellcasting | None, tuple[str, ...]]:
    """Read only listed spell levels/slots and the explicitly stated casting numbers."""
    ability = _SPELLCAST_ABILITY.search(body)
    caster_level_match = _SPELLCAST_LEVEL.search(body)
    dc = _SPELLCAST_DC.search(body)
    attack = _SPELLCAST_ATTACK.search(body)
    headings = list(_SPELL_LIST_HEADING.finditer(body))
    if ability is None or caster_level_match is None or dc is None or attack is None or not headings:
        return None, ("spellcasting: incomplete ability, save DC, attack bonus or spell list",)

    slots: dict[str, int] = {}
    spells: list[tuple[int, str]] = []
    unsupported: list[str] = []
    for index, heading in enumerate(headings):
        listed_level = 0 if heading.group(1) else int(heading.group(2))
        if listed_level:
            slots[str(listed_level)] = int(heading.group(3))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        listed = body[heading.end() : end].strip()
        # A period followed by whitespace ends the spell-list sentence. A few extracted stat
        # blocks put punctuation immediately before the first spell ("Cantrips (at will):.fire
        # bolt"); keep that token and strip only its edge punctuation below instead of treating it
        # as an empty list. This also leaves every comma-delimited unsupported name countable.
        listed = re.split(r"\.\s+", listed, maxsplit=1)[0]
        names = [" ".join(part.split()).strip(" .;") for part in listed.split(",")]
        for spell_name in names:
            if not spell_name:
                continue
            spec: SpellSpec | None = catalog_spell(spell_name)
            if spec is None or spec.level != listed_level:
                unsupported.append(spell_name)
                continue
            spells.append((listed_level, spec.name))
    if not spells:
        unsupported.append("no listed spell is in the engine's action-spell catalog")
    result = MonsterSpellcasting(
        ability=ability.group(1).upper()[:3],
        caster_level=int(caster_level_match.group(1)),
        save_dc=int(dc.group(1)),
        attack_bonus=int(attack.group(1)),
        spells=tuple(dict.fromkeys(spells)),
        slots=slots,
    )
    notes = (
        (f"spellcasting: {len(unsupported)} unsupported spell list entr{'y' if len(unsupported) == 1 else 'ies'} not executed",)
        if unsupported
        else ()
    )
    return result, notes


def parse_monster_stats(text: str, *, name: str) -> MonsterStats:
    """Read the exact record's stat block. Raises MonsterStatsError without AC and HP."""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    ac = re.search(r"\bArmor Class\s+(\d+)", text or "", re.IGNORECASE)
    hp = re.search(r"\bHit Points\s+(\d+)", text or "", re.IGNORECASE)
    if not ac or not hp:
        raise MonsterStatsError("armor class and hit points are required")
    speed = _line_value(text, "Speed")
    abilities = _abilities(text, lines)
    saves = _saves(text)
    resistances, decl_r = _damage_rules(text, "Damage Resistances")
    immunities, decl_i = _damage_rules(text, "Damage Immunities")
    vulnerabilities, decl_v = _damage_rules(text, "Damage Vulnerabilities")
    declared: list[str] = [*decl_r, *decl_i, *decl_v]
    if speed:
        declared.append(f"speed {speed}: movement is not simulated")
    if _line_value(text, "Condition Immunities"):
        declared.append("condition immunities: conditions have no engine effect")

    # Entries: "Name. text" lines, continued by the lines that follow until the next entry.
    entries: list[list[str]] = []
    section = "traits"
    current: list[str] | None = None
    for line in lines:
        heading = _SECTION_HEADINGS.get(line.lower().rstrip(":"))
        if heading is not None:
            section, current = heading, None
            if heading == "lair":
                declared.append(
                    "lair actions: not executed (no encounter data states that the creature is in its lair)"
                )
            continue
        if _STAT_LINE.match(line):
            current = None
            continue
        match = _ENTRY.match(line)
        if match is None and section == "actions":
            possible = _LOWER_ATTACK_ENTRY.match(line)
            if possible and _ATTACK_LEAD.match(possible.group(2)):
                match = possible
        if match:
            current = [section, match.group(1).strip(), match.group(2).strip()]
            entries.append(current)
        elif current is not None:
            current[2] += " " + line

    attacks: list[MonsterAttack] = []
    legendary_entries: list[tuple[str, str]] = []
    multiattack_body: str | None = None
    regeneration: Regeneration | None = None
    spellcasting: MonsterSpellcasting | None = None
    for entry_section, entry_name, body in entries:
        lowered = entry_name.lower()
        # Section ownership comes before interpreting the wording. A save or Recharge clause in a
        # legendary, lair, reaction, bonus or other action is not an ordinary monster action.
        if entry_section == "legendary":
            legendary_entries.append((entry_name, body))
            continue
        if entry_section in ("lair", "reactions", "bonus"):
            declared.append(f"{entry_name}: {entry_section} action not executed")
            continue
        if entry_section == "other":
            declared.append(f"{entry_name}: action not executed")
            continue

        recharge = _RECHARGE_NAME.search(entry_name)
        action_name = entry_name[: recharge.start()].strip() if recharge else entry_name
        recharge_on = int(recharge.group(1) or recharge.group(2)) if recharge else None
        if (
            recharge
            and entry_section == "actions"
            and re.match(r"(?:Melee|Ranged) Weapon Attack:", body)
        ):
            # An attack that recharges (EDG-043): executed once its attack text reads exactly.
            attack, rider = _attack(body, action_name)
            if attack is None:
                declared.append(
                    f"{entry_name}: recharge ability not executed (attack text not exact)"
                )
            else:
                attacks.append(replace(attack, recharge=recharge_on))
                if rider:
                    declared.append(rider)
        elif (
            entry_section == "actions"
            and (recharge or "recharge" not in lowered)
            and (save_action := _save_action(body, action_name, recharge_on)) is not None
        ):
            attacks.append(save_action)
        elif "recharge" in lowered:
            declared.append(f"{entry_name}: recharge ability not executed")
        elif lowered == "multiattack" and multiattack_body is None:
            multiattack_body = body
        elif lowered == "regeneration" and entry_section == "traits":
            regeneration = _regeneration(body)
            if regeneration is None:
                declared.append(f"{entry_name}: regeneration clause could not be read exactly")
            elif "dies only if" in body:
                declared.append(
                    f"{entry_name}: a creature at 0 HP is defeated at once; its death clause "
                    "(regeneration at the start of its next turn) is not executed"
                )
        elif entry_section == "actions" and _ATTACK_LEAD.match(body):
            attack, rider = _attack(body, entry_name)
            if attack is None:
                declared.append(f"{entry_name}: attack could not be read exactly")
            else:
                attacks.append(attack)
                if rider:
                    declared.append(rider)
        elif lowered == "spellcasting" and entry_section == "traits":
            spellcasting, spell_notes = _spellcasting_entry(body)
            declared.extend(spell_notes)
            if spellcasting is None:
                declared.append(f"{entry_name}: no spellcasting effect is executed")
            elif not spellcasting.spells:
                declared.append(f"{entry_name}: no supported spell is executed")
        elif (lowered == "brute" and entry_section == "traits"
              and re.search(r"\bincluded in the attack\b", body, re.IGNORECASE)):
            # The source's attack dice already include this trait. Do not count
            # it twice or declare those existing dice an unexecuted ability.
            pass
        else:
            declared.append(f"{entry_name}: trait not executed")

    multiattack: tuple[tuple[str, int], ...] = ()
    if multiattack_body is not None:
        counted = _multiattack(multiattack_body, tuple(attacks))
        if counted is None:
            declared.append("Multiattack: counts could not be read exactly; one attack is used")
        else:
            multiattack = counted
        if re.search(r"\bdisadvantage\b", multiattack_body or "", re.IGNORECASE):
            declared.append("Multiattack: the disadvantage on a later attack is not executed")

    budget = _LEGENDARY_BUDGET.search(text)
    legendary_actions = (_count(budget.group(1)) or 0) if budget else 0
    legendary: list[LegendaryOption] = []
    for entry_name, body in legendary_entries:
        option, note = (
            _legendary_option(entry_name, body, tuple(attacks))
            if legendary_actions
            else (None, None)
        )
        if option is None:
            declared.append(f"{entry_name}: legendary action not executed")
            continue
        legendary.append(option)
        if note:
            declared.append(note)

    return MonsterStats(
        name=str(name)[:60],
        hp_max=int(hp.group(1)),
        ac=int(ac.group(1)),
        speed=speed,
        abilities=abilities,
        saves=saves,
        attacks=tuple(attacks),
        multiattack=multiattack,
        regeneration=regeneration,
        resistances=resistances,
        immunities=immunities,
        vulnerabilities=vulnerabilities,
        declared=tuple(declared),
        legendary_actions=legendary_actions,
        legendary=tuple(legendary),
        spellcasting=spellcasting,
        creature_type=_creature_type(lines),
    )


def _creature_type(lines: list[str]) -> str | None:
    """The type word of the size-and-type line that precedes the armor class ("humanoid")."""
    for line in lines[:4]:
        if re.match(r"Armor Class\b", line, re.IGNORECASE):
            break
        match = _TYPE_LINE.match(line)
        if match:
            return match.group(1).lower()
    return None


# --------------------------------------------------------------------------------------------
# Combat helpers (pure except where a combatant dict is documented as updated)
# --------------------------------------------------------------------------------------------


def stats_of(combatant: Mapping[str, Any]) -> MonsterStats | None:
    """The combatant's exact stats, or None for a legacy or generic combatant."""
    raw = combatant.get("monster_stats")
    if not isinstance(raw, Mapping):
        return None
    try:
        return MonsterStats.from_dict(raw)
    except MonsterStatsError:
        return None


def combat_fields(stats: MonsterStats) -> dict[str, Any]:
    """Legacy mirror fields for readers that expect one attack and a DEX bonus. Derived, not authoritative."""
    first = stats.attacks[0] if stats.attacks else None
    return {
        "hp_max": stats.hp_max,
        "hp_current": stats.hp_max,
        "ac": stats.ac,
        "attack_bonus": first.bonus if first else 0,
        "damage_dice": first.damage_dice if first else "0",
        "initiative_bonus": stats.initiative_bonus,
    }


def attack_plan(stats: MonsterStats, spent: Iterable[str] = ()) -> tuple[MonsterAttack, ...]:
    """Attacks this creature makes in one turn: every listed attack once per multiattack count.

    `spent` names recharge attacks used and not yet recharged, and once-per-fight actions used: they
    are left out of the plan. A ready recharge attack that the Multiattack does not name (a breath
    weapon) replaces the Multiattack, as it is an action of its own; without a multiattack, a ready
    recharge attack is used first too (its use is then spent), else the first attack that does not
    recharge.
    """
    spent_set = set(spent)
    multiattack_names = {name for name, _count in stats.multiattack}
    ready = [
        a
        for a in stats.attacks
        if a.recharge is not None and a.name not in spent_set and a.name not in multiattack_names
    ]
    if ready:
        return (ready[0],)
    if stats.multiattack:
        by_name = {a.name: a for a in stats.attacks}
        return tuple(
            by_name[name]
            for name, count in stats.multiattack
            for _ in range(count)
            if name not in spent_set
        )
    return tuple(a for a in stats.attacks if a.recharge is None and a.name not in spent_set)[:1]


def legendary_plan(stats: MonsterStats, left: int) -> tuple[LegendaryOption, ...]:
    """The options `left` legendary actions can pay for, in the order the engine tries them.

    The most expensive first, ties in the record's order. The cost is the book's own measure of an
    option's strength, so the budget goes to the strongest option it covers and the rest to cheaper ones.
    """
    return tuple(sorted((o for o in stats.legendary if o.cost <= left), key=lambda o: -o.cost))


def recharge_turn_start(combatant: dict) -> list[tuple[str, DiceOutcome, bool]]:
    """Roll the d6 for each spent recharge attack at the start of this creature's turn (EDG-043).

    Returns (attack name, the engine's d6 roll, recharged) for each one rolled. A recharged attack
    leaves the spent list; one that did not recharge stays spent, as does a once-per-fight action.
    """
    stats = stats_of(combatant)
    spent = [str(n) for n in combatant.get("recharge_spent") or ()]
    if stats is None or not spent:
        return []
    results: list[tuple[str, DiceOutcome, bool]] = []
    still: list[str] = []
    for name in spent:
        attack = next((a for a in stats.attacks if a.name == name), None)
        if attack is None:  # a stored name with no attack behind it: nothing to restore
            continue
        if attack.recharge is None:  # a once-per-fight action does not recharge
            still.append(name)
            continue
        outcome = roll("1d6")
        recharged = outcome.total >= attack.recharge
        results.append((name, outcome, recharged))
        if not recharged:
            still.append(name)
    combatant["recharge_spent"] = still
    return results


def spend_recharge(combatant: dict, attack: MonsterAttack) -> None:
    """Record that a recharge attack was used, so it waits for its recharge roll (or, for a
    once-per-fight action, for the end of the encounter)."""
    if attack.recharge is None and not (attack.save and attack.save.once):
        return
    spent = [str(n) for n in combatant.get("recharge_spent") or ()]
    if attack.name not in spent:
        spent.append(attack.name)
    combatant["recharge_spent"] = spent


def regenerate(combatant: dict) -> tuple[int, str | None]:
    """Start-of-turn regeneration (mutates `hp_current` and resets `damage_taken_types`).

    Heals the stated amount up to the maximum, unless the creature took a suppressing damage type
    since its last turn. A creature at 0 HP never reaches this point (it is already defeated).
    """
    stats = stats_of(combatant)
    if stats is None or stats.regeneration is None:
        return 0, None
    name = str(combatant.get("name", "?"))
    taken = set(combatant.get("damage_taken_types") or [])
    combatant["damage_taken_types"] = []
    hp = int(combatant.get("hp_current") or 0)
    hp_max = int(combatant.get("hp_max") or stats.hp_max)
    if hp <= 0 or hp >= hp_max:
        return 0, None
    blocked = sorted(taken & set(stats.regeneration.suppressed_by))
    if blocked:
        return (
            0,
            f"{name} does not regenerate: it took {' and '.join(blocked)} damage since its last turn.",
        )
    healed = min(stats.regeneration.amount, hp_max - hp)
    combatant["hp_current"] = hp + healed
    return healed, f"{name} regenerates {healed} HP."


def record_damage(combatant: dict, damage_type: str | None, amount: int) -> None:
    """Remember a damage type taken since this creature's last turn (regeneration suppression)."""
    stats = stats_of(combatant)
    if not damage_type or amount <= 0 or stats is None or stats.regeneration is None:
        return
    taken = set(combatant.get("damage_taken_types") or [])
    taken.add(damage_type)
    combatant["damage_taken_types"] = sorted(taken)


def adjust_monster_damage(
    combatant: Mapping[str, Any], amount: int, damage_type: str | None, *, magical: bool
) -> tuple[int, str]:
    """The damage a foe takes after its resistances, immunities and vulnerabilities.

    Returns (amount, note). The note is empty when nothing applies. A player's weapon is treated as
    nonmagical; a spell is magical. An unknown damage type applies no rule, and the note says so.
    """
    stats = stats_of(combatant)
    if stats is None or amount <= 0:
        return amount, ""
    if not (stats.resistances or stats.immunities or stats.vulnerabilities):
        return amount, ""
    if damage_type is None:
        return amount, " (damage type unknown: resistances and immunities not applied)"

    def applies(rules: tuple[DamageRule, ...]) -> bool:
        return any(
            r.damage_type == damage_type and (not r.nonmagical_only or not magical) for r in rules
        )

    if applies(stats.immunities):
        return 0, f" ({damage_type} damage is blocked: immune)"
    resistant = applies(stats.resistances)
    vulnerable = applies(stats.vulnerabilities)
    if resistant == vulnerable:
        return amount, ""  # neither, or both (which cancel)
    if resistant:
        return amount // 2, f" ({damage_type} damage halved: resistant)"
    return amount * 2, f" ({damage_type} damage doubled: vulnerable)"


def weapon_damage_type(weapon_name: str | None) -> str | None:
    return WEAPON_DAMAGE_TYPE.get((weapon_name or "").strip().lower())


def narrator_note(combatant: Mapping[str, Any]) -> str:
    """Engine facts about an exact creature's traits, for the narrator's roster ("" when none)."""
    stats = stats_of(combatant)
    if stats is None:
        return ""
    parts: list[str] = []
    if stats.multiattack:
        parts.append(
            "multiattack: " + ", ".join(f"{name} x{count}" for name, count in stats.multiattack)
        )
    if stats.regeneration is not None:
        text = f"regenerates {stats.regeneration.amount} HP at the start of its turn"
        if stats.regeneration.suppressed_by:
            text += (
                " unless it took "
                + " or ".join(stats.regeneration.suppressed_by)
                + " damage since its last turn"
            )
        parts.append(text)
    if stats.legendary:
        options = ", ".join(f"{o.name} (costs {o.cost})" for o in stats.legendary)
        parts.append(
            f"{stats.legendary_actions} legendary actions, at most one option at the end of each "
            f"other creature's turn, regained at the start of its own: {options}"
        )
    spent = set(combatant.get("recharge_spent") or ())
    for attack in stats.attacks:
        if (
            attack.recharge is not None
        ):  # EDG-043: a recharge attack is ready or spent, by engine state
            state = "spent until its d6 recharge" if attack.name in spent else "ready"
            parts.append(f"{attack.name} recharges on {attack.recharge}-6 ({state})")
    return "".join(f"; {part}" for part in parts)


def declared_line(combatant: Mapping[str, Any]) -> str | None:
    """The combat-start line naming what this creature's record declares but the engine does not run."""
    stats = stats_of(combatant)
    if stats is None or not stats.declared:
        return None
    return f"Declared, not executed for {stats.name}: " + "; ".join(stats.declared) + "."
