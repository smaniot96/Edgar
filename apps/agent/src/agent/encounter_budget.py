"""Deterministic solo encounter budget (EDG-148, calibrated against the 2014 DMG in EDG-153).

The budget is owned by code and derived only from the hero's level, so the same level always gets
the same limits and no RNG is involved. It is applied once, when a new roster is built; legacy combat
rows are never re-scaled.

Per-level limits (`_LIMITS`, read through `solo_budget`):
  * foes: at most `min(4, 2 + (level - 1) // 4)`.
  * HP per foe: the first generic boss `[scale(18), scale(30)]`, other foes `[scale(5), scale(11)]`,
    where `scale(b, level) = (b * (level + 3) + 2) // 4` (level 1 is the prompt-promised band).
  * attack bonus and average damage per hit (before critical doubling): a per-level ceiling.

Why the ceilings are what they are (docs/rules-verification-2014.md, section 6): the maximum generic
roster is one boss at its HP ceiling plus the other foes at theirs, every foe at the attack and damage
ceilings. Its adjusted XP for ONE hero, using the 2014 DMG thresholds, the monster-count multiplier for
a party of fewer than three (`dmg_thresholds.single_hero_multiplier`) and the DMG's CR tables, must not
exceed the hero's deadly threshold. The ceilings are the highest pairs found that keep both attack and
damage non-decreasing across levels. Level 1 keeps the prompt's HP bands and two-foe count and lowers
the attack bonus to +4 and the damage to 3 per hit, because the level-1 deadly threshold is only 100 XP.
A test recomputes this worst case for all 20 levels from the DMG tables.

Enforcement is deterministic and never empties the roster:
  * Exact named actors are kept first, including when their number exceeds the ordinary count
    limit. Other foes beyond the count limit, or that would push total HP over budget, are LEFT
    OUT. Bosses precede other optional foes, followed by the builder's order.
  * Only the first generic boss takes the boss HP band; a later generic boss takes the foe band.
  * Numbers outside a range are CLAMPED into it. Damage is trimmed by dropping extra dice, then
    smaller die sizes, then the modifier, until the average fits. Generic foes only.
  * An exact creature (one with a `creature_id`) is never rescaled (EDG-004): its HP, AC, attacks
    and traits come from its verified record. The budget limits only optional foe counts, and
    notes ("unscaled exact creature: ...") any exact foe whose record exceeds a per-foe band.
  * Names, `creature_id`, `source_reference` and every other field are never changed.
  * Armor class is not budgeted.

The XP check (`summary["xp_check"]`) classifies the kept roster for ONE hero with the DMG
thresholds. Exact foes count their printed XP (`challenge_xp`, read from the record); generic foes
count the XP their clamped numbers imply. A roster at or below the deadly threshold is informational.
Above it, the summary carries a warning:
  * `above_deadly`: adjusted XP above the deadly threshold but below twice it;
  * `far_above_deadly`: adjusted XP at least twice the deadly threshold.
A warning never refuses the fight and never changes a creature. An exact foe with no readable XP
makes the check unknown (`xp_check` is None), and no warning is given.

Every adjustment is recorded as an engine-written note. The combat start message shows the notes and
any warning, and `combat_state["solo_budget"]` persists the summary for the HUD. An unscaled note is
information, not an adaptation, so it does not mark the encounter as adapted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.dmg_thresholds import (
    adjusted_xp,
    deadly_threshold,
    difficulty,
    implied_xp,
    single_hero_multiplier,
)
from tools.dice import parse_dice

MIN_LEVEL = 1
MAX_LEVEL = 20
_BOSS_HP = (18, 30)
_FOE_HP = (5, 11)
_BASE_AVG_DAMAGE = 5
_DIE_LADDER = (4, 6, 8, 10, 12)
TIER_ABOVE_DEADLY = "above_deadly"
TIER_FAR_ABOVE_DEADLY = "far_above_deadly"

# level: (max foes, attack bonus ceiling, average damage per hit ceiling). Checked by the tests.
_LIMITS: dict[int, tuple[int, int, int]] = {
    1: (2, 4, 3),
    2: (2, 4, 5),
    3: (2, 4, 8),
    4: (2, 4, 8),
    5: (3, 6, 8),
    6: (3, 6, 8),
    7: (3, 6, 8),
    8: (3, 6, 8),
    9: (4, 6, 8),
    10: (4, 6, 14),
    11: (4, 6, 14),
    12: (4, 6, 14),
    13: (4, 6, 14),
    14: (4, 6, 14),
    15: (4, 8, 14),
    16: (4, 8, 14),
    17: (4, 8, 20),
    18: (4, 8, 20),
    19: (4, 8, 20),
    20: (4, 9, 29),
}


def clamp_level(level: Any) -> int:
    try:
        value = int(level or MIN_LEVEL)
    except (TypeError, ValueError):
        value = MIN_LEVEL
    return max(MIN_LEVEL, min(MAX_LEVEL, value))


def _scale(base: int, level: int) -> int:
    return (base * (level + 3) + 2) // 4


@dataclass(frozen=True)
class SoloBudget:
    level: int
    max_foes: int
    boss_hp: tuple[int, int]
    foe_hp: tuple[int, int]
    total_hp: int
    attack_bonus_max: int
    damage_avg_max: int

    def describe(self) -> str:
        return (
            f"at most {self.max_foes} foes; boss HP {self.boss_hp[0]}-{self.boss_hp[1]}; "
            f"other foes HP {self.foe_hp[0]}-{self.foe_hp[1]}; total enemy HP at most "
            f"{self.total_hp}; attack bonus at most +{self.attack_bonus_max}; average damage "
            f"per hit at most {self.damage_avg_max}"
        )


def solo_budget(level: Any) -> SoloBudget:
    lvl = clamp_level(level)
    boss_hp = (_scale(_BOSS_HP[0], lvl), _scale(_BOSS_HP[1], lvl))
    foe_hp = (_scale(_FOE_HP[0], lvl), _scale(_FOE_HP[1], lvl))
    max_foes, attack_max, damage_max = _LIMITS[lvl]
    return SoloBudget(
        level=lvl,
        max_foes=max_foes,
        boss_hp=boss_hp,
        foe_hp=foe_hp,
        total_hp=boss_hp[1] + (max_foes - 1) * foe_hp[1],
        attack_bonus_max=attack_max,
        damage_avg_max=damage_max,
    )


def _avg(count: int, sides: int, modifier: int) -> float:
    return count * (sides + 1) / 2 + modifier


def clamp_damage(expr: str, avg_max: int) -> str:
    """Bring a damage expression's average to `avg_max` or below, deterministically."""
    try:
        parsed = parse_dice(str(expr))
    except ValueError:
        return expr
    count, sides, modifier = parsed.count, parsed.sides, parsed.modifier
    while count > 1 and _avg(count, sides, modifier) > avg_max:
        count -= 1
    while sides > _DIE_LADDER[0] and _avg(count, sides, modifier) > avg_max:
        sides = max(s for s in _DIE_LADDER if s < sides)
    while modifier > -3 and _avg(count, sides, modifier) > avg_max:
        modifier -= 1
    return f"{count}d{sides}{modifier:+d}" if modifier else f"{count}d{sides}"


UNSCALED_PREFIX = "unscaled exact creature"


def is_exact(enemy: dict) -> bool:
    """An exact creature: a roster entry that names a verified creature record."""
    return bool(enemy.get("creature_id"))


def _named_actor_slot(enemy: dict) -> tuple | None:
    """Only a verified creature with an explicit canonical actor binding owns a required slot."""
    source = enemy.get("source_reference")
    if (not is_exact(enemy) or not isinstance(source, dict)
            or not isinstance(source.get("actor_id"), str) or not source["actor_id"]):
        return None
    return tuple(source.get(key) for key in
                 ("actor_collection", "actor_manifest_sha256", "actor_source_sha256", "actor_id"))


def _clamp_foe(enemy: dict, budget: SoloBudget, notes: list[str], *, boss: bool) -> dict:
    name = enemy.get("name", "?")
    out = dict(enemy)
    lo, hi = budget.boss_hp if boss else budget.foe_hp
    hp = int(enemy.get("hp_max", lo))
    if not lo <= hp <= hi:
        new_hp = max(lo, min(hi, hp))
        notes.append(f"{name} HP {hp} -> {new_hp}")
        out["hp_max"] = out["hp_current"] = new_hp
    bonus = int(enemy.get("attack_bonus", 0))
    if bonus > budget.attack_bonus_max:
        notes.append(f"{name} attack +{bonus} -> +{budget.attack_bonus_max}")
        out["attack_bonus"] = budget.attack_bonus_max
    dice = str(enemy.get("damage_dice", ""))
    fitted = clamp_damage(dice, budget.damage_avg_max)
    if fitted != dice:
        notes.append(f"{name} damage {dice} -> {fitted}")
        out["damage_dice"] = fitted
    return out


def _unscaled(enemy: dict, budget: SoloBudget, notes: list[str]) -> dict:
    """An exact creature keeps its record's numbers; only an out-of-band value is noted."""
    name = enemy.get("name", "?")
    lo, hi = budget.boss_hp if enemy.get("is_boss") else budget.foe_hp
    hp = int(enemy.get("hp_max", lo))
    if not lo <= hp <= hi:
        notes.append(f"{UNSCALED_PREFIX}: {name} keeps its record HP {hp} (solo band {lo}-{hi})")
    bonus = int(enemy.get("attack_bonus", 0))
    if bonus > budget.attack_bonus_max:
        notes.append(
            f"{UNSCALED_PREFIX}: {name} keeps its record attack +{bonus} "
            f"(solo limit +{budget.attack_bonus_max})"
        )
    dice = str(enemy.get("damage_dice", ""))
    if dice and clamp_damage(dice, budget.damage_avg_max) != dice:
        notes.append(
            f"{UNSCALED_PREFIX}: {name} keeps its record damage {dice} "
            f"(solo average limit {budget.damage_avg_max})"
        )
    return dict(enemy)


def _generic_xp(enemy: dict) -> int | None:
    """XP a generic foe implies from its clamped numbers (DMG CR tables); None if unreadable."""
    try:
        parsed = parse_dice(str(enemy.get("damage_dice", "")))
    except ValueError:
        return None
    return implied_xp(
        int(enemy.get("hp_max", 1)),
        _avg(parsed.count, parsed.sides, parsed.modifier),
        int(enemy.get("attack_bonus", 0)),
    )


def xp_check(kept: list[dict], level: int) -> dict | None:
    """Classify the kept roster for ONE hero (DMG p.82). None when an exact foe's XP is unreadable."""
    if not kept:
        return None
    monster_xp: list[int] = []
    exact_foes: list[dict] = []
    for foe in kept:
        if is_exact(foe):
            if foe.get("challenge_xp") is None:
                return None
            xp = int(foe["challenge_xp"])
            exact_foes.append(
                {"name": foe.get("name", "?"), "cr": foe.get("challenge_cr"), "xp": xp}
            )
        else:
            xp = _generic_xp(foe)
            if xp is None:
                return None
        monster_xp.append(xp)
    adjusted = adjusted_xp(monster_xp)
    deadly = deadly_threshold(level)
    if adjusted <= deadly:
        warning = None
    elif adjusted < 2 * deadly:
        warning = TIER_ABOVE_DEADLY
    else:
        warning = TIER_FAR_ABOVE_DEADLY
    return {
        "foes": len(kept),
        "raw_xp": sum(monster_xp),
        "multiplier": float(single_hero_multiplier(len(kept))),
        "adjusted_xp": float(adjusted),
        "difficulty": difficulty(float(adjusted), level),
        "deadly_threshold": deadly,
        "exact_foes": exact_foes,
        "warning": warning,
    }


def warning_text(check: dict, level: int) -> str:
    """Player-facing warning naming the exact foes and the numbers behind the tier."""
    exact = [f"{f['name']} (CR {f['cr']}, {f['xp']} XP)" for f in check["exact_foes"]]
    generic = check["foes"] - len(exact)
    parts = list(exact)
    if generic:
        parts.append(f"{generic} budgeted foe{'s' if generic != 1 else ''}")
    who = " and ".join(parts) if len(parts) <= 2 else ", ".join(parts)
    scale = "far above" if check["warning"] == TIER_FAR_ABOVE_DEADLY else "above"
    return (
        f"Warning: this fight is {scale} a solo level {level} hero's deadly threshold of "
        f"{check['deadly_threshold']} XP. Foes: {who}. Their raw XP is {check['raw_xp']}, which "
        f"counts as {check['adjusted_xp']:g} adjusted XP (x{check['multiplier']:g} for {check['foes']} foes). "
        "Exact creature records are not rescaled and the fight is not refused. A deadly fight "
        "can be lethal for a solo hero."
    )


def party_budget(level: Any, party_size: int) -> SoloBudget:
    """The solo budget of a party fight (EDG-049): one more foe per extra hero, at most 6 in all.

    Per-foe bands and ceilings stay those of the level; the total enemy HP grows with the extra foes.
    Foe quality is never loosened, so an extra hero adds foes, not stronger ones.
    """
    base = solo_budget(level)
    foes = min(6, base.max_foes + max(0, int(party_size) - 1))
    return SoloBudget(
        level=base.level,
        max_foes=foes,
        boss_hp=base.boss_hp,
        foe_hp=base.foe_hp,
        total_hp=base.boss_hp[1] + (foes - 1) * base.foe_hp[1],
        attack_bonus_max=base.attack_bonus_max,
        damage_avg_max=base.damage_avg_max,
    )


def apply_solo_budget(
    enemies: list[dict], level: Any, *, party_size: int = 1
) -> tuple[list[dict], dict]:
    """Return (roster within budget, summary). Pure: the input list and dicts are not mutated.

    `party_size` (EDG-049) above one widens the foe count (`party_budget`); one hero is the solo budget
    exactly as before. The DMG threshold check is for one hero, so a party summary carries none.

    The summary is `{"level", "adapted", "notes", "xp_check", "warning"}`, ready to persist and show
    the player. `xp_check` and `warning` are None when there is nothing to classify.
    """
    budget = party_budget(level, party_size) if party_size > 1 else solo_budget(level)
    notes: list[str] = []
    if not enemies:
        return [], _summary(budget, notes, None)

    # Canonical participants cannot disappear during fitting. Optional populations use
    # the remaining slots; a boss label is independent of an actor's story identity.
    named_count = len({slot for enemy in enemies if (slot := _named_actor_slot(enemy)) is not None})
    if named_count > budget.max_foes:
        notes.append(f"All {named_count} named actors retained above the ordinary solo limit of {budget.max_foes} foes; exact statistics and encounter danger are preserved")
    priority = sorted(range(len(enemies)),
                      key=lambda i: (_named_actor_slot(enemies[i]) is None, not enemies[i].get("is_boss"), i))
    kept: list[int] = []
    kept_actors: set[tuple] = set()
    boss_band: dict[int, bool] = {}
    boss_band_taken = False
    generic_total = 0
    generic_kept = False
    for i in priority:
        foe = enemies[i]
        name = foe.get("name", "?")
        actor_slot = _named_actor_slot(foe)
        if actor_slot is not None and actor_slot in kept_actors:
            notes.append(f"{name} duplicate named actor left out; its canonical participant is already retained")
        elif actor_slot is not None:
            kept.append(i)
            kept_actors.add(actor_slot)
        elif len(kept) >= budget.max_foes:
            notes.append(f"{name} left out (solo limit of {budget.max_foes} foes)")
        elif is_exact(foe):
            kept.append(i)  # exact records are not rescaled, so only the count limit applies
        else:
            as_boss = bool(foe.get("is_boss")) and not boss_band_taken
            lo, hi = budget.boss_hp if as_boss else budget.foe_hp
            hp = max(lo, min(hi, int(foe.get("hp_max", lo))))  # HP once clamped
            if generic_kept and generic_total + hp > budget.total_hp:
                notes.append(f"{name} left out (solo total enemy HP limit of {budget.total_hp})")
            else:
                kept.append(i)
                boss_band[i] = as_boss
                boss_band_taken = boss_band_taken or as_boss
                generic_total += hp
                generic_kept = True

    roster = [
        _unscaled(enemies[i], budget, notes)
        if is_exact(enemies[i])
        else _clamp_foe(enemies[i], budget, notes, boss=boss_band[i])
        for i in sorted(kept)
    ]
    check = None if party_size > 1 else xp_check(roster, budget.level)
    summary = _summary(budget, notes, check)
    if party_size > 1:
        summary["party_size"] = int(party_size)
    return roster, summary


def _summary(budget: SoloBudget, notes: list[str], check: dict | None) -> dict:
    changed = [n for n in notes if not n.startswith(UNSCALED_PREFIX)]
    warning = None
    if check and check["warning"]:
        warning = {"tier": check["warning"], "text": warning_text(check, budget.level)}
    return {
        "level": budget.level,
        "adapted": bool(changed),
        "notes": list(notes),
        "xp_check": check,
        "warning": warning,
    }


def adaptation_sentence(summary: dict) -> str:
    """Engine-written player-facing text for the combat start, or '' when there is nothing to say.

    An adapted roster says so. An unscaled exact creature is named as unscaled, so the player sees
    that its record was kept, not rescaled. A warning (EDG-153) follows the notes.
    """
    parts: list[str] = []
    notes = summary.get("notes") or []
    if notes:
        if summary.get("party_size"):
            who = f"a party of {summary['party_size']} level {summary['level']} heroes"
            head = (
                f"Encounter adapted for {who}: " if summary.get("adapted") else f"{who.capitalize()}: "
            )
        else:
            head = (
                f"Encounter adapted for a solo level {summary['level']} hero: "
                if summary.get("adapted")
                else f"Solo level {summary['level']} hero: "
            )
        parts.append(head + "; ".join(notes) + ".")
    warning = summary.get("warning") or {}
    if warning.get("text"):
        parts.append(warning["text"])
    return " ".join(parts)
