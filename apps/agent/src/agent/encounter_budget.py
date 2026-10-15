"""Deterministic solo encounter budget (EDG-148).

The budget is owned by code and derived only from the hero's level, so the same level always
gets the same limits and no RNG is involved. It is applied once, when a new roster is built;
legacy combat rows are never re-scaled.

Scale: `m(level) = 1 + (level - 1) / 4`, so level 1 is 1.0 and level 20 is 5.75. Integer
form `(base * (level + 3) + 2) // 4` rounds half up. Level-1 values are the bands the
encounter-builder prompt has always promised: a lone boss of 18-30 HP and mooks of 5-11 HP.

Limits per roster:
  * foes: at most `min(4, 2 + (level - 1) // 4)`.
  * HP per foe: boss `[scale(18), scale(30)]`, other foes `[scale(5), scale(11)]`.
  * total enemy HP: boss ceiling + (max foes - 1) * other-foe ceiling.
  * attack bonus: at most `5 + (level - 1) // 4`.
  * average damage per hit (before critical doubling): at most `scale(5)`.

Enforcement is deterministic and never empties the roster:
  * Foes beyond the count limit, or that would push total HP over budget, are LEFT OUT. Bosses
    are kept first, then the builder's order. The first kept foe is never removed.
  * Numbers outside a range are CLAMPED into it. Damage is trimmed by dropping extra dice,
    then smaller die sizes, then the modifier, until the average fits.
  * Names, `creature_id`, `source_reference` and every other field are never changed. Exact
    creature records keep their identity. The numbers a builder proposed for them are still
    bounded, because the record text is not parsed here (replacing them with record-parsed
    stats is EDG-004's scope).
  * Armor class is not budgeted.

Every adjustment is recorded as an engine-written note. The combat start message shows the
notes, and `combat_state["solo_budget"]` persists them for the HUD.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tools.dice import parse_dice

MIN_LEVEL = 1
MAX_LEVEL = 20
MAX_FOES_CAP = 4
_BOSS_HP = (18, 30)
_FOE_HP = (5, 11)
_BASE_AVG_DAMAGE = 5
_DIE_LADDER = (4, 6, 8, 10, 12)


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
    max_foes = min(MAX_FOES_CAP, 2 + (lvl - 1) // 4)
    return SoloBudget(
        level=lvl,
        max_foes=max_foes,
        boss_hp=boss_hp,
        foe_hp=foe_hp,
        total_hp=boss_hp[1] + (max_foes - 1) * foe_hp[1],
        attack_bonus_max=5 + (lvl - 1) // 4,
        damage_avg_max=_scale(_BASE_AVG_DAMAGE, lvl),
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


def _hp_band(enemy: dict, budget: SoloBudget) -> tuple[int, int]:
    return budget.boss_hp if enemy.get("is_boss") else budget.foe_hp


def _clamp_foe(enemy: dict, budget: SoloBudget, notes: list[str]) -> dict:
    name = enemy.get("name", "?")
    out = dict(enemy)
    lo, hi = _hp_band(enemy, budget)
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


def apply_solo_budget(enemies: list[dict], level: Any) -> tuple[list[dict], dict]:
    """Return (roster within budget, summary). Pure: the input list and dicts are not mutated.

    The summary is `{"level", "adapted", "notes"}`, ready to persist and show the player.
    """
    budget = solo_budget(level)
    notes: list[str] = []
    if not enemies:
        return [], _summary(budget, notes)

    # Bosses are kept first so a count or HP trim never removes the climactic foe.
    priority = sorted(range(len(enemies)), key=lambda i: (not enemies[i].get("is_boss"), i))
    kept: set[int] = set()
    total = 0
    for i in priority:
        foe = enemies[i]
        lo, hi = _hp_band(foe, budget)
        hp = max(lo, min(hi, int(foe.get("hp_max", lo))))  # the HP this foe will have once clamped
        if len(kept) >= budget.max_foes:
            notes.append(f"{foe.get('name', '?')} left out (solo limit of {budget.max_foes} foes)")
        elif kept and total + hp > budget.total_hp:
            notes.append(
                f"{foe.get('name', '?')} left out (solo total enemy HP limit of {budget.total_hp})"
            )
        else:
            kept.add(i)
            total += hp

    roster = [_clamp_foe(enemies[i], budget, notes) for i in sorted(kept)]
    return roster, _summary(budget, notes)


def _summary(budget: SoloBudget, notes: list[str]) -> dict:
    return {"level": budget.level, "adapted": bool(notes), "notes": list(notes)}


def adaptation_sentence(summary: dict) -> str:
    """Engine-written player-facing line for an adapted roster, or '' when nothing changed."""
    if not summary.get("adapted"):
        return ""
    return (
        f"Encounter adapted for a solo level {summary['level']} hero: "
        + "; ".join(summary["notes"])
        + "."
    )
