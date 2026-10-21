"""2014 Dungeon Master's Guide encounter tables for a solo hero (EDG-153).

Pure data and helpers. Nothing here reads the rulebooks at runtime, and no rulebook text is copied.
Every number was checked by hand against the local, read-only 2014 DMG (data/pdfs/dm_guide.pdf,
page numbers are printed page numbers, which equal the PDF page numbers):

* XP Thresholds by Character Level, levels 1-20, Easy/Medium/Hard/Deadly: DMG p.82.
* Encounter Multipliers (1 monster x1, 2 x1.5, 3-6 x2, 7-10 x2.5, 11-14 x3, 15+ x4): DMG p.82.
* Parties of fewer than three characters move up one multiplier step (1.5 for a lone monster, 5 for
  fifteen or more): DMG p.83, "Party Size".
* Experience Points by Challenge Rating: DMG p.275.
* Monster Statistics by Challenge Rating (HP, damage per round, attack bonus): DMG p.274. Step 4
  averages the defensive and offensive challenge ratings and adjusts the offensive one by one step
  for every two points of attack bonus away from the table's value.

Difficulty follows DMG p.82, step 5: the adjusted XP is compared with the thresholds, and the closest
threshold that is LOWER than the adjusted XP sets the difficulty. An adjusted XP equal to a threshold
therefore stays in the tier below it, so "deadly" means strictly above the deadly threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from fractions import Fraction

from edgar_core.creature_xp import challenge_xp_from_stat_block

MIN_LEVEL = 1
MAX_LEVEL = 20

# DMG p.82: (easy, medium, hard, deadly) per character level.
XP_THRESHOLDS: dict[int, tuple[int, int, int, int]] = {
    1: (25, 50, 75, 100),
    2: (50, 100, 150, 200),
    3: (75, 150, 225, 400),
    4: (125, 250, 375, 500),
    5: (250, 500, 750, 1100),
    6: (300, 600, 900, 1400),
    7: (350, 750, 1100, 1700),
    8: (450, 900, 1400, 2100),
    9: (550, 1100, 1600, 2400),
    10: (600, 1200, 1900, 2800),
    11: (800, 1600, 2400, 3600),
    12: (1000, 2000, 3000, 4500),
    13: (1100, 2200, 3400, 5100),
    14: (1250, 2500, 3800, 5700),
    15: (1400, 2800, 4300, 6400),
    16: (1600, 3200, 4800, 7200),
    17: (2000, 3900, 5900, 8800),
    18: (2100, 4200, 6300, 9500),
    19: (2400, 4900, 7300, 10900),
    20: (2800, 5700, 8500, 12700),
}

# DMG p.275: Experience Points by Challenge Rating. The book gives CR 0 as 0 XP if the monster
# poses no threat and 10 XP otherwise; the engine uses 10 (the same value as edgar_core's CR table).
CR_XP: dict[str, int] = {
    "0": 10, "1/8": 25, "1/4": 50, "1/2": 100, "1": 200, "2": 450, "3": 700, "4": 1100,
    "5": 1800, "6": 2300, "7": 2900, "8": 3900, "9": 5000, "10": 5900, "11": 7200,
    "12": 8400, "13": 10000, "14": 11500, "15": 13000, "16": 15000, "17": 18000,
    "18": 20000, "19": 22000, "20": 25000, "21": 33000, "22": 41000, "23": 50000,
    "24": 62000, "25": 75000, "26": 90000, "27": 105000, "28": 120000, "29": 135000,
    "30": 155000,
}  # fmt: skip

CR_ORDER: tuple[str, ...] = ("0", "1/8", "1/4", "1/2", *(str(n) for n in range(1, 31)))

# DMG p.274: Monster Statistics by Challenge Rating. Per CR: attack bonus, HP range, and
# damage-per-round range. Armor Class and save DC are not used by the solo budget.
CR_STATS: dict[str, tuple[int, int, int, int, int]] = {
    #        attack  hp_lo  hp_hi  dmg_lo  dmg_hi
    "0": (3, 1, 6, 0, 1),
    "1/8": (3, 7, 35, 2, 3),
    "1/4": (3, 36, 49, 4, 5),
    "1/2": (3, 50, 70, 6, 8),
    "1": (3, 71, 85, 9, 14),
    "2": (3, 86, 100, 15, 20),
    "3": (4, 101, 115, 21, 26),
    "4": (5, 116, 130, 27, 32),
    "5": (6, 131, 145, 33, 38),
    "6": (6, 146, 160, 39, 44),
    "7": (6, 161, 175, 45, 50),
    "8": (7, 176, 190, 51, 56),
    "9": (7, 191, 205, 57, 62),
    "10": (7, 206, 220, 63, 68),
    "11": (8, 221, 235, 69, 74),
    "12": (8, 236, 250, 75, 80),
    "13": (8, 251, 265, 81, 86),
    "14": (8, 266, 280, 87, 92),
    "15": (8, 281, 295, 93, 98),
    "16": (9, 296, 310, 99, 104),
    "17": (10, 311, 325, 105, 110),
    "18": (10, 326, 340, 111, 116),
    "19": (10, 341, 355, 117, 122),
    "20": (10, 356, 400, 123, 140),
    "21": (11, 401, 445, 141, 158),
    "22": (11, 446, 490, 159, 176),
    "23": (11, 491, 535, 177, 194),
    "24": (12, 536, 580, 195, 212),
    "25": (12, 581, 625, 213, 230),
    "26": (12, 626, 670, 231, 248),
    "27": (13, 671, 715, 249, 266),
    "28": (13, 716, 760, 267, 284),
    "29": (13, 761, 805, 285, 302),
    "30": (14, 806, 850, 303, 320),
}

DIFFICULTIES = ("trivial", "easy", "medium", "hard", "deadly")


def clamp_level(level: int) -> int:
    return max(MIN_LEVEL, min(MAX_LEVEL, int(level)))


def thresholds(level: int) -> tuple[int, int, int, int]:
    """Easy, medium, hard and deadly XP for one hero of `level` (DMG p.82)."""
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError(f"DMG thresholds exist for levels {MIN_LEVEL}-{MAX_LEVEL}, not {level}")
    return XP_THRESHOLDS[level]


def deadly_threshold(level: int) -> int:
    return thresholds(level)[3]


def cr_xp(label: str) -> int:
    """XP of a challenge rating label such as "1/4" or "3" (DMG p.275)."""
    return CR_XP[label]


def cr_value(label: str) -> Fraction:
    return Fraction(label) if "/" in label else Fraction(int(label))


def _lowest_cr_with(top_of: dict[str, int], value: float) -> int:
    """Index of the lowest CR whose table top covers `value`; the highest CR caps the rest."""
    for index, label in enumerate(CR_ORDER):
        if value <= top_of[label]:
            return index
    return len(CR_ORDER) - 1


def defensive_cr(hp: int) -> str:
    """Challenge rating the DMG table suggests for hit points `hp` (DMG p.274, step 4)."""
    index = _lowest_cr_with({k: v[2] for k, v in CR_STATS.items()}, hp)
    return CR_ORDER[index]


def offensive_cr(damage: float, attack_bonus: int) -> str:
    """Challenge rating for a damage-per-round figure, shifted by the attack bonus (DMG p.274-275).

    The attack bonus moves the rating one step per two points it differs from the table's value.
    """
    index = _lowest_cr_with({k: v[4] for k, v in CR_STATS.items()}, damage)
    gap = attack_bonus - CR_STATS[CR_ORDER[index]][0]
    if abs(gap) >= 2:
        steps = int(abs(gap) // 2) * (1 if gap > 0 else -1)
        index = max(0, min(len(CR_ORDER) - 1, index + steps))
    return CR_ORDER[index]


def implied_cr(hp: int, damage: float, attack_bonus: int) -> str:
    """The rating a stat block implies: the average of defensive and offensive CR, rounded to the
    nearest table CR with ties rounded up (DMG p.274-275, "Average Challenge Rating").

    Armor Class is not part of the estimate, because the solo budget does not set AC.
    """
    average = (cr_value(defensive_cr(hp)) + cr_value(offensive_cr(damage, attack_bonus))) / 2
    best = min(CR_ORDER, key=lambda label: (abs(cr_value(label) - average), -cr_value(label)))
    return best


def implied_xp(hp: int, damage: float, attack_bonus: int) -> int:
    return cr_xp(implied_cr(hp, damage, attack_bonus))


def single_hero_multiplier(monster_count: int) -> Fraction:
    """DMG p.82 encounter multiplier, moved one step up for a party of fewer than three (DMG p.83)."""
    if monster_count < 1:
        raise ValueError("An encounter has at least one monster")
    if monster_count == 1:
        return Fraction(3, 2)
    if monster_count == 2:
        return Fraction(2)
    if monster_count <= 6:
        return Fraction(5, 2)
    if monster_count <= 10:
        return Fraction(3)
    if monster_count <= 14:
        return Fraction(4)
    return Fraction(5)


def adjusted_xp(monster_xp: Iterable[int]) -> Fraction:
    """Total monster XP times the single-hero multiplier for the number of monsters (DMG p.82)."""
    values = list(monster_xp)
    if not values:
        return Fraction(0)
    return sum(values) * single_hero_multiplier(len(values))


def difficulty(adjusted: float, level: int) -> str:
    """DMG p.82 step 5 for one hero: the closest threshold lower than `adjusted` sets the tier."""
    easy, medium, hard, deadly = thresholds(level)
    if adjusted <= easy:
        return "trivial"
    if adjusted <= medium:
        return "easy"
    if adjusted <= hard:
        return "medium"
    if adjusted <= deadly:
        return "hard"
    return "deadly"


def printed_challenge(text: str) -> tuple[str, int] | None:
    """The record's own Challenge line as (CR label, XP), or None when the text has none.

    Reads through the engine's CR/XP parser (edgar_core), which cross-checks the printed XP against
    the table above. A record without a readable line is unknown, not zero.
    """
    try:
        return challenge_xp_from_stat_block(text)
    except ValueError:
        return None
