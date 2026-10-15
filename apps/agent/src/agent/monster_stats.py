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
a poisoned rider on a hit whose wording is read exactly (`AttackRider`), and a Recharge X-6 attack
(`MonsterAttack.recharge`), rolled at the start of the creature's turns once spent. The engine
decides every roll.

Declared, never implied: speed and movement, legendary, lair and reaction actions, spellcasting,
other recharge abilities (breath and area saves), spell attacks, other damage riders and every
other trait. Each one is listed in `MonsterStats.declared` and shown in the combat start message
and the roster.

Parsing is conservative: when a clause cannot be read exactly it is declared rather than guessed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

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
    "club": "bludgeoning", "dagger": "piercing", "quarterstaff": "bludgeoning",
    "mace": "bludgeoning", "spear": "piercing", "handaxe": "slashing", "javelin": "piercing",
    "light hammer": "bludgeoning", "sickle": "slashing", "shortsword": "piercing",
    "scimitar": "slashing", "rapier": "piercing", "whip": "slashing", "longsword": "slashing",
    "battleaxe": "slashing", "warhammer": "bludgeoning", "morningstar": "piercing",
    "flail": "bludgeoning", "trident": "piercing", "war pick": "piercing",
    "greatsword": "slashing", "greataxe": "slashing", "maul": "bludgeoning", "glaive": "slashing",
    "halberd": "slashing", "pike": "piercing", "lance": "piercing", "shortbow": "piercing",
    "longbow": "piercing", "hand crossbow": "piercing", "heavy crossbow": "piercing",
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
_ENTRY = re.compile(r"^([A-Z][\w'’ ()/,-]{0,70}?)\.\s+(\S.*)$")
_ATTACK = re.compile(
    r"^(Melee|Ranged) Weapon Attack:\s*\+(-?\d+) to hit\b.*?\bHit:\s*(\d+)\s*\(([^)]*)\)\s*"
    r"([A-Za-z]+) damage\b(.*)$",
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
# "Recharge 5-6" (or "Recharge 6") in an ability name. Recharge after a rest is not a d6 recharge.
_RECHARGE_NAME = re.compile(r"\s*\(Recharge\s*(?:([2-5])\s*[-–]\s*6|(6))\)\s*$", re.IGNORECASE)


class MonsterStatsError(ValueError):
    """A creature record has no stat block the engine can read exactly."""


@dataclass(frozen=True)
class AttackRider:
    """A condition the target of a hit must save against (EDG-043). Only exact wording is read.

    `poisoned`: a Constitution save at `dc`, failure poisons the target for one minute, and the
    target repeats the save at the end of each of its turns (the repeat ends the effect on success).
    """

    kind: str
    dc: int
    ability: str
    duration_rounds: int


@dataclass(frozen=True)
class MonsterAttack:
    name: str
    bonus: int
    damage_dice: str
    damage_type: str
    ranged: bool = False
    recharge: int | None = None  # "Recharge N-6": the d6 result that restores the attack (N)
    rider: AttackRider | None = None


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
    """A stored attack. Rows written before riders and recharge existed read with neither."""
    rider = raw.get("rider")
    return MonsterAttack(
        name=str(raw["name"]),
        bonus=int(raw["bonus"]),
        damage_dice=str(raw["damage_dice"]),
        damage_type=str(raw["damage_type"]),
        ranged=bool(raw.get("ranged", False)),
        recharge=None if raw.get("recharge") is None else int(raw["recharge"]),
        rider=None if not isinstance(rider, Mapping) else AttackRider(**rider),
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
        m.group(1).upper(): int(m.group(2))
        for m in re.finditer(r"\b(STR|DEX|CON|INT|WIS|CHA)\s+\d+\s*\(([+-]?\d+)\)", text, re.I)
    }
    if len(labelled) == len(ABILITIES):
        return {a: labelled[a] for a in ABILITIES}
    # Unlabelled ability row: the header "STR DEX CON INT WIS CHA" precedes "18 (+4) 13 (+1) ...".
    for index, line in enumerate(lines[:-1]):
        if re.fullmatch(r"STR\s+DEX\s+CON\s+INT\s+WIS\s+CHA", line.strip(), re.IGNORECASE):
            pairs = re.findall(r"(\d+)\s*\(([+-]?\d+)\)", lines[index + 1])
            if len(pairs) == len(ABILITIES):
                return {a: int(mod) for a, (_score, mod) in zip(ABILITIES, pairs, strict=True)}
    return {}


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
    rules: list[DamageRule] = []
    declared: list[str] = []
    for clause in line.split(";"):
        words = re.findall(r"[a-z]+", clause.lower())
        types = [w for w in words if w in DAMAGE_TYPES]
        if not types:
            continue
        if "nonmagical" in words:
            rules += [DamageRule(t, nonmagical_only=True) for t in types]
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


def _attack(body: str, name: str) -> tuple[MonsterAttack | None, str | None]:
    match = _ATTACK.match(body)
    if not match:
        return None, None
    kind, bonus, average, dice_text, damage_type, rest = match.groups()
    dice = re.sub(r"\s+", "", dice_text)
    damage_type = damage_type.lower()
    if not re.fullmatch(r"\d+d\d+(?:[+-]\d+)?", dice) or damage_type not in DAMAGE_TYPES:
        return None, None
    parsed = parse_dice(dice)
    expected = math.floor(parsed.count * (parsed.sides + 1) / 2 + parsed.modifier)
    if expected != int(average):
        return None, None  # the stated average must agree with the stated dice
    poison = _POISON_RIDER.match(rest)
    if poison:
        rider = AttackRider("poisoned", int(poison.group(1)), "CON", duration_rounds=10)
        return MonsterAttack(
            name, int(bonus), dice, damage_type, kind.lower() == "ranged", None, rider
        ), None
    declared = f"{name}: damage rider or extra effect not executed" if rest.strip(" .") else None
    attack = MonsterAttack(name, int(bonus), dice, damage_type, ranged=kind.lower() == "ranged")
    return attack, declared


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
    if pairs:
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
    return tuple(counts.items())


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
            continue
        if _STAT_LINE.match(line):
            current = None
            continue
        match = _ENTRY.match(line)
        if match:
            current = [section, match.group(1).strip(), match.group(2).strip()]
            entries.append(current)
        elif current is not None:
            current[2] += " " + line

    attacks: list[MonsterAttack] = []
    multiattack_body: str | None = None
    regeneration: Regeneration | None = None
    for entry_section, entry_name, body in entries:
        lowered = entry_name.lower()
        recharge = _RECHARGE_NAME.search(entry_name)
        if (
            recharge
            and entry_section == "actions"
            and re.match(r"(?:Melee|Ranged) Weapon Attack:", body)
        ):
            # An attack that recharges (EDG-043): executed once its attack text reads exactly.
            attack, rider = _attack(body, entry_name[: recharge.start()].strip())
            if attack is None:
                declared.append(
                    f"{entry_name}: recharge ability not executed (attack text not exact)"
                )
            else:
                n = int(recharge.group(1) or recharge.group(2))
                attacks.append(replace(attack, recharge=n))
                if rider:
                    declared.append(rider)
        elif "recharge" in lowered:
            declared.append(f"{entry_name}: recharge ability not executed")
        elif entry_section in ("legendary", "lair", "reactions", "bonus"):
            declared.append(f"{entry_name}: {entry_section} action not executed")
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
        elif entry_section == "actions" and re.match(r"(?:Melee|Ranged) Weapon Attack:", body):
            attack, rider = _attack(body, entry_name)
            if attack is None:
                declared.append(f"{entry_name}: attack could not be read exactly")
            else:
                attacks.append(attack)
                if rider:
                    declared.append(rider)
        elif lowered == "spellcasting" or "Spell Attack" in body[:80]:
            declared.append(f"{entry_name}: spellcasting is not executed")
        else:
            declared.append(f"{entry_name}: trait not executed")

    multiattack: tuple[tuple[str, int], ...] = ()
    if multiattack_body is not None:
        counted = _multiattack(multiattack_body, tuple(attacks))
        if counted is None:
            declared.append("Multiattack: counts could not be read exactly; one attack is used")
        else:
            multiattack = counted

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

    `spent` names recharge attacks used and not yet recharged: they are left out of the plan. Without
    a multiattack, a ready recharge attack is used first (its use is then spent), else the first
    attack that does not recharge.
    """
    spent_set = set(spent)
    if stats.multiattack:
        by_name = {a.name: a for a in stats.attacks}
        return tuple(
            by_name[name]
            for name, count in stats.multiattack
            for _ in range(count)
            if by_name[name].recharge is None or name not in spent_set
        )
    ready = [a for a in stats.attacks if a.recharge is not None and a.name not in spent_set]
    if ready:
        return (ready[0],)
    return tuple(a for a in stats.attacks if a.recharge is None)[:1]


def recharge_turn_start(combatant: dict) -> list[tuple[str, DiceOutcome, bool]]:
    """Roll the d6 for each spent recharge attack at the start of this creature's turn (EDG-043).

    Returns (attack name, the engine's d6 roll, recharged) for each one rolled. A recharged attack
    leaves the spent list; one that did not recharge stays spent.
    """
    stats = stats_of(combatant)
    spent = [str(n) for n in combatant.get("recharge_spent") or ()]
    if stats is None or not spent:
        return []
    results: list[tuple[str, DiceOutcome, bool]] = []
    still: list[str] = []
    for name in spent:
        attack = next((a for a in stats.attacks if a.name == name and a.recharge is not None), None)
        if attack is None:  # a stored name with no recharge attack behind it: nothing to restore
            continue
        outcome = roll("1d6")
        recharged = outcome.total >= attack.recharge
        results.append((name, outcome, recharged))
        if not recharged:
            still.append(name)
    combatant["recharge_spent"] = still
    return results


def spend_recharge(combatant: dict, attack: MonsterAttack) -> None:
    """Record that a recharge attack was used, so it waits for its recharge roll."""
    if attack.recharge is None:
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
