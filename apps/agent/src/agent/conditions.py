"""Engine-applied conditions, concentration and monster riders (EDG-043).

Spell conditions from catalog spells (`agent.spells`), always applied by the engine and recorded in
the target's typed `effects` (`agent.combatants`):

  * `blinded` (Blindness/Deafness): the foe fails its Constitution save against the caster's spell
    save DC. Its own attacks have disadvantage and attacks against it have advantage. It repeats the
    Constitution save at the end of each of its turns; success ends the effect. The effect lasts one
    minute (10 rounds) unless the save ends it earlier. The spell is not concentration here.
  * `marked` (Guiding Bolt): on a hit, the next attack roll against the foe has advantage. That roll
    spends the mark, and the mark otherwise ends at the end of the caster's next turn (2 rounds).
  * `paralyzed` (Hold Person, concentration): the foe fails its Wisdom save against the caster's spell
    save DC and is paralyzed for one minute. A paralyzed creature is incapacitated (it cannot act but
    stays in the fight). Attack rolls against it have advantage, and a melee hit from within 5 feet is
    a critical hit (PHB Appendix A, Paralyzed). The foe repeats the Wisdom save at the end of each of
    its turns; success ends the effect. Only humanoids are targets (`humanoid_refusal`).

Concentration (PHB, Concentration, p. 203). The caster holds at most one concentration spell, stored
as `concentration` on the player combatant. Casting another concentration spell ends the first, and
the first spell's effects on foes end with it. The spell also ends when its minute passes, when no foe
still carries its effect, and when the caster falls to 0 HP. Taking damage requires a Constitution
save (`concentration_save`): DC 10 or half the damage, whichever is higher (rounded down), and it ends
the spell on failure. The engine rolls every save and sets every number.

Monster riders (`AttackRider`): a foe's exact attack text may poison the hero on a hit. The hero makes
the stated Constitution save (`rider_save`); failure records `poisoned` (disadvantage on the hero's
attack rolls and ability checks). The hero repeats the save at the end of each of its own turns
(`hero_end_of_turn_saves`) with the sheet's Constitution save modifier, and success ends the effect.
Monster save actions (`AttackSave`, rolled in `agent.graph_combat`) leave the hero `frightened` or
`stunned` the same way (`afflict`), for one minute with the repeat save. A rider worded "until the end
of the attacker's next turn" has no repeat save and ends with that turn (`end_of_source_turn`).

Conditions on the hero from monster text (PHB Appendix A, PDF pp.291-293):
  * `frightened`: disadvantage on attack rolls and ability checks while the source of the fear is in
    sight, and no willing move closer to it (`fear_refusal`). Line of sight is not modelled: a source
    that still stands is in sight. When it is defeated, the condition and its save persist, but its
    attack/check and movement restrictions no longer apply.
  * `stunned`: incapacitated (the hero's turn passes with no action, move or flight), fails Strength
    and Dexterity saves automatically (`combatants.save_auto_fails`), and attacks against the hero
    have advantage.

Deliberately absent: other conditions (charmed, grappled, incapacitated and the rest), any condition
applied from model prose, and poisoned foes. The engine rolls every save and sets every number; a
narrated condition without an applied effect is never recorded.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agent.combatants import (
    add_effect,
    concentration_of,
    effects_of,
    has_effect,
    is_standing,
    release_effect,
    stable_id,
)
from agent.monster_stats import AttackRider, stats_of
from agent.positioning import MELEE
from agent.resolution import ability_modifier, proficiency_bonus, save_proficiencies
from tools.dice import DiceOutcome, roll

# Conditions this module may apply. Anything else is refused by the typed operation.
SPELL_CONDITIONS: tuple[str, ...] = ("blinded", "marked", "paralyzed")
# Weapons whose reach is more than 5 feet: a hit from one is not a critical against a paralyzed foe,
# because the engine does not track the distance such an attacker stands at.
REACH_WEAPONS = frozenset({"glaive", "halberd", "lance", "pike"})


@dataclass(frozen=True)
class SaveRoll:
    """One repeat save made at the end of a combatant's turn, rolled by the engine."""

    name: str  # the combatant's display name
    kind: str  # the condition the save may end
    ability: str
    dc: int
    total: int
    ended: bool
    outcome: DiceOutcome


@dataclass(frozen=True)
class ConcentrationRoll:
    """The Constitution save made when the caster took damage while concentrating."""

    spell: str
    dc: int
    total: int
    kept: bool  # True when the concentration holds
    outcome: DiceOutcome


@dataclass(frozen=True)
class RiderRoll:
    """The save a monster's rider demanded of the hero on a hit."""

    kind: str
    ability: str
    dc: int
    total: int
    failed: bool
    outcome: DiceOutcome


def apply_spell_condition(
    target: dict,
    kind: str,
    *,
    source: str,
    round_applied: int,
    duration_rounds: int,
    repeat_save: Mapping[str, Any] | None = None,
) -> bool:
    """Record a spell condition on a foe. False when the foe already has it (no stacking)."""
    if kind not in SPELL_CONDITIONS:
        raise ValueError(f"not a spell condition: {kind!r}")
    last_round = int(round_applied) + max(1, int(duration_rounds)) - 1
    return add_effect(
        target,
        kind,
        source=source,
        round_applied=round_applied,
        expires_after_round=last_round,
        save=repeat_save,
    )


def spell_condition_sentence(kind: str, name: str, *, applied: bool) -> str:
    """The engine's sentence about a spell condition, for the round summary and narrator."""
    if kind == "blinded":
        if applied:
            return (
                f"{name} is blinded: its attacks have disadvantage and attacks against it have "
                "advantage. It repeats the Constitution save at the end of each of its turns."
            )
        return f"{name} is already blinded."
    if kind == "paralyzed":
        if applied:
            return (
                f"{name} is paralyzed: it cannot act, and attacks against it have advantage. It "
                "repeats the Wisdom save at the end of each of its turns."
            )
        return f"{name} is already paralyzed."
    if applied:
        return f"{name} is marked: the next attack roll against it has advantage."
    return f"{name} is already marked."


def humanoid_refusal(spell_name: str, target: Mapping[str, Any]) -> str | None:
    """A refusal when a humanoid-only spell targets a foe that is not a readable humanoid.

    A record whose type line cannot be read is refused too: the engine does not guess the type.
    """
    stats = stats_of(target)
    kind = stats.creature_type if stats is not None else None
    if kind == "humanoid":
        return None
    name = str(target.get("display_name") or target.get("name") or "the target")
    if kind is None:
        return (
            f"{spell_name} is not cast: the creature type of {name} cannot be read from its record, "
            "and the spell affects only humanoids, so no spell effect is applied."
        )
    return f"{spell_name} is not cast: {name} is a {kind}, not a humanoid, so no spell effect is applied."


def consume_mark(target: dict) -> bool:
    """Spend a mark on the attack roll made against this foe. False when it was not marked."""
    return release_effect(target, "marked")


def paralyzed_melee_crit(
    target: Mapping[str, Any], *, weapon_name: str | None, ranged: bool, spell: bool
) -> bool:
    """True when a hit on this target is a critical hit: a paralyzed foe, struck in melee from 5 feet.

    Ranged and spell attacks are not from within 5 feet of the foe, and reach weapons are not
    assumed to stand within 5 feet, so none of them is a critical here (the rule is not applied).
    """
    if ranged or spell or not has_effect(target, "paralyzed"):
        return False
    return (weapon_name or "").strip().lower() not in REACH_WEAPONS


def save_modifier(character: Mapping[str, Any] | None, ability: str) -> int:
    """The sheet's saving-throw modifier: the ability modifier, plus proficiency when the sheet has it."""
    bonus = ability_modifier(character, ability)
    if ability in save_proficiencies(character):
        bonus += proficiency_bonus((character or {}).get("level"))
    return bonus


def _repeat_saves(combatant: dict, bonus_of: Callable[[str], int]) -> list[SaveRoll]:
    rolls: list[SaveRoll] = []
    for effect in effects_of(combatant):
        save = effect.get("save")
        if not isinstance(save, dict):
            continue
        ability = str(save.get("ability") or "")
        dc = int(save.get("dc") or 0)
        bonus = bonus_of(ability)
        outcome = roll(f"1d20{bonus:+d}" if bonus else "1d20")
        ended = outcome.total >= dc
        rolls.append(
            SaveRoll(
                name=str(combatant.get("display_name") or combatant.get("name") or "?"),
                kind=str(effect["kind"]),
                ability=ability,
                dc=dc,
                total=outcome.total,
                ended=ended,
                outcome=outcome,
            )
        )
        if ended:
            release_effect(combatant, str(effect["kind"]))
    return rolls


def end_of_turn_saves(combatant: dict) -> list[SaveRoll]:
    """Repeat the save of each save-ending effect, at the end of a foe's turn.

    The engine rolls d20 plus the foe's stated save bonus (a foe without an exact record has none).
    A success ends that effect. Rows without save-ending effects are not touched.
    """
    stats = stats_of(combatant)
    return _repeat_saves(combatant, lambda ability: stats.save_bonus(ability) if stats else 0)


def hero_end_of_turn_saves(player: dict, character: Mapping[str, Any] | None) -> list[SaveRoll]:
    """Repeat the save of each save-ending effect on the hero, at the end of the hero's turn."""
    return _repeat_saves(player, lambda ability: save_modifier(character, ability))


def save_line(save: SaveRoll) -> str:
    """The round-summary line for a repeat save (no numbers beyond the engine roll)."""
    result = "ends the effect" if save.ended else "keeps the effect"
    return (
        f"{save.name} repeats its {save.ability} save against {save.kind} "
        f"(rolled {save.total} vs DC {save.dc}) and {result}."
    )


def afflict(
    player: dict,
    kind: str,
    *,
    source: str,
    round_applied: int,
    ability: str,
    dc: int,
    duration_rounds: int = 10,
    source_id: str | None = None,
) -> None:
    """Record a condition from a monster on the hero: it lasts `duration_rounds` (a minute is 10) and
    the hero repeats the save at the end of each of its turns. It does not stack."""
    add_effect(
        player,
        kind,
        source=source,
        round_applied=round_applied,
        expires_after_round=round_applied + max(1, duration_rounds) - 1,
        save={"ability": ability, "dc": dc},
        source_id=source_id if kind == "frightened" else None,
    )


def end_fear_of_defeated(player: dict, foes: list[dict]) -> str | None:
    """Stop a defeated fear source's mechanical effects while the condition and save persist.

    Line of sight is not modelled, so a source that still stands counts as in sight.
    """
    fear = next((e for e in effects_of(player) if e["kind"] == "frightened"), None)
    if fear is None or fear.get("source_defeated"):
        return None
    source_id = str(fear.get("source_id") or fear["source"] or "")
    if any(stable_id(f) == source_id and is_standing(f) for f in foes):
        return None
    fear["source_defeated"] = True
    # `effects_of` returns copies to keep reads pure; write the updated entry back explicitly.
    player["effects"] = [
        fear if e["kind"] == "frightened" else e for e in effects_of(player)
    ]
    return str(fear["source"])


def end_of_source_turn(player: dict, source: dict, round_num: int) -> list[str]:
    """End the hero's conditions that last until the end of `source`'s next turn: called at the end
    of that creature's turn. A condition it applied this round runs on into its next turn."""
    ended = [
        e["kind"]
        for e in effects_of(player)
        if e.get("ends_with_turn_of") == stable_id(source)
        and e["applied_round"] < round_num
    ]
    for kind in ended:
        release_effect(player, kind)
    return ended


def fear_refusal(
    hero: str, player: Mapping[str, Any], steps: Sequence[tuple[str, list[dict]]]
) -> str | None:
    """The refusal when a frightened hero declares a move closer to the source of its fear (PHB
    Appendix A, PDF p.291). `steps` are the planned (destination band, foes that change band) moves."""
    fear = next((e for e in effects_of(player) if e["kind"] == "frightened"), None)
    if fear is not None and not fear.get("source_defeated") and any(
        band == MELEE
        and stable_id(foe) == str(fear.get("source_id") or fear["source"])
        for band, foes in steps
        for foe in foes
    ):
        return (
            f"{hero} is frightened of {fear['source']} and cannot willingly move closer to it. "
            "Choose another move: no roll is made and no turn is spent."
        )
    return None


def rider_save(
    player: dict,
    rider: AttackRider,
    *,
    character: Mapping[str, Any] | None,
    source: str,
    round_applied: int,
    source_id: str | None = None,
) -> RiderRoll:
    """The hero's save against a rider on a hit. Failure records the condition with a repeat save."""
    bonus = save_modifier(character, rider.ability)
    outcome = roll(f"1d20{bonus:+d}" if bonus else "1d20")
    failed = outcome.total < rider.dc
    if failed and rider.duration_rounds is None:
        # Until the end of the attacker's next turn; the round's end is the backstop if it never comes.
        add_effect(
            player,
            rider.kind,
            source=source,
            round_applied=round_applied,
            expires_after_round=round_applied + 1,
            ends_with_turn_of=source_id or source,
            source_id=source_id if rider.kind == "frightened" else None,
        )
    elif failed:
        afflict(
            player,
            rider.kind,
            source=source,
            round_applied=round_applied,
            ability=rider.ability,
            dc=rider.dc,
            duration_rounds=rider.duration_rounds,
            source_id=source_id,
        )
    return RiderRoll(rider.kind, rider.ability, rider.dc, outcome.total, failed, outcome)


def rider_save_line(name: str, rider: RiderRoll, *, attack: str) -> str:
    """The round-summary line for a rider's save (the engine's roll, never a narrated one)."""
    if rider.failed:
        return (
            f"{name} fails the {rider.ability} save against {attack} (rolled {rider.total} vs DC "
            f"{rider.dc}) and is {rider.kind}."
        )
    return (
        f"{name} succeeds on the {rider.ability} save against {attack} (rolled {rider.total} vs "
        f"DC {rider.dc}) and is not {rider.kind}."
    )


# --------------------------------------------------------------------------------------------
# Concentration
# --------------------------------------------------------------------------------------------


def start_concentration(
    player: dict, spell: str, *, round_applied: int, duration_rounds: int
) -> None:
    """The caster concentrates on a spell whose effect has taken hold on a foe."""
    player["concentration"] = {
        "spell": spell,
        "round_applied": int(round_applied),
        "expires_after_round": int(round_applied) + max(1, int(duration_rounds)) - 1,
    }


def drop_concentration(player: dict, foes: list[dict]) -> str | None:
    """End the caster's concentration: its spell's effects end on every foe. Returns the spell name."""
    spell = (concentration_of(player) or {}).get("spell")
    player.pop("concentration", None)
    if not spell:
        return None
    for foe in foes:
        for kind in {e["kind"] for e in effects_of(foe) if e.get("source") == spell}:
            release_effect(foe, kind)
    return str(spell)


def drop_concentration_if_incapacitated(player: dict, foes: list[dict]) -> str | None:
    """Incapacitation ends concentration immediately, including a persisted stun on load (PHB p.203)."""
    if not (has_effect(player, "stunned") or has_effect(player, "paralyzed")):
        return None
    return drop_concentration(player, foes)


def concentration_save(
    player: dict,
    foes: list[dict],
    character: Mapping[str, Any] | None,
    damage: int,
) -> ConcentrationRoll | None:
    """The caster's Constitution save after taking `damage` while concentrating. None when not.

    DC is 10 or half the damage, whichever is higher (rounded down). A failure ends the spell.
    """
    concentration = concentration_of(player)
    if concentration is None or damage <= 0:
        return None
    dc = max(10, int(damage) // 2)
    bonus = save_modifier(character, "CON")
    outcome = roll(f"1d20{bonus:+d}" if bonus else "1d20")
    kept = outcome.total >= dc
    if not kept:
        drop_concentration(player, foes)
    return ConcentrationRoll(str(concentration["spell"]), dc, outcome.total, kept, outcome)


def settle_concentration(player: dict, foes: list[dict], round_num: int) -> str | None:
    """End the concentration once its minute has passed or no foe still carries its effect.

    Called at the end of each round, after the timed effects expire. Returns the spell name ended.
    """
    concentration = concentration_of(player)
    if concentration is None:
        return None
    last = concentration.get("expires_after_round")
    timed_out = last is not None and int(last) <= int(round_num)
    held = any(e.get("source") == concentration["spell"] for f in foes for e in effects_of(f))
    if timed_out or not held:
        return drop_concentration(player, foes)
    return None


def expire_effects(combatants: list[dict], round_num: int) -> list[tuple[str, str]]:
    """End timed effects whose last round has passed. Returns (foe name, kind) for each one ended."""
    ended: list[tuple[str, str]] = []
    for c in combatants:
        for effect in effects_of(c):
            last = effect.get("expires_after_round")
            if last is not None and int(last) <= int(round_num):
                release_effect(c, str(effect["kind"]))
                ended.append(
                    (str(c.get("display_name") or c.get("name") or "?"), str(effect["kind"]))
                )
    return ended
