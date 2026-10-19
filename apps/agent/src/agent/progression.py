"""Explicit campaign modes and milestone advancement (EDG-045).

Two campaign settings, both explicit in state (`campaigns.progression_mode`, `campaigns.death_mode`):

  * progression_mode 'fixed' (the default for every existing campaign): the sheet keeps its level.
  * progression_mode 'milestone': the engine grants one milestone for each encounter the hero wins
    (see `milestone_granted`). A milestone is spent by an explicit level-up. Advancement stops at
    `MAX_ADVANCED_LEVEL`, the highest level whose tables the engine verifies for the supported
    classes (PHB, PDF pp.58, 72, 91, 96, 114). Level 1-5 covers every supported class feature the
    engine executes; Extra Attack, subclass features and Ability Score Improvement beyond the
    listed choices are declared in `agent.supported_rules`, not executed.

What a level-up changes, all engine-owned:
  * hit point maximum: the class hit die's fixed value (`floor(die / 2) + 1`, the PHB "or" value
    listed on each class table) or an engine roll of the die, plus the Constitution modifier. A
    gain never goes below zero. Current HP rises by the same amount.
  * level, and with it the proficiency bonus, Hit Dice total, spell slot totals and the
    level-gated class features (Action Surge at 2, Sneak Attack dice, Rage uses, ...).
  * an Ability Score Improvement at level 4 (PHB, PDF p.73): two points to one score, or one point
    to each of two scores, each score capped at 20.

Nothing here reads a model or database. The API service applies a plan inside one transaction.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agent.resolution import (
    _ABILITY_ALIASES,
    ABILITIES,
    HIT_DIE,
    ability_mod,
    ability_scores,
    character_class,
)
from tools.dice import DiceOutcome, roll

PROGRESSION_FIXED = "fixed"
PROGRESSION_MILESTONE = "milestone"
PROGRESSION_MODES: tuple[str, ...] = (PROGRESSION_FIXED, PROGRESSION_MILESTONE)
DEFAULT_PROGRESSION_MODE = PROGRESSION_FIXED

DEATH_SUBDUED = "subdued"
DEATH_SAVES = "death_saves"
DEATH_MODES: tuple[str, ...] = (DEATH_SUBDUED, DEATH_SAVES)
DEFAULT_DEATH_MODE = DEATH_SUBDUED

# The highest level milestone advancement reaches (see the module docstring).
MAX_ADVANCED_LEVEL = 5
# The level at which the Ability Score Improvement choice is offered (PHB, PDF p.73).
ASI_LEVEL = 4
ASI_POINTS = 2
ASI_MAX_SCORE = 20

HP_METHODS: tuple[str, ...] = ("average", "roll")
DEFAULT_HP_METHOD = "average"


class ProgressionError(ValueError):
    """A level-up the rules or the campaign mode do not allow. The message is player-facing."""


# Features the class gains at each level, from the class tables (PHB, PDF pp.58, 72, 91, 96, 114).
# `executed` is True only where the engine runs the feature: the rest are named so a level-up can
# say what the hero gained, and are declared as not executed in the supported-scope contract.
@dataclass(frozen=True)
class LevelFeature:
    name: str
    executed: bool


_FEATURES: dict[str, dict[int, tuple[LevelFeature, ...]]] = {
    "fighter": {
        2: (LevelFeature("Action Surge", True),),
        3: (LevelFeature("Martial Archetype", False),),
        4: (LevelFeature("Ability Score Improvement", True),),
        5: (LevelFeature("Extra Attack", False),),
    },
    "rogue": {
        2: (LevelFeature("Cunning Action", False),),
        3: (LevelFeature("Roguish Archetype", False), LevelFeature("Sneak Attack (2d6)", True)),
        4: (LevelFeature("Ability Score Improvement", True),),
        5: (LevelFeature("Uncanny Dodge", False), LevelFeature("Sneak Attack (3d6)", True)),
    },
    "wizard": {
        2: (LevelFeature("Arcane Tradition", False),),
        4: (LevelFeature("Ability Score Improvement", True),),
    },
    "cleric": {
        2: (LevelFeature("Channel Divinity", False),),
        4: (LevelFeature("Ability Score Improvement", True),),
        5: (LevelFeature("Destroy Undead", False),),
    },
    "ranger": {
        2: (LevelFeature("Fighting Style", False), LevelFeature("Spellcasting", True)),
        3: (LevelFeature("Ranger Archetype", False),),
        4: (LevelFeature("Ability Score Improvement", True),),
        5: (LevelFeature("Extra Attack", False),),
    },
}
# Spell slot totals change with the level for the casters; the slot tables live in agent.spells
# (verified in docs/rules-verification-2014.md), so the feature list only names the spellcasting.
SUPPORTED_CLASSES = ("fighter", "rogue", "wizard", "cleric", "ranger")


def ability_name(raw: object) -> str | None:
    """The canonical ability (STR ... CHA) for a player's word such as "str" or "strength"."""
    return _ABILITY_ALIASES.get(str(raw).strip().lower())


def is_supported_class(character: dict | None) -> bool:
    return character_class(character) in SUPPORTED_CLASSES


def hit_die_fixed_value(sides: int) -> int:
    """The class table's "or" value for a hit die: `floor(die / 2) + 1` (1d10 -> 6, 1d8 -> 5)."""
    return sides // 2 + 1


def hit_die_sides(character_cls: str) -> int:
    return HIT_DIE.get(character_cls, 8)


@dataclass(frozen=True)
class HitPointGain:
    amount: int
    method: str
    dice: DiceOutcome | None  # the engine roll when method == "roll"; None for the fixed value


def hit_point_gain(
    character: dict | None, method: str = DEFAULT_HP_METHOD, rng: random.Random | None = None
) -> HitPointGain:
    """The hit point maximum a level adds: fixed value or engine roll, plus the CON modifier."""
    if method not in HP_METHODS:
        raise ProgressionError(f"hp_method must be one of {', '.join(HP_METHODS)}.")
    sides = hit_die_sides(character_class(character))
    con = ability_mod(_score(character, "CON"))
    if method == "roll":
        outcome = roll(f"1d{sides}", rng)
        return HitPointGain(max(0, outcome.total + con), method, outcome)
    return HitPointGain(max(0, hit_die_fixed_value(sides) + con), method, None)


def _score(character: dict | None, ability: str) -> int:
    return ability_scores(character).get(ability, 10)


def level_features(character_cls: str, level: int) -> list[dict[str, object]]:
    """The features the class gains at `level`, each with whether the engine executes it."""
    rows = _FEATURES.get(character_cls, {}).get(level, ())
    return [{"name": f.name, "executed": f.executed} for f in rows]


def can_advance(level: int, milestones_pending: int) -> bool:
    """A level-up needs an unspent milestone and a level below the advancement cap."""
    return milestones_pending >= 1 and level < MAX_ADVANCED_LEVEL


def milestone_granted(level: int, milestones_pending: int) -> bool:
    """Whether a won encounter grants a milestone: the hero may not hold more than the cap needs."""
    return level + milestones_pending < MAX_ADVANCED_LEVEL


def validate_asi(
    level_after: int, increases: dict[str, int] | None, scores: dict[str, int]
) -> dict[str, int]:
    """Check an Ability Score Improvement choice: only at ASI_LEVEL, two points, no score above 20.

    Returns the normalised {ABILITY: points} increases. An empty choice is refused at ASI_LEVEL.
    """
    if level_after != ASI_LEVEL:
        if increases:
            raise ProgressionError(
                f"An ability score improvement is only taken at level {ASI_LEVEL}."
            )
        return {}
    if not increases:
        raise ProgressionError(
            f"Choose {ASI_POINTS} points of ability score improvement at level {ASI_LEVEL}."
        )
    cleaned: dict[str, int] = {}
    for key, points in increases.items():
        ability = _ABILITY_ALIASES.get(str(key).strip().lower())
        if ability not in ABILITIES:
            raise ProgressionError(f"{key} is not an ability score.")
        if isinstance(points, bool) or not isinstance(points, int) or points < 1 or points > 2:
            raise ProgressionError("Each ability score improvement is 1 or 2 points.")
        cleaned[ability] = cleaned.get(ability, 0) + points
    if sum(cleaned.values()) != ASI_POINTS or max(cleaned.values()) > ASI_POINTS:
        raise ProgressionError(
            f"An ability score improvement is {ASI_POINTS} points: +2 to one score or +1 to two."
        )
    for ability, points in cleaned.items():
        if scores.get(ability, 10) + points > ASI_MAX_SCORE:
            raise ProgressionError(f"{ability} cannot rise above {ASI_MAX_SCORE}.")
    return cleaned


@dataclass(frozen=True)
class LevelUpPlan:
    """What one level-up does. Built by `plan_level_up` and applied atomically by the API."""

    from_level: int
    to_level: int
    hit_points: HitPointGain
    asi: dict[str, int] = field(default_factory=dict)
    features: list[dict[str, object]] = field(default_factory=list)


def plan_level_up(
    character: dict | None,
    *,
    from_level: int,
    milestones_pending: int,
    hp_method: str = DEFAULT_HP_METHOD,
    asi: dict[str, int] | None = None,
    rng: random.Random | None = None,
) -> LevelUpPlan:
    """Decide a level-up without changing anything. Raises ProgressionError when it is not allowed."""
    if not is_supported_class(character):
        raise ProgressionError("Level-up is offered only for the supported classes.")
    if from_level != int(character.get("level") or 1):
        raise ProgressionError("The sheet's level has changed. Reload the sheet and try again.")
    if milestones_pending < 1:
        raise ProgressionError("No milestone is waiting to be spent.")
    if from_level >= MAX_ADVANCED_LEVEL:
        raise ProgressionError(f"Milestone advancement stops at level {MAX_ADVANCED_LEVEL}.")
    to_level = from_level + 1
    gain = hit_point_gain(character, hp_method, rng)
    chosen = validate_asi(to_level, asi, _ability_map(character))
    return LevelUpPlan(
        from_level=from_level,
        to_level=to_level,
        hit_points=gain,
        asi=chosen,
        features=level_features(character_class(character), to_level),
    )


def _ability_map(character: dict | None) -> dict[str, int]:
    return ability_scores(character)


def apply_asi_to_stats(stats: dict | None, increases: dict[str, int]) -> dict:
    """The sheet's `stats` with each ability raised. Existing key spellings are kept."""
    out = dict(stats or {})
    for ability, points in increases.items():
        key = next(
            (k for k in out if _ABILITY_ALIASES.get(str(k).strip().lower()) == ability),
            ability,
        )
        try:
            current = int(out.get(key, 10))
        except (TypeError, ValueError):
            current = 10
        out[key] = min(ASI_MAX_SCORE, current + points)
    return out
