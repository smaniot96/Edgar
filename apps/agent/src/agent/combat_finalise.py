"""The end of one combat round: its effects, end conditions, persisted state, the adjudication the turn
persists and the pending narration (EDG-057 pass 2).

Everything here runs after the last foe has acted and before the narrator is called. It decides the
round's outcome, the combat state the API persists, the adjudication and the round's telemetry inputs,
and the `PendingRoundNarration` whose one narrator call is the only thing left to produce (EDG-079).
Nothing here rolls a die.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.action_economy import economy_record
from agent.combat_narration import PendingRoundNarration
from agent.combat_round_record import RoundRecord
from agent.combat_view import _display, initiative_view
from agent.combatants import (
    apply_player_disposition,
    clear_effects,
    disposition_of,
    finish_victory,
    has_effect,
    is_standing,
    stable_id,
)
from agent.conditions import expire_effects, settle_concentration
from agent.death import DEATH_MODE_SAVES, record_from
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.resolution import DEATH_SAVES_KEY, decide_completion, second_wind_status
from agent.supported_rules import actions_for_state, ruling_lines


@dataclass(frozen=True)
class RoundEnd:
    """How the round ended, decided once every creature has acted."""

    ended: bool
    outcome: str | None  # "victory", "defeat", "fled" or None while the fight goes on
    second_wind_used: bool  # the derived HUD mirror; the sheet resource is the authority


@dataclass(frozen=True)
class SettlementInput:
    """What the adjudication the turn persists is built from. All of it is decided before narration."""

    adjudication: AdjudicationResult
    state: dict[str, Any]  # the round's state (for the rulings and the completion)
    parsed: Any
    player_input: str
    player_hp_start: int  # the hero's HP before the round; the adjudication's HP delta is measured from it
    sw_before: int  # Second Wind uses on the sheet before the round
    cf_before: dict[str, int]  # class feature uses on the sheet before the round
    cf_left: dict[str, int]  # class feature uses after the round
    spell_expended: dict[str, int] | None
    inventory_used: list[str]
    dealt: int  # the hero's damage this round (the adjudication's damage)
    later_swing_dice: list[dict]
    outcome: str | None


@dataclass(frozen=True)
class RoundSettlement:
    """The adjudication after the round's summary and completion are decided, with the effects it used."""

    adjudication: AdjudicationResult
    engine_effects: tuple[str, ...]
    roll_mode: str  # the round's d20 mode: advantage beats disadvantage beats normal


def close_round_effects(rec: RoundRecord) -> None:
    """Timed effects and concentration end with their round (EDG-043), after every creature has acted."""
    round_num = int(rec.combat_state.get("round", 1) or 1)
    for foe_name, expired in expire_effects(rec.combatants, round_num):
        rec.log_lines.append(f"{foe_name} is no longer {expired}.")
    if ended_spell := settle_concentration(rec.player, rec.enemies, round_num):
        rec.log_lines.append(f"{rec.hero}'s concentration on {ended_spell} ends.")
    for foe in rec.enemies:
        if ended_spell := settle_concentration(foe, [rec.player], round_num):
            rec.log_lines.append(f"{_display(foe)}'s concentration on {ended_spell} ends.")


def settle_round(
    rec: RoundRecord, *, declared: list[Any], player_acted: bool, max_rounds: int
) -> RoundEnd:
    """Apply the hero's final HP and disposition, decide the end conditions, and write the combat state
    the API persists. `max_rounds` is the round cap (`MAX_COMBAT_ROUNDS`, passed in so it stays patchable
    where the node reads it)."""
    player, character, combat_state = rec.player, rec.character, rec.combat_state
    combatants, hero, log_lines = rec.combatants, rec.hero, rec.log_lines
    player["hp_current"] = rec.hp
    apply_player_disposition(player, subdued=False)
    # Derived mirror for the HUD only; the sheet resource above is the authority.
    second_wind_used = bool(second_wind_status(character)) and rec.sw_left <= 0
    player["second_wind_used"] = second_wind_used
    active_enemies = [e for e in rec.enemies if is_standing(e)]

    # --- End conditions ---
    rec.round_num = int(combat_state.get("round", 1) or 1)
    ended, outcome = False, None
    if rec.fled:
        ended, outcome = True, "fled"
        log_lines.append("You break away from the fight and escape.")
    elif rec.dead:
        # EDG-045: death saves or massive damage ended the hero. The encounter is a defeat.
        ended, outcome = True, "defeat"
        player["disposition"] = "dead"
        player["alive"] = False
        log_lines.append(f"{hero} dies.")
    elif not active_enemies:
        ended, outcome = True, "victory"
        log_lines.append("All enemies are defeated. Victory!")
    elif rec.hp <= 0 and rec.death_mode != DEATH_MODE_SAVES:
        ended, outcome = True, "defeat"
        log_lines.append(f"{hero} is overwhelmed and falls unconscious, subdued by the enemy.")
    elif rec.round_num >= max_rounds:
        ended, outcome = True, "fled"
        log_lines.append("The fight drags on; you disengage to regroup.")

    if outcome == "defeat" and rec.death_mode != DEATH_MODE_SAVES:
        # Solo campaigns have no human GM to adjudicate death, so a "defeat" means the player
        # is subdued / knocked unconscious (and the story continues — often captured), rather
        # than a permanent dead-end. Leave them at 1 HP.
        rec.hp = 1
        player["hp_current"] = 1
        apply_player_disposition(player, subdued=True)
    if outcome == "victory":
        finish_victory(combatants)
    if ended:
        # Typed effects end with the encounter (EDG-021): nothing carries into a later fight.
        clear_effects(combatants)
        for combatant in combatants:
            combatant.pop("concentration", None)  # EDG-043: all casters stop with the fight
        player.pop("raging", None)  # rage, too, lasts only for this encounter (EDG-145)
        player.pop("rage_round", None)

    # EDG-044: what this turn spent, kept on the player row (persisted with the combatants).
    spent = declared if player_acted and not rec.fled else []
    readied_fired = "readied_attack" in rec.round_effects
    player["action_economy"] = economy_record(
        rec.round_num,
        spent,
        dodging=has_effect(player, "dodging"),
        reaction="readied attack" if readied_fired else None,
    )
    view = initiative_view(combatants)
    player_view_index = next((i for i, c in enumerate(view) if c["is_player"]), 0)
    combat_state.update(
        {
            "combatants": combatants,
            "initiative_order": [c["name"] for c in combatants],
            "initiative": view,
            "economy": player["action_economy"],
            "round": rec.round_num if ended else rec.round_num + 1,
            "ended": ended,
            "outcome": outcome,
            "player_ac": rec.player_ac,
            "second_wind_used": second_wind_used,
            "second_wind_uses_remaining": rec.sw_left,
            "raging": bool(player.get("raging")),
            "current_turn_index": player_view_index,
            "current_turn": None if ended else _display(player),
        }
    )
    return RoundEnd(ended=ended, outcome=outcome, second_wind_used=second_wind_used)


def settle_adjudication(rec: RoundRecord, inp: SettlementInput) -> RoundSettlement:
    """The adjudication the API persists: engine HP and the spent uses, the validated flags and the
    round summary with its rulings, then the completion decision."""
    adj = inp.adjudication
    log_lines = rec.log_lines
    cu = adj.character_update or CharacterUpdate()
    cu.hp_delta = rec.hp - inp.player_hp_start
    cu.damage_dice = None
    if rec.sw_left != inp.sw_before:
        # Only a spent use is written; a sheet without the key keeps deriving its full uses.
        cu.second_wind_uses_remaining = rec.sw_left
    cf_changed = {f: left for f, left in inp.cf_left.items() if left != inp.cf_before.get(f)}
    if cf_changed:
        # Class feature uses (EDG-145): only the spent features are written, as for Second Wind.
        cu.class_feature_uses = cf_changed
    cu.spell_slots_expended = inp.spell_expended
    if rec.death_mode == DEATH_MODE_SAVES:
        # EDG-045: the hero's death record after the round; {} when conscious (clears a stored one).
        record = record_from(rec.player.get(DEATH_SAVES_KEY))
        cu.death_saves = record.to_dict() if record is not None else {}
    for item in inp.inventory_used:
        if item not in cu.inventory_remove:
            cu.inventory_remove.append(item)
    adj.character_update = cu
    adj.damage = inp.dealt or None
    adj.dice_log = [
        *adj.dice_log,
        *[d for d in rec.dice_log if d not in adj.dice_log],
        *inp.later_swing_dice,
    ]
    # Declared unsupported mechanics are ruled on in the round summary (no engine effect).
    summary_text = " ".join(line for line in log_lines if line)
    engine_effects = tuple(rec.round_effects)
    round_roll_mode = (
        "advantage"
        if "advantage" in rec.round_modes
        else "disadvantage"
        if "disadvantage" in rec.round_modes
        else "normal"
    )
    actions = actions_for_state(
        {**inp.state, "engine_effects": engine_effects, "engine_roll_mode": round_roll_mode}
    )
    for ruling in ruling_lines(actions):
        if ruling not in summary_text:
            log_lines.append(ruling)
    adj.mechanical_summary = " ".join(line for line in log_lines if line)

    boss_victory = inp.outcome == "victory" and any(e.get("is_boss") for e in rec.enemies)
    decide_completion(
        adj,
        world_flags=inp.state.get("world_flags"),
        parsed=inp.parsed,
        player_input=inp.player_input,
        combat_active=inp.outcome != "victory",
        boss_victory=boss_victory,
    )
    return RoundSettlement(adjudication=adj, engine_effects=engine_effects, roll_mode=round_roll_mode)


def build_pending_narration(
    rec: RoundRecord,
    *,
    state: dict[str, Any],
    retr: dict[str, Any],
    settlement: RoundSettlement,
    end: RoundEnd,
    player_action: dict[str, Any],
    bonus_record: dict | None,
    stage_ms: dict[str, int],
) -> PendingRoundNarration:
    """The mechanics of the round are final: this is everything the narrator is given and everything
    persisted, with the one narrator call left to run (EDG-079)."""
    adj, engine_effects, round_roll_mode = (
        settlement.adjudication,
        settlement.engine_effects,
        settlement.roll_mode,
    )
    combat_state = rec.combat_state
    combat_state["round_actors"] = rec.round_actors
    narr_state = {
        **state,
        "adjudication_result": adj,
        "combat_state": combat_state,
        "engine_effects": engine_effects,
        "engine_roll_mode": round_roll_mode,
    }
    return PendingRoundNarration(
        narr_state=narr_state,
        combatants=rec.combatants,
        adjudication=adj,
        outcome=end.outcome,
        base_update={
            **retr,
            "combat_state": combat_state,
            "adjudication_result": adj,
            "engine_roll_mode": round_roll_mode,
            "rules_context": state.get("rules_context", []),
            "adventure_context": state.get("adventure_context", []),
        },
        round_event={
            "turn_id": state.get("turn_id"),
            "round_num": rec.round_num,
            "ended": end.ended,
            "outcome": end.outcome,
            "actors": rec.actors,
            "player_action": {**player_action, **({"bonus_action": bonus_record} if bonus_record else {})},
            "outcomes": rec.outcomes,
            "dispositions": [
                {
                    "id": stable_id(c),
                    "name": _display(c),
                    "is_player": bool(c.get("is_player")),
                    "disposition": disposition_of(c),
                    "hp_current": c.get("hp_current"),
                }
                for c in rec.combatants
            ],
            "dice_log": adj.dice_log,
        },
        stage_ms=dict(stage_ms),
    )
