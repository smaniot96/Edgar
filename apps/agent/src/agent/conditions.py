"""Engine-applied conditions from supported spells (EDG-043, first slice).

Two conditions are applied and enforced by the engine, always to a foe, always from a catalog spell
(`agent.spells`), and always recorded in the combatant's typed `effects` (`agent.combatants`):

  * `blinded` (Blindness/Deafness): the foe fails its Constitution save against the caster's spell
    save DC. Its own attacks have disadvantage and attacks against it have advantage. It repeats the
    Constitution save at the end of each of its turns; success ends the effect. The effect lasts one
    minute (10 rounds) unless the save ends it earlier. The spell is not concentration here.
  * `marked` (Guiding Bolt): on a hit, the next attack roll against the foe has advantage. That roll
    spends the mark, and the mark otherwise ends at the end of the caster's next turn (2 rounds).

Deliberately absent: other conditions (poisoned, frightened, paralyzed, stunned and the rest), any
condition applied from monster text or model prose, and concentration. The engine rolls every save
and sets every number; a narrated condition without an applied effect is never recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent.combatants import add_effect, effects_of, release_effect
from agent.monster_stats import stats_of
from tools.dice import DiceOutcome, roll

# Conditions this module may apply. Anything else is refused by the typed operation.
SPELL_CONDITIONS: tuple[str, ...] = ("blinded", "marked")


@dataclass(frozen=True)
class SaveRoll:
    """One repeat save made at the end of a foe's turn, rolled by the engine."""

    name: str  # the foe's display name
    kind: str  # the condition the save may end
    ability: str
    dc: int
    total: int
    ended: bool
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
    if applied:
        return f"{name} is marked: the next attack roll against it has advantage."
    return f"{name} is already marked."


def consume_mark(target: dict) -> bool:
    """Spend a mark on the attack roll made against this foe. False when it was not marked."""
    return release_effect(target, "marked")


def end_of_turn_saves(combatant: dict) -> list[SaveRoll]:
    """Repeat the save of each save-ending effect, at the end of the combatant's turn.

    The engine rolls d20 plus the foe's stated save bonus (a foe without an exact record has none).
    A success ends that effect. Rows without save-ending effects are not touched.
    """
    rolls: list[SaveRoll] = []
    stats = stats_of(combatant)
    for effect in effects_of(combatant):
        save = effect.get("save")
        if not isinstance(save, dict):
            continue
        ability = str(save.get("ability") or "")
        dc = int(save.get("dc") or 0)
        bonus = stats.save_bonus(ability) if stats is not None else 0
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


def save_line(save: SaveRoll) -> str:
    """The round-summary line for a repeat save (no numbers beyond the engine roll)."""
    result = "ends the effect" if save.ended else "keeps the effect"
    return (
        f"{save.name} repeats its {save.ability} save against {save.kind} "
        f"(rolled {save.total} vs DC {save.dc}) and {result}."
    )


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
