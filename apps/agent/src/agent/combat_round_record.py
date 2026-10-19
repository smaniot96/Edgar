"""The mutable record of one resolving combat round (EDG-057 split from graph_combat).

The enemy phase and the player's phase share these values in initiative order. The record only holds
state: it rolls no die and decides no rule. Its lists and dicts are the same objects the round
returns, so the telemetry and the persisted combat state see every change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundRecord:
    """Engine-owned values one round reads and writes, shared by the enemy and player phases."""

    hero: str
    character: dict
    player: dict
    enemies: list[dict]
    combatants: list[dict]
    player_ac: int
    round_num: int
    combat_state: dict
    hp: int
    sw_left: int
    fled: bool
    second_wind_auto_at_zero_hp: bool
    log_lines: list[str]
    dice_log: list[dict]
    round_effects: list[str]
    round_modes: list[str]
    # Enemy attack resolutions in initiative order (combat_telemetry). Only the enemy phase appends here.
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    actors: list[dict] = field(default_factory=list)
    round_actors: list[str] = field(default_factory=list)
    lair_done: bool = False
    # EDG-045: 'subdued' (defeat leaves the hero at 1 HP) or 'death_saves' (agent.death). Set per round.
    death_mode: str = "subdued"
    # The hero died this round (death saves or massive damage): the encounter ends in defeat.
    dead: bool = False
