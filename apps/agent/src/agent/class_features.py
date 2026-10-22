"""Class features beyond Second Wind (EDG-145): engine-owned uses, dice and combat effects.

Implemented here (each has a contract entry in `agent.supported_rules`):
  * Fighter Action Surge: one use, restored by a short or long rest (two uses from level 17). A use
    adds one more weapon attack against the same foe in the same combat round.
  * Rogue Sneak Attack: 1d6 per two rogue levels, rounded up, added to a hit once per turn when the
    attack has advantage from an engine source and uses a finesse or ranged weapon. It has no uses.
  * Barbarian Rage: uses per long rest by level (2, 3, 4, 5, 6 from levels 1, 3, 6, 12, 17). Raging
    adds +2 damage (+3 from level 9, +4 from level 16) to hits with a melee weapon whose attack
    ability is Strength, and halves bludgeoning, piercing and slashing damage taken, but only
    outside heavy armor. It starts in a combat round without spending the attack and ends after
    1 minute (10 rounds) or with the encounter. Its Strength advantage and its early end (no attack
    or damage in a turn, or by choice) are not tracked.

Uses live in CharacterAssignment.stats["class_features"] (JSONB, no migration). A sheet without the
key has its full uses. The rage flag is `raging` on the player's combatant entry, with the round it
began in `rage_round`, so it ends with the encounter. Nothing here rolls a number except through the
caller's engine dice.
"""

from __future__ import annotations

import random
import re

from agent.action_economy import affirmative_matches
from agent.resolution import (
    CLASS_FEATURES_KEY,
    CheckResult,
    _attack_ability,
    character_class,
    choose_weapon,
    wears_heavy_armor,
)
from tools.dice import DiceOutcome, roll

ACTION_SURGE = "action_surge"
RAGE = "rage"
SNEAK_ATTACK = "sneak_attack"
RESOURCE_FEATURES = (ACTION_SURGE, RAGE)
IMPLEMENTED_FEATURES = (ACTION_SURGE, SNEAK_ATTACK, RAGE)
# Names the combat round records in `engine_effects` when it executes a feature this turn.
FEATURE_EFFECTS = frozenset(IMPLEMENTED_FEATURES)

# Player wording that declares a feature. Narration is matched by the narrower NARRATION_CLAIMS.
DECLARED_TEXT = {
    ACTION_SURGE: re.compile(r"\baction\s+surge\b", re.IGNORECASE),
    SNEAK_ATTACK: re.compile(r"\bsneak\s+attacks?\b", re.IGNORECASE),
    RAGE: re.compile(
        # "into rage" needs its article ("into a rage"), so "into rage metal" declares nothing.
        r"\b(?:into\s+(?:a|my|the)\s+|(?:enter|activat|use|start|begin)\w*\s+(?:a\s+|my\s+|the\s+)?)rage\b"
        r"|\bbarbarian\s+rage\b|\bi\s+rage\b(?!\s+(?:against|about|over)\b)",
        re.IGNORECASE,
    ),
}
# Narration that describes a feature as in effect. A bare "rage" or "surge" is prose, not a claim.
NARRATION_CLAIMS = {
    ACTION_SURGE: r"\baction\s+surge\b",
    SNEAK_ATTACK: r"\bsneak\s+attacks?\b",
    RAGE: r"\bbarbarian\s+rage\b|\b(?:enter|enters|entered)\s+(?:a\s+)?rage\b",
}

_RAGE_USES = ((17, 6), (12, 5), (6, 4), (3, 3), (1, 2))  # (from level, uses per long rest)
RAGE_ROUNDS = 10  # a rage lasts 1 minute (PHB, PDF p.49): 10 rounds of 6 seconds
_RAGE_RESISTED = frozenset({"bludgeoning", "piercing", "slashing"})


def _level(character: dict | None) -> int:
    try:
        return min(20, max(1, int((character or {}).get("level") or 1)))
    except (TypeError, ValueError):
        return 1


def holds_feature(character: dict | None, feature: str) -> bool:
    """Whether the class has the feature at this level (Action Surge starts at fighter level 2)."""
    cls = character_class(character)
    if feature == ACTION_SURGE:
        return cls == "fighter" and _level(character) >= 2
    if feature == RAGE:
        return cls == "barbarian"
    if feature == SNEAK_ATTACK:
        return cls == "rogue"
    return False


def max_uses(character: dict | None, feature: str) -> int:
    """Uses per rest for a resource feature; 0 when the class lacks it or it has no uses."""
    if feature not in RESOURCE_FEATURES or not holds_feature(character, feature):
        return 0
    if feature == ACTION_SURGE:
        return 2 if _level(character) >= 17 else 1
    level = _level(character)
    return next(uses for floor, uses in _RAGE_USES if level >= floor)


def uses_left(character: dict | None, feature: str) -> int:
    """Uses left on the sheet, clamped to the maximum. Missing or malformed state means full."""
    maximum = max_uses(character, feature)
    stats = (character or {}).get("stats")
    features = stats.get(CLASS_FEATURES_KEY) if isinstance(stats, dict) else None
    entry = features.get(feature) if isinstance(features, dict) else None
    left = entry.get("uses_remaining") if isinstance(entry, dict) else None
    if not isinstance(left, int) or isinstance(left, bool):
        return maximum
    return max(0, min(maximum, left))


def resource_uses(character: dict | None) -> dict[str, int]:
    """Uses left for each resource feature the character holds (empty for other classes)."""
    return {f: uses_left(character, f) for f in RESOURCE_FEATURES if max_uses(character, f) > 0}


def class_feature_status(character: dict | None) -> dict | None:
    """Public view of the resource features the character holds; None when there are none."""
    held = [f for f in RESOURCE_FEATURES if max_uses(character, f) > 0]
    if not held:
        return None
    return {
        f: {"uses_remaining": uses_left(character, f), "uses_max": max_uses(character, f)}
        for f in held
    }


def requested_features(text: str | None, entity: str | None = None) -> tuple[str, ...]:
    """Implemented features the player declares, from their words and the parser's feature name.

    Stable order (IMPLEMENTED_FEATURES). Pure: it names what was asked, never what applies.
    """
    # An entity proposed by the parser cannot re-authorize a feature the player negated.
    # Keep entity-only compatibility for callers that have no original text.
    body = str(text or "").strip() or f"use {entity or ''}"
    return tuple(f for f in IMPLEMENTED_FEATURES if affirmative_matches(DECLARED_TEXT[f], body))


def spend_use(character: dict | None, feature: str, left: int | None = None) -> int | None:
    """Spend one use: the count left afterwards, or None (nothing spent) when none is left.

    `left` lets the combat round pass its own running count. The caller persists the result.
    """
    remaining = uses_left(character, feature) if left is None else left
    if remaining <= 0 or max_uses(character, feature) <= 0:
        return None
    return remaining - 1


def class_feature_after_rest(character: dict | None, kind: str | None) -> dict[str, int] | None:
    """Resource uses restored by a rest, or None when the rest changes nothing.

    A short or long rest restores Action Surge (2014 PHB). A long rest also restores Rage. A sheet
    that is already full is not rewritten.
    """
    if kind not in ("short", "long"):
        return None
    restored: dict[str, int] = {}
    for feature in RESOURCE_FEATURES:
        maximum = max_uses(character, feature)
        if maximum <= 0 or (feature == RAGE and kind != "long"):
            continue
        if uses_left(character, feature) != maximum:
            restored[feature] = maximum
    return restored or None


def _rage_in_effect(character: dict | None, raging: bool) -> bool:
    """Rage's benefits need a raging barbarian who is not wearing heavy armor (PHB, PDF p.49)."""
    return raging and holds_feature(character, RAGE) and not wears_heavy_armor(character)


def rage_resists(character: dict | None, damage_type: str | None, *, raging: bool) -> bool:
    """Whether Rage halves this damage: bludgeoning, piercing or slashing; an untyped one is not."""
    return damage_type in _RAGE_RESISTED and _rage_in_effect(character, raging)


def rage_damage_bonus(character: dict | None, attack: CheckResult, *, raging: bool) -> int:
    """Flat damage added to a raging hit: a melee weapon attack using Strength. 0 otherwise."""
    if not _rage_in_effect(character, raging):
        return 0
    if attack.kind != "attack" or not attack.success or attack.weapon is None:
        return 0
    if attack.weapon.ranged or _attack_ability(character, attack.weapon) != "STR":
        return 0
    level = _level(character)
    return 4 if level >= 16 else 3 if level >= 9 else 2


def sneak_attack_roll(
    character: dict | None,
    attack: CheckResult,
    *,
    mode: str,
    used: bool,
    rng: random.Random | None = None,
) -> DiceOutcome | None:
    """Sneak Attack dice for a hit, or None. Needs advantage from the engine, a finesse or ranged
    weapon, and no earlier Sneak Attack this turn. A critical doubles the dice."""
    if used or mode != "advantage" or attack.kind != "attack" or not attack.success:
        return None
    if not holds_feature(character, SNEAK_ATTACK):
        return None
    weapon = attack.weapon or choose_weapon(character)
    if not (weapon.finesse or weapon.ranged):
        return None
    count = (_level(character) + 1) // 2 * (2 if attack.critical else 1)
    return roll(f"{count}d6", rng)


def ruling(feature: str, character: dict | None) -> str:
    """The player-facing ruling for a declared feature the engine did not execute this turn."""
    if feature == ACTION_SURGE:
        if character_class(character) != "fighter":
            reason = "Only a fighter has Action Surge."
        elif _level(character) < 2:
            reason = "Action Surge needs fighter level 2."
        elif uses_left(character, feature) <= 0:
            reason = "Action Surge has no uses left until a short or long rest."
        else:
            reason = (
                "Action Surge adds one more weapon attack, and needs an attack on a foe in combat."
            )
        return f"Narrative ruling: {reason} No extra action is taken."
    if feature == SNEAK_ATTACK:
        if character_class(character) != "rogue":
            reason = "Only a rogue has Sneak Attack."
        else:
            reason = (
                "Sneak Attack adds damage only to a hit with advantage from the engine, using a "
                "finesse or ranged weapon, once per turn in combat."
            )
        return f"Narrative ruling: {reason} No extra damage is applied."
    if character_class(character) != "barbarian":
        reason = "Only a barbarian has Rage."
    elif uses_left(character, feature) <= 0:
        reason = "Rage has no uses left until a long rest."
    else:
        reason = (
            "Rage is activated in combat. It adds damage to melee weapon attacks that use Strength "
            "and halves bludgeoning, piercing and slashing damage (not in heavy armor) for 1 minute."
        )
    return f"Narrative ruling: {reason} No rage is applied."
