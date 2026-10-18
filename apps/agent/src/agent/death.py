"""Death saving throws and instant death (EDG-045), used only when a campaign's death_mode is
'death_saves'. The 'subdued' mode (the default) never reaches this module.

Rules as the 2014 PHB writes them (PDF pp.198-199):
  * Damage that takes a creature from above 0 HP to 0 HP: if the damage left over is equal to or
    more than the hit point maximum, the creature dies outright. Otherwise it falls unconscious.
  * Any damage taken at 0 HP is a death saving throw failure, two if it is from a critical hit. Damage
    that equals or exceeds the hit point maximum at 0 HP kills outright.
  * At the start of its turn, a creature at 0 HP that is not stable rolls a d20 (no modifier):
    10 or higher succeeds, anything lower fails, a natural 1 counts as two failures, and a natural
    20 regains 1 HP and consciousness. Three successes make it stable (no more rolls; it stays at
    0 HP and unconscious). Three failures kill it. Successes and failures need not be consecutive.
  * A creature that regains any HP, or becomes stable, starts its counts again at zero.

The engine rolls every d20 through `tools.dice.roll`, so a turn's dice are scripted in tests with
`use_rng`. A record is stored on the sheet (CharacterAssignment.stats["death_saves"]) or on the hero's
combatant row, never as a model's proposal.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from tools.dice import DiceOutcome, roll

DEATH_MODE_SUBDUED = "subdued"
DEATH_MODE_SAVES = "death_saves"

DYING = "dying"  # at 0 HP, unconscious, making death saving throws
STABLE = "stable"  # at 0 HP, unconscious, no longer making them
DEAD = "dead"  # permanent (EDG-045)
STATES: tuple[str, ...] = (DYING, STABLE, DEAD)

SUCCESSES_TO_STABILISE = 3
FAILURES_TO_DIE = 3
SUCCESS_THRESHOLD = 10  # a death saving throw of 10 or higher succeeds (PHB, PDF p.198)


@dataclass(frozen=True)
class DeathRecord:
    state: str
    successes: int = 0
    failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "successes": self.successes, "failures": self.failures}


def record_from(value: Any) -> DeathRecord | None:
    """The stored record, or None for a conscious hero or a malformed value (no record is kept)."""
    if not isinstance(value, dict) or value.get("state") not in STATES:
        return None
    successes = _count(value.get("successes"))
    failures = _count(value.get("failures"))
    if value["state"] == STABLE:
        return DeathRecord(STABLE)
    return DeathRecord(value["state"], successes, failures)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, min(FAILURES_TO_DIE, value))


@dataclass(frozen=True)
class DamageOutcome:
    hp: int
    record: DeathRecord | None  # None when the hero is above 0 HP
    failures_added: int
    dropped: bool  # damage took the hero from above 0 HP to 0 HP (falls unconscious)
    instant_death: bool  # massive damage: dead outright, not by failures


def take_damage(
    hp_before: int,
    hp_max: int,
    amount: int,
    *,
    critical: bool = False,
    record: DeathRecord | None = None,
) -> DamageOutcome:
    """Apply one damage instance under the death rules. Pure: rolls nothing."""
    current = record if record is None or isinstance(record, DeathRecord) else record_from(record)
    if current is not None and current.state == DEAD:
        return DamageOutcome(0, current, 0, dropped=False, instant_death=False)
    if amount <= 0:
        return DamageOutcome(hp_before, current, 0, dropped=False, instant_death=False)
    if hp_before > 0:
        if amount < hp_before:
            return DamageOutcome(hp_before - amount, None, 0, dropped=False, instant_death=False)
        if amount - hp_before >= max(1, hp_max):
            return DamageOutcome(0, DeathRecord(DEAD), 0, dropped=True, instant_death=True)
        return DamageOutcome(0, DeathRecord(DYING), 0, dropped=True, instant_death=False)
    # Already at 0 HP: damage is a failure (two for a critical hit), or instant death.
    if amount >= max(1, hp_max):
        return DamageOutcome(0, DeathRecord(DEAD), 0, dropped=False, instant_death=True)
    added = 2 if critical else 1
    base = current if current is not None else DeathRecord(DYING)
    failures = min(FAILURES_TO_DIE, base.failures + added)
    if failures >= FAILURES_TO_DIE:
        record_out = DeathRecord(DEAD, base.successes, failures)
    else:
        # Damage ends a stable creature's stability: it makes death saves again (PHB, PDF p.198).
        record_out = DeathRecord(DYING, base.successes, failures)
    return DamageOutcome(0, record_out, added, dropped=False, instant_death=False)


@dataclass(frozen=True)
class DeathSave:
    natural: int
    success: bool
    regained: bool  # a natural 20: 1 HP and consciousness
    hp: int
    record: DeathRecord | None  # None once the hero regains consciousness
    dice: DiceOutcome


def death_save(record: DeathRecord, rng: random.Random | None = None) -> DeathSave:
    """Roll one death saving throw for a dying hero. Only a dying record may be rolled for."""
    if record.state != DYING:
        raise ValueError("Only a dying creature makes death saving throws.")
    dice = roll("1d20", rng)
    natural = dice.rolls[0]
    successes, failures = record.successes, record.failures
    if natural == 20:
        return DeathSave(natural, True, True, 1, None, dice)
    success = False
    if natural == 1:
        failures += 2
    elif natural >= SUCCESS_THRESHOLD:
        successes += 1
        success = True
    else:
        failures += 1
    if failures >= FAILURES_TO_DIE:
        out = DeathRecord(DEAD, successes, min(FAILURES_TO_DIE, failures))
    elif successes >= SUCCESSES_TO_STABILISE:
        out = DeathRecord(STABLE)
    else:
        out = DeathRecord(DYING, successes, failures)
    return DeathSave(natural, success, False, 0, out, dice)


def heal_clears_record(hp_after: int) -> bool:
    """Any healing that leaves the hero above 0 HP ends unconsciousness and the death record."""
    return hp_after > 0


def life_state_of(stats: Any) -> str:
    """The hero's state for the roster: 'alive', or the record's state (dying, stable, dead)."""
    from agent.resolution import DEATH_SAVES_KEY

    record = record_from(stats.get(DEATH_SAVES_KEY)) if isinstance(stats, dict) else None
    return "alive" if record is None else record.state


def public_view(record: DeathRecord | None) -> dict[str, Any] | None:
    """The sheet's public view of a death record (None when the hero is not dying)."""
    if record is None:
        return None
    return {
        "state": record.state,
        "successes": record.successes,
        "failures": record.failures,
        "successes_to_stabilise": SUCCESSES_TO_STABILISE,
        "failures_to_die": FAILURES_TO_DIE,
    }
