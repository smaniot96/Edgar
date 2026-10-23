"""The player's action slot of one combat round (EDG-057 pass 2).

`run_player_turn` resolves what the hero declared, in the order the round has always rolled dice: the
bonus actions, Action Surge, declared movement (with opportunity attacks), then the action slot. The
hero's end-of-turn checks run after the adjudication, in `finish_hero_turn`.

The record (`RoundRecord`) is shared with the enemy phase and holds the values both phases write. The
declaration (`Declaration`) holds what was decided before any die. This module rolls dice only through
the engine helpers it calls, and decides no rule that is not already decided there.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from agent.action_economy import ACTION, SECOND_WIND, Declared
from agent.battlefield import Route, battlefield_of, pos_of
from agent.class_features import (
    ACTION_SURGE,
    RAGE,
    RAGE_ROUNDS,
    SNEAK_ATTACK,
    rage_damage_bonus,
    sneak_attack_roll,
    spend_use,
)
from agent.combat_declaration import Declaration
from agent.combat_dice import _contest, _mode_log
from agent.combat_enemy_turns import EnemyTurns
from agent.combat_grid import (
    attack_reach_check,
    grid_active,
    grid_fear_refusal,
    hero_budget,
    plan_route,
    project,
    walk_hero,
)
from agent.combat_plan import phase_for
from agent.combat_round_record import RoundRecord
from agent.combatants import (
    CombatManeuver,
    add_effect,
    apply_defeat,
    attack_roll_mode,
    can_act,
    contest_defender_bonus,
    declared_lethality,
    disposition_of,
    effects_of,
    has_effect,
    impaired_checks,
    is_standing,
    release_effect,
    stable_id,
)
from agent.conditions import (
    apply_spell_condition,
    consume_mark,
    drop_concentration,
    drop_concentration_if_incapacitated,
    end_fear_of_defeated,
    fear_refusal,
    hero_end_of_turn_saves,
    paralyzed_melee_crit,
    save_line,
    spell_condition_sentence,
    start_concentration,
)
from agent.contingency import holds as contingency_holds, not_held_text
from agent.death import (
    DEAD,
    DEATH_MODE_SAVES,
    DYING,
    STABLE,
    DeathRecord,
    death_save,
    record_from,
)
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    adjust_monster_damage,
    record_damage,
    stats_of,
    weapon_damage_type,
)
from agent.positioning import MELEE, RANGED, MovePlan, band_of, plan_moves, start_turn
from agent.readied_action import ReadyOrder, movement_text
from agent.resolution import (
    DEATH_SAVES_KEY,
    SECOND_WIND_REFUSAL,
    CheckResult,
    check_spec,
    choose_weapon,
    heal_from_consumable,
    resolve_attack,
    resolve_from_parsed,
    roll_attack_damage,
    spend_second_wind,
    wears_heavy_armor,
)
from agent.spells import CastPlan, narrate, resolve_spell


@dataclass(frozen=True)
class PlayerTurnInput:
    """The hero's slot inputs that the record does not hold. All of them are decided before any die."""

    decl: Declaration
    player_input: str
    parsed: ParsedInput
    hp_max: int
    cf_requested: Collection[str]  # class features the hero asked for this round (EDG-145)
    cf_left: dict[str, int]  # class feature uses left; spent uses are written here
    turns: EnemyTurns


@dataclass(frozen=True)
class PlayerTurnResult:
    """What the hero's slot produced, read by the adjudication and the round's finalisation."""

    player_acted: bool
    player_action: dict
    bonus_record: dict | None
    check_result: CheckResult | None
    dealt: int
    inventory_used: list[str]
    spell_expended: dict[str, int] | None
    extra_outcome: str  # the bonus action's text and the action's text, joined as the summary reads them
    declared: list[Declared]  # the economy slots this turn spent, with a refused one removed
    later_swing_dice: list[dict]  # Action Surge and off-hand dice, appended unmerged (see the adjudication)


@dataclass
class _Turn:
    """The slot's mutable working state. It is internal to this module; the result is the public output."""

    rec: RoundRecord
    inp: PlayerTurnInput
    player_acted: bool = False
    player_action: dict = field(default_factory=lambda: {"action": "none", "result": "down"})
    bonus_text: str = ""
    bonus_record: dict | None = None
    extra_outcome: str = ""
    check_result: CheckResult | None = None
    dealt: int = 0
    inventory_used: list[str] = field(default_factory=list)
    spell_expended: dict[str, int] | None = None
    declared: list[Declared] = field(default_factory=list)
    later_swing_dice: list[dict] = field(default_factory=list)
    sneak_used: bool = False
    runtime_fear_blocked: bool = False
    surge_swing: bool = False
    disengaged: bool = False
    # Combat grid: feet of movement walked this turn, and whether the attack is at long range.
    moved_ft: int = 0
    long_range: bool = False
    out_of_reach: bool = False


def _strike(
    target: dict, amount: int, parsed: Any, player_input: str, damage_type: str | None = None
) -> str:
    """Apply an engine-rolled hit to an enemy; returns the status phrase for the round log."""
    target["hp_current"] = max(0, int(target["hp_current"]) - amount)
    record_damage(target, damage_type, amount)
    if target["hp_current"] <= 0:
        apply_defeat(target, declared_lethality(parsed, player_input))
        if disposition_of(target) == "unconscious":
            return "and it is knocked unconscious!"
        return "and it falls!"
    return f"({target['hp_current']}/{target['hp_max']} HP left)"


def _healed_hero(t: _Turn) -> str:
    """Who a healing effect restores: the named ally of a party fight (EDG-049), else the caster."""
    ally = t.inp.decl.ally_target
    return str(ally.get("display_name") or ally["name"]) if ally is not None else t.rec.hero


def _apply_heal(t: _Turn, amount: int) -> str:
    """Restore `amount` HP to the caster, or to the ally the declaration names. Returns the engine's
    sentence about an ally ('' for the caster, whose sentence the caller already writes)."""
    rec, ally = t.rec, t.inp.decl.ally_target
    if ally is None or rec.party is None:
        rec.hp = min(t.inp.hp_max, rec.hp + amount)
        return ""
    return rec.party.heal_ally(ally, amount)


def run_player_turn(rec: RoundRecord, inp: PlayerTurnInput) -> PlayerTurnResult:
    """Resolve the hero's declared slot for this round and return what it produced."""
    t = _Turn(rec=rec, inp=inp, declared=inp.decl.declared)
    _start_slot(t)
    _action_surge(t)
    phases = []
    for kind in inp.decl.execution_order:
        # An off-hand strike is resolved after the main attack by _attack, never twice.
        if kind == "off_hand_attack":
            continue
        phase = phase_for(kind, inp.decl.declared)
        if phase not in phases:
            phases.append(phase)
    if not phases:
        phases = ["bonus", "move", "action"]  # legacy/readied declarations
    for phase in phases:
        if rec.hp <= 0 or has_effect(rec.player, "stunned") or has_effect(rec.player, "paralyzed"):
            t.player_acted = False
        if phase == "bonus":
            _bonus_actions(t)
        elif phase == "move":
            _declared_moves(t)
        else:
            _grid_reach(t)
            _resolve_action(t)
    if "action" not in phases:
        _grid_reach(t)
        _resolve_action(t)  # records a move/bonus-only turn, without inventing another action
    _contingency(t)
    return PlayerTurnResult(
        player_acted=t.player_acted,
        player_action=t.player_action,
        bonus_record=t.bonus_record,
        check_result=t.check_result,
        dealt=t.dealt,
        inventory_used=t.inventory_used,
        spell_expended=t.spell_expended,
        extra_outcome=" ".join(part for part in (t.bonus_text, t.extra_outcome) if part),
        declared=t.declared,
        later_swing_dice=t.later_swing_dice,
    )


def _death_save_at_turn_start(rec: RoundRecord) -> None:
    """A hero at 0 HP rolls a death saving throw at the start of its turn (EDG-045, agent.death).

    A stable hero does not roll. The roll is the engine's d20, logged with the round's dice.
    """
    record = record_from(rec.player.get(DEATH_SAVES_KEY)) or DeathRecord(DYING)
    if record.state != DYING:
        return
    save = death_save(record)
    hero = rec.hero
    rec.dice_log.append(save.dice.to_log(f"{hero} death saving throw"))
    rec.hp = save.hp
    rec.player[DEATH_SAVES_KEY] = save.record.to_dict() if save.record is not None else None
    if save.regained:
        rec.log_lines.append(
            f"{hero} rolls a 20 on a death saving throw, regains 1 HP and is conscious again."
        )
    elif save.record is not None and save.record.state == DEAD:
        rec.dead = True
        rec.log_lines.append(f"{hero} rolls {save.natural} on a death saving throw and dies.")
    elif save.record is not None and save.record.state == STABLE:
        rec.log_lines.append(f"{hero} succeeds on a third death saving throw and is stable.")
    else:
        verdict = "succeeds" if save.success else "fails"
        rec.log_lines.append(
            f"{hero} {verdict} a death saving throw (rolled {save.natural}): "
            f"{save.record.successes if save.record else 0}/3 successes, "
            f"{save.record.failures if save.record else 0}/3 failures."
        )


def _start_slot(t: _Turn) -> None:
    rec, inp = t.rec, t.inp
    player, hero, combat_state = rec.player, rec.hero, rec.combat_state
    decl = inp.decl
    inp.turns.before_initiative_slot(player)
    if rec.death_mode == DEATH_MODE_SAVES and rec.hp <= 0 and not rec.dead:
        _death_save_at_turn_start(rec)
    # An enemy ahead in initiative may have stunned the hero just now (EDG-043).
    t.player_acted = (
        rec.hp > 0
        and not has_effect(player, "stunned")
        and not has_effect(player, "paralyzed")
        and not player.get("surprised")
    )
    # EDG-044: a Dodge lasts until the hero's next turn starts, which is here (after the foes that
    # act ahead of the hero in initiative). A Dodge declared this turn applies to the foes after.
    release_effect(player, "dodging")
    # EDG-044c: the hero's reaction comes back at the start of its own turn (PDF p.191). A readied attack
    # not yet triggered lapses here: it was held until the hero's next turn (PDF p.194).
    if player.get("reaction_used"):
        start_turn(
            player
        )  # written only when a reaction was spent, so a plain turn's row keeps its shape
    if _lapse_readied(player):
        rec.log_lines.append(f"{hero}'s readied attack lapses: its trigger did not occur.")
    if t.player_acted and decl.standing_up and has_effect(player, "prone"):
        if grid_active(combat_state) and hero_budget(player, dash=False) == 0:
            rec.log_lines.append(f"{hero} cannot stand up now: speed is zero.")
        else:
            if grid_active(combat_state):
                t.moved_ft += hero_budget(player, dash=False) // 2
            release_effect(player, "prone")
            rec.log_lines.append(f"{hero} stands up and is no longer prone.")
    # A rage lasts 1 minute (PHB, PDF p.49), counted from the round it began in.
    if player.get("raging") and (
        int(combat_state.get("round", 1) or 1) - int(player.get("rage_round") or 1) >= RAGE_ROUNDS
    ):
        player.pop("raging")
        rec.log_lines.append(f"{hero}'s rage ends after 1 minute.")


def _bonus_actions(t: _Turn) -> None:
    """The declared bonus action, in its validated position beside the turn's action."""
    rec, inp = t.rec, t.inp
    player, character, hero = rec.player, rec.character, rec.hero
    combat_state = rec.combat_state
    decl = inp.decl
    dice_log, log_lines = rec.dice_log, rec.log_lines
    if t.player_acted and decl.wants_second_wind:
        sw = spend_second_wind(character, remaining=rec.sw_left)
        if sw is None:
            # Refused without a roll: no uses left until a short or long rest. No bonus action is spent.
            t.bonus_text = SECOND_WIND_REFUSAL.format(hero=hero)
            t.bonus_record = {"action": "second_wind", "result": "already_used"}
            t.declared = [d for d in t.declared if d is not SECOND_WIND]
        else:
            hp_before_healing = rec.hp
            rec.hp = min(inp.hp_max, rec.hp + sw.amount)
            restored = rec.hp - hp_before_healing
            dice_log.append({**sw.dice.to_log("second wind"), "applied": restored})
            rec.sw_left -= 1
            t.bonus_text = f"{hero} uses Second Wind and regains {restored} HP."
            if restored != sw.amount:
                t.bonus_text += f" The healing roll was {sw.amount}; maximum HP limits recovery."
            t.bonus_record = {
                "action": "second_wind",
                "result": "used",
                "recovered": restored,
                "rolled": sw.amount,
            }
            rec.round_effects.append("bonus_action")
            rec.events.append(
                {
                    "kind": "second_wind",
                    "text": t.bonus_text,
                    "actor_id": stable_id(player),
                }
            )
        log_lines.append(t.bonus_text)
    if t.player_acted and decl.bonus_spell is not None:
        from agent.combat_grid import spell_reach_check

        if spell_reach_check(
            combat_state, decl.bonus_spell.spell, player, decl.ally_target or player
        ):
            log_lines.append(
                f"The current positions prevent {decl.bonus_spell.spell.name}; no spell slot is spent."
            )
            return
        heal_result = resolve_spell(character, decl.bonus_spell)
        dice_log += heal_result.dice_log
        t.spell_expended = heal_result.expended
        ally_note = _apply_heal(t, heal_result.healing)
        t.bonus_text = (narrate(heal_result, hero=_healed_hero(t), target=None) + ally_note).strip()
        t.bonus_record = {
            "action": "catalog_spell",
            "spell": heal_result.spec.name,
            "healing": heal_result.healing,
        }
        rec.round_effects.append("bonus_action")
        log_lines.append(t.bonus_text)
    # Class features (EDG-145). Rage is a bonus action (EDG-044): it sits beside the turn's attack.
    if t.player_acted and not rec.fled and RAGE in inp.cf_requested and not player.get("raging"):
        if inp.cf_left.get(RAGE, 0) > 0:
            inp.cf_left[RAGE] = spend_use(character, RAGE, inp.cf_left[RAGE])
            player["raging"] = True
            player["rage_round"] = int(combat_state.get("round", 1) or 1)
            rec.round_effects.append(RAGE)
            log_lines.append(
                f"{hero} flies into a rage!"
                + (" Heavy armor blocks its benefits." if wears_heavy_armor(character) else "")
            )
            t.bonus_record = t.bonus_record or {"action": "rage", "result": "started"}


def _action_surge(t: _Turn) -> None:
    """Action Surge adds a second swing, and only when the player attacks a foe (EDG-145)."""
    rec, inp = t.rec, t.inp
    decl = inp.decl
    hero = rec.hero
    t.surge_swing = bool(
        t.player_acted
        and not rec.fled
        and decl.is_attack
        and decl.target is not None
        and ACTION_SURGE in inp.cf_requested
        and inp.cf_left.get(ACTION_SURGE, 0) > 0
    )
    if t.surge_swing:
        inp.cf_left[ACTION_SURGE] = spend_use(rec.character, ACTION_SURGE, inp.cf_left[ACTION_SURGE])
        rec.round_effects.append(ACTION_SURGE)
        rec.log_lines.append(f"{hero} uses Action Surge and gets one more attack.")


def _declared_moves(t: _Turn) -> None:
    """Declared moves happen before the action. Leaving a foe's melee band provokes one opportunity
    attack from it, unless the hero took Disengage this turn (PDF p.193, p.196)."""
    decl = t.inp.decl
    if not (t.player_acted and not t.rec.fled):
        return
    _apply_moves(
        t,
        decl.moves,
        disengage=t.disengaged,
        dash=decl.dash,
        text=movement_text(t.inp.player_input),
    )
    if decl.dash:
        t.rec.round_effects.append("dash")
    if decl.disengage:
        t.rec.round_effects.append("disengage")


def _apply_moves(
    t: _Turn, moves: MovePlan, *, disengage: bool, dash: bool, text: str | None = None
) -> None:
    """Apply a planned move to the foes: bands, recorded distances, and the opportunity attacks it
    provokes. A foe the fear rule blocks keeps its band and distance (EDG-145). On the combat grid the
    hero walks a route instead (`_grid_moves`)."""
    if grid_active(t.rec.combat_state) and moves.route is not None:
        _grid_moves(t, moves, disengage=disengage, dash=dash, text=text)
        return
    rec, inp = t.rec, t.inp
    player, hero = rec.player, rec.hero
    log_lines = rec.log_lines
    actual_steps: list[tuple[str, list[dict]]] = []
    blocked: set[int] = set()
    for band, foes in moves.steps:
        allowed: list[dict] = []
        for foe in foes:
            refusal_after_enemy_turn = (
                fear_refusal(hero, player, [(band, [foe])])
                if band == MELEE and band_of(foe) == RANGED
                else None
            )
            if refusal_after_enemy_turn:
                t.runtime_fear_blocked = True
                blocked.add(id(foe))
                moves.bands[id(foe)] = band_of(foe)
                log_lines.append(f"{refusal_after_enemy_turn.split('. ')[0]}; {foe['name']} remains beyond 5 feet.")
                continue
            allowed.append(foe)
            provoked = (
                band == RANGED
                and band_of(foe) == MELEE
                and not disengage
                and rec.hp > 0
                and can_act(foe)
                and not foe.get("surprised")
                and not foe.get("reaction_used")
            )
            if provoked:
                inp.turns.opportunity_attack(foe)
            foe["band"] = band
            moves.bands[id(foe)] = band
        if allowed:
            actual_steps.append((band, allowed))
            for foe in allowed:
                rec.events.append({
                    "kind": "withdraw" if band == RANGED else "close",
                    "text": f"{hero} {'withdraws from' if band == RANGED else 'closes with'} {foe['name']}.",
                    "actor_id": stable_id(player), "target_id": stable_id(foe),
                })
    moves.steps = actual_steps
    for row, distance in moves.distances:
        if id(row) not in blocked:
            row["distance_ft"] = distance  # a feet move's recorded distance (positioning.distance_of)


def _grid_moves(t: _Turn, moves: MovePlan, *, disengage: bool, dash: bool, text: str | None) -> None:
    """Combat grid: walk the declared moves. The route is planned again from the squares as they are
    now, because foes ahead of the hero in initiative may have moved since the declaration. A move that
    can no longer be made is logged and the hero holds position; no move is invented in its place."""
    rec = t.rec
    player = rec.player
    start = pos_of(player)
    if start is None:
        return
    living = [e for e in rec.enemies if can_act(e)]
    budget = hero_budget(player, dash=dash) - t.moved_ft
    route = plan_route(
        rec.combat_state, player, text, moves, living, budget_ft=budget, hero_name=rec.hero, dash=dash
    )
    if isinstance(route, str):
        rec.log_lines.append(f"{rec.hero} cannot make the declared move now: {route.split('. ')[0]}.")
        route = Route(start)
    if refusal := grid_fear_refusal(rec.combat_state, player, route):
        t.runtime_fear_blocked = True
        rec.log_lines.append(f"{refusal.split('. ')[0]}; {rec.hero} holds position.")
        route = Route(start)
    walked = walk_hero(t.inp.turns, route, disengage=disengage)
    t.moved_ft += walked.cost_ft
    project(moves, walked, living)


def _grid_reach(t: _Turn) -> None:
    """Combat grid: the declared attack's reach and range, checked again from where the hero stands
    once its moves are made (a foe may have moved). Out of reach, the attack is not rolled."""
    decl, rec = t.inp.decl, t.rec
    t.long_range = decl.attack_long_range
    target = decl.target
    if (
        not grid_active(rec.combat_state)
        or target is None
        or decl.is_spell
        or not (decl.is_attack or decl.combat_action is not None)
        or (decl.combat_action is not None and decl.combat_action.kind == "release")
        or pos_of(rec.player) is None
    ):
        return
    weapon = choose_weapon(rec.character, t.inp.player_input, prefer_ranged=decl.attack_ranged)
    refusal, t.long_range = attack_reach_check(
        rec.hero,
        target,
        from_square=pos_of(rec.player),  # type: ignore[arg-type]
        weapon_name=weapon.name if decl.combat_action is None else None,
        ranged=decl.attack_ranged and decl.combat_action is None,
        battlefield=battlefield_of(rec.combat_state),
    )
    t.out_of_reach = refusal is not None


def _contingency(t: _Turn) -> None:
    """The turn's "if" clause, after its action (EDG-182). The condition is read from the recorded state;
    a branch that holds is applied through the same path as a declared move, and one that does not hold
    is logged, so nothing happens silently."""
    decl, rec = t.inp.decl, t.rec
    clause = decl.contingency
    if clause is None or rec.fled or not t.player_acted:
        return
    target = decl.target
    holds_now = contingency_holds(
        clause,
        target_standing=is_standing(target) if target is not None else None,
        hero_hp=rec.hp,
    )
    if not holds_now:
        rec.log_lines.append(not_held_text(clause))
        return
    foes = [e for e in rec.enemies if can_act(e)]
    plan = plan_moves(
        clause.branch,
        [],
        foes,
        pick=lambda n: None,
    )
    if plan.question:
        rec.log_lines.append(f"{clause.describe()} holds, but the branch could not be carried out: {plan.question}")
        return
    rec.log_lines.append(f"{clause.describe()} holds, so {rec.hero} carries on: {clause.branch}.")
    rec.round_effects.append("contingency")
    if grid_active(rec.combat_state) and pos_of(rec.player) is not None:
        plan.route = Route(pos_of(rec.player))  # type: ignore[arg-type]
    _apply_moves(t, plan, disengage=t.disengaged, dash=decl.dash, text=clause.branch)


def _resolve_action(t: _Turn) -> None:
    """The action slot: one branch per declared kind, the first that applies (the check is the fallback)."""
    rec, inp = t.rec, t.inp
    player, hero, character = rec.player, rec.hero, rec.character
    decl = inp.decl
    log_lines = rec.log_lines
    if not t.player_acted:
        if rec.hp > 0:
            if player.get("surprised"):
                t.player_action = {"action": "none", "result": "surprised"}
                rec.round_effects.append("surprised")
                log_lines.append(f"{hero} is surprised and cannot act on this first turn.")
            elif has_effect(player, "paralyzed"):
                t.player_action = {"action": "none", "result": "paralyzed"}
                rec.round_effects.append("paralyzed")
                log_lines.append(f"{hero} is paralyzed and cannot act.")
            else:
                t.player_action = {"action": "none", "result": "stunned"}
                rec.round_effects.append("stunned")
                log_lines.append(f"{hero} is stunned and cannot act.")
        else:
            log_lines.append(f"{hero} is down before they can act.")
    elif rec.fled:
        t.player_action = {"action": "flee", "result": "fled"}
        log_lines.append(f"{hero} disengages and flees the fight.")
    elif decl.missing_item:
        wanted = getattr(inp.parsed, "uses_item", None) or "that item"
        t.extra_outcome = f"{hero} reaches for {wanted}, but does not have it."
        t.player_action = {"action": "item", "result": "not_carried"}
        log_lines.append(t.extra_outcome)
    elif decl.heal_item:
        _healing_item(t, decl.heal_item)
    elif decl.dodge:
        add_effect(player, "dodging", source="dodge", round_applied=int(rec.combat_state.get("round", 1) or 1))
        rec.round_effects.append("dodge")
        t.extra_outcome = f"{hero} takes the Dodge action: attacks against {hero} have disadvantage until {hero}'s next turn."
        log_lines.append(t.extra_outcome)
    elif decl.spell_plan is not None and decl.spell_plan.spell.action == ACTION:
        _catalog_spell(t, decl.spell_plan)
    elif decl.spell_declared:
        # Narrative ruling only; appended to the round summary below.
        t.extra_outcome = ""
        t.player_action = {"action": "declared_action", "result": "narrative_only"}
    elif decl.ready is not None:
        _ready(t, decl.ready)
    elif decl.combat_action is not None and decl.combat_action.kind == "release":
        _release(t)
    elif t.out_of_reach or (
        not grid_active(rec.combat_state)
        and decl.target is not None
        and not decl.is_spell
        and not decl.attack_ranged
        and (decl.is_attack or decl.combat_action is not None)
        and band_of(decl.target) == RANGED
    ):
        target = decl.target
        action_name = (
            f"maneuver_{decl.combat_action.kind}" if decl.combat_action is not None else "attack"
        )
        t.player_action = {
            "action": action_name,
            "target": stable_id(target),
            "result": "out_of_reach",
        }
        t.extra_outcome = (
            f"{hero} cannot reach {target['name']} from {hero}'s square: it moved out of range."
            if t.out_of_reach
            else f"{hero} cannot reach {target['name']} with a melee attack from beyond 5 feet."
        )
        log_lines.append(t.extra_outcome)
    elif decl.combat_action is not None and decl.target is not None:
        _maneuver(t, decl.combat_action, decl.target)
    elif decl.is_attack and decl.target is not None:
        _attack(t, decl.target)
    elif (
        decl.dash
        or decl.disengage
        or decl.moving
        or decl.standing_up
        or decl.wants_second_wind
        or decl.bonus_spell is not None
        or decl.rage_declared
    ):
        # A bonus action, a move, Dash or Disengage was declared and is resolved above (or costs no
        # roll): the action is that, not a parsed check.
        if decl.dash or decl.disengage:
            t.player_action = {"action": "dash" if decl.dash else "disengage", "result": "taken"}
            if decl.disengage:
                t.disengaged = True
                rec.events.append({
                    "kind": "disengage", "text": f"{hero} takes Disengage; withdrawal this turn does not provoke opportunity attacks.",
                    "actor_id": stable_id(player),
                })
        elif decl.moving:
            moved = (
                decl.moves.route.moved if decl.moves.route is not None else bool(decl.moves.steps)
            )
            t.player_action = {"action": "move", "result": "moved" if moved else "already_there"}
        elif decl.standing_up:
            t.player_action = {"action": "stand_up", "result": "stood_up"}
    else:
        check_result, extra = resolve_from_parsed(
            character, inp.parsed, player_input=inp.player_input, impaired=impaired_checks(player)
        )
        rec.dice_log.extend(extra)
        t.player_action = {
            "action": "check",
            "success": check_result.success if check_result is not None else None,
        }
        t.check_result = check_result
        if check_result is not None:
            log_lines.append(check_result.describe() + ".")


def _healing_item(t: _Turn, heal_item: str) -> None:
    """A healing consumable that is actually carried; it is then removed from the inventory."""
    rec = t.rec
    hero, character = rec.hero, rec.character
    heal = heal_from_consumable(character, heal_item)
    if heal is not None:
        if heal.dice is not None:
            rec.dice_log.append(heal.dice.to_log(f"healing ({heal_item})"))
        ally_note = _apply_heal(t, heal.amount)
        t.inventory_used.append(heal_item)
        if t.inp.decl.ally_target is None:
            t.extra_outcome = f"{hero} uses {heal_item} and regains {heal.amount} HP."
        else:
            t.extra_outcome = f"{hero} uses {heal_item} on {_healed_hero(t)}, restoring {heal.amount} HP.{ally_note}"
        t.player_action = {"action": "item", "item": heal_item, "result": "healed", "healed": heal.amount}
        rec.log_lines.append(t.extra_outcome)
    else:
        t.player_action = {"action": "item", "item": heal_item, "result": "no_effect"}


def _catalog_spell(t: _Turn, spell_plan: CastPlan) -> None:
    """Catalog spell: the engine rolls it; the slot is spent, and damage (if any) lands on the chosen
    foe the same way a weapon hit does. An upcast spell reaches its extra foes, each with its own save,
    from the one slot."""
    rec, inp = t.rec, t.inp
    player, hero, character = rec.player, rec.hero, rec.character
    combat_state, enemies = rec.combat_state, rec.enemies
    decl = inp.decl
    spec = spell_plan.spell
    log_lines = rec.log_lines
    from agent.combat_grid import spell_reach_check

    recipients = [decl.ally_target or player] if spec.kind == "heal" else decl.spell_targets
    if any(
        target is not None and spell_reach_check(combat_state, spec, player, target)
        for target in recipients
    ):
        log_lines.append(f"The current positions prevent {spec.name}; no spell slot is spent.")
        return
    if spec.concentration:
        # Casting a concentration spell ends the caster's earlier one (EDG-043).
        if previous := drop_concentration(player, enemies):
            log_lines.append(f"{hero}'s concentration on {previous} ends.")
    applied_condition: str | None = None
    dealt = t.dealt
    # The spell's narrative lines. `rec.outcomes` holds only enemy attack resolutions (EDG-159).
    spell_lines: list[str] = []
    for spell_target in decl.spell_targets:
        target_stats = stats_of(spell_target) if spell_target else None
        if target_stats is not None:
            save_bonus = target_stats.save_bonus(spell_plan.spell.save_ability or "DEX")
        elif spell_target is not None and spell_plan.spell.save_ability == "DEX":
            # A legacy foe has only its initiative (DEX) bonus; other saves stay unmodified.
            save_bonus = int(spell_target.get("initiative_bonus", 0) or 0)
        else:
            save_bonus = 0
        # Spell attacks are ranged: restraint/prone effects set advantage or disadvantage on them.
        spell_mode = (
            attack_roll_mode(
                player,
                spell_target,
                ranged=True,
                within_five_feet=band_of(spell_target) == MELEE,
            )
            if spell_target is not None and spell_plan.spell.kind == "attack"
            else "normal"
        )
        rec.round_modes.append(spell_mode)
        # A restrained foe makes its Dexterity saves at disadvantage (PHB Appendix A, PDF p.293).
        save_mode = (
            "disadvantage"
            if spell_target is not None
            and spell_plan.spell.save_ability == "DEX"
            and has_effect(spell_target, "restrained")
            else "normal"
        )
        spell_result = resolve_spell(
            character,
            spell_plan,
            target_ac=int(spell_target.get("ac", 12)) if spell_target else None,
            target_save_bonus=save_bonus,
            save_mode=save_mode,
            roll_mode=spell_mode,
        )
        rec.dice_log += spell_result.dice_log
        t.spell_expended = spell_result.expended
        target_name = spell_target["name"] if spell_target else None
        extra_outcome = narrate(spell_result, hero=_healed_hero(t), target=target_name)
        if spell_result.healing:
            extra_outcome = f"{extra_outcome}{_apply_heal(t, spell_result.healing)}"
        if spell_result.damage and spell_target is not None:
            spell_type = spell_plan.spell.damage_type
            dealt, resist_note = adjust_monster_damage(
                spell_target, spell_result.damage, spell_type, magical=True
            )
            status = _strike(
                spell_target, dealt, inp.parsed, inp.player_input, damage_type=spell_type
            )
            inp.turns.monster_concentration_damage(spell_target, dealt)
            extra_outcome = f"{extra_outcome}{resist_note} {status}"
        if spell_target is not None and spell_result.attack is not None:
            consume_mark(spell_target)  # the attack roll spends a mark it was made against
        if spell_result.condition and spell_target is not None and is_standing(spell_target):
            # A foe that the spell defeated keeps no condition (defeat clears effects anyway).
            applied = apply_spell_condition(
                spell_target,
                spell_result.condition,
                source=spec.name,
                round_applied=int(combat_state.get("round", 1) or 1),
                duration_rounds=spec.duration_rounds,
                repeat_save=(
                    {"ability": spec.save_ability, "dc": spell_result.save_dc}
                    if spec.save_ability and spell_result.save_dc is not None
                    else None
                ),
            )
            if applied:
                rec.round_effects.append(spell_result.condition)
                applied_condition = spell_result.condition
                if spec.concentration:
                    start_concentration(
                        player,
                        spec.name,
                        round_applied=int(combat_state.get("round", 1) or 1),
                        duration_rounds=spec.duration_rounds,
                    )
                if ended_spell := drop_concentration_if_incapacitated(spell_target, [player]):
                    log_lines.append(
                        f"{spell_target['name']}'s concentration on {ended_spell} ends while incapacitated."
                    )
            extra_outcome = " ".join(
                [
                    extra_outcome,
                    spell_condition_sentence(
                        spell_result.condition, spell_target["name"], applied=applied
                    ),
                ]
            )
        spell_lines.append(extra_outcome)
    extra_outcome = " ".join(spell_lines)
    t.player_action = {
        "action": "catalog_spell",
        "spell": spell_result.spec.name,
        "target": stable_id(decl.spell_targets[0]) if decl.spell_targets[0] else None,
        "slot_level": spell_result.slot_level,
        "damage": spell_result.damage,
        "healing": spell_result.healing,
        "condition": applied_condition,
    }
    if spell_result.attack is not None:
        t.check_result = spell_result.attack
        log_lines.append(f"{t.check_result.describe()}. {extra_outcome}")
    else:
        log_lines.append(extra_outcome)
    t.extra_outcome = extra_outcome
    t.dealt = dealt


def _release(t: _Turn) -> None:
    """Releasing a foe the hero has grappled or restrained (EDG-021, EDG-044c)."""
    rec, decl = t.rec, t.inp.decl
    hero = rec.hero
    t.player_action = {"action": "release", "result": "nothing_bound"}
    if decl.release_target is None:
        t.extra_outcome = f"{hero} has nothing bound to release."
    else:
        # A grapple (PDF p.196) and a restraint from another source both end when the hero lets go.
        released = [kind for kind in ("grappled", "restrained") if release_effect(decl.release_target, kind)]
        t.player_action = {
            "action": "release",
            "target": stable_id(decl.release_target),
            "result": "released",
        }
        t.extra_outcome = f"{hero} releases {decl.release_target['name']}; it is no longer {released[0] if released else 'held'}."
    rec.log_lines.append(t.extra_outcome)


def _lapse_readied(player: dict) -> bool:
    """At the start of the hero's turn an unfired readied attack lapses; a standing order stays (EDG-182)."""
    before = effects_of(player)
    after = [e for e in before if not (e["kind"] == "readied" and not e.get("standing"))]
    if len(after) == len(before):
        return False
    player["effects"] = after
    return True


def standing_order_foes(player: dict) -> list[str]:
    """The foes the hero's standing orders name, in the order they were given."""
    return [
        str(e.get("foe_name"))
        for e in effects_of(player)
        if e["kind"] == "readied" and e.get("standing") and e.get("foe_name")
    ]


def cancel_standing_orders(player: dict, foes: list[str]) -> list[str]:
    """Cancel the standing orders against the named foes. Returns the cancelled foes' names. No roll is
    made and no turn is spent (EDG-182)."""
    cancelled: list[str] = []
    kept: list[dict] = []
    wanted = {f.lower() for f in foes}
    for entry in effects_of(player):
        named = str(entry.get("foe_name") or "")
        if entry["kind"] == "readied" and entry.get("standing") and named.lower() in wanted:
            cancelled.append(named)
            continue
        kept.append(entry)
    if cancelled:
        player["effects"] = kept
    return cancelled


def _ready(t: _Turn, order: ReadyOrder) -> None:
    """The Ready action (PDF p.194): the attack is held for its trigger, with no roll now (EDG-044c).

    The effect is typed and visible; `fire_readied` resolves it when the foe closes in. A second readied
    attack while one is held is refused by the effect model, so at most one is held at a time.
    """
    rec = t.rec
    player, hero = rec.player, rec.hero
    round_num = int(rec.combat_state.get("round", 1) or 1)
    if not add_effect(
        player,
        "readied",
        source=f"attack {order.foe_name} when it closes in",
        source_id=order.foe_id,
        round_applied=round_num,
    ):
        t.player_action = {"action": "ready", "result": "already_readied"}
        t.extra_outcome = f"{hero} already has a readied attack."
        rec.log_lines.append(t.extra_outcome)
        return
    for entry in player.get("effects") or []:
        if entry.get("kind") == "readied":
            entry["weapon_hint"] = order.weapon_hint  # the weapon the words name, read when it fires
            entry["standing"] = bool(order.standing)  # kept across triggers until cancelled (EDG-182)
            entry["foe_name"] = order.foe_name  # the foe a standing order names, for the public view
    rec.round_effects.append("readied")
    t.player_action = {"action": "ready", "target": order.foe_id, "result": "readied"}
    t.extra_outcome = (
        f"{hero} readies an attack: when {order.foe_name} closes in, {hero} attacks with the reaction."
    )
    rec.log_lines.append(t.extra_outcome)
    if order.standing:
        rec.log_lines.append(f"{hero}'s standing order holds until {hero} cancels it.")


def fire_readied(rec: RoundRecord, turns: EnemyTurns, foe: dict) -> None:
    """A foe has just closed into melee on its own turn: the hero's readied attack, if any, resolves now.

    The hero's reaction makes it (PDF p.194), before the foe's own attacks (PDF p.191). It fires once,
    only when the hero can react and the foe is still standing, and it spends the reaction until the
    hero's next turn. The attack is the engine's ordinary melee attack roll and damage.
    """
    player = rec.player
    entry = next(
        (
            e
            for e in effects_of(player)
            if e["kind"] == "readied" and e.get("source_id") == stable_id(foe)
        ),
        None,
    )
    if (
        entry is None
        or rec.hp <= 0
        or player.get("reaction_used")
        or not can_act(player)
        or not is_standing(foe)
    ):
        return
    entry_standing = bool(entry.get("standing"))
    if not entry_standing:
        release_effect(player, "readied")  # a standing order is kept and fires again next time
    player["reaction_used"] = True
    rec.round_effects.append("readied_attack")
    rec.log_lines.append(f"{foe['name']} closes in, and {rec.hero} makes the readied attack.")
    decl = Declaration(
        is_attack=True,
        is_spell=False,
        spell_declared=False,
        spell_plan=None,
        bonus_spell=None,
        dodge=False,
        dash=False,
        disengage=False,
        moving=False,
        standing_up=False,
        wants_second_wind=False,
        rage_declared=False,
        heal_item=None,
        missing_item=False,
        combat_action=None,
        declared=[],
        target=foe,
        release_target=None,
        spell_target=None,
        spell_targets=[None],
        moves=MovePlan(),
        attack_ranged=False,
        off_weapon=None,
    )
    inp = PlayerTurnInput(
        decl=decl,
        player_input=str(entry.get("weapon_hint") or ""),
        parsed=None,
        hp_max=rec.hp,  # read only by Second Wind, which a reaction never uses
        cf_requested=(),
        cf_left={},
        turns=turns,
    )
    swing = _Turn(rec=rec, inp=inp)
    _attack(swing, foe, readied=True)


def _maneuver(t: _Turn, combat_action: CombatManeuver, target: dict) -> None:
    """A contested Athletics maneuver against the foe's Dexterity (EDG-021). Engine dice decide it."""
    rec = t.rec
    player, hero, character = rec.player, rec.hero, rec.character
    combat_state, log_lines = rec.combat_state, rec.log_lines
    check_mode = "disadvantage" if impaired_checks(player) else "normal"
    # The foe chooses Strength (Athletics) or Dexterity (Acrobatics), so its better modifier counts (PDF p.196).
    won, mine, theirs, mine_draws = _contest(
        check_spec(character, "athletics").modifier,
        contest_defender_bonus(target),
        attacker_mode=check_mode,
    )
    rec.dice_log.extend(
        [
            _mode_log(mine, "athletics contest", check_mode, mine_draws),
            theirs.to_log(f"{target['name']} contest"),
        ]
    )
    verdict = "success" if won else "failure"
    contest_line = (
        f"contest: {hero} athletics{f' at {check_mode}' if check_mode != 'normal' else ''} "
        f"{mine.expression} = {mine.total} "
        f"(d20 rolled {mine.rolls[0]}) vs {target['name']} {theirs.expression} = {theirs.total} "
        f"(d20 rolled {theirs.rolls[0]}) -> {verdict}"
    )
    target_name = target["name"]
    kind = combat_action.kind
    if not won:
        extra_outcome = (
            f"{hero} fails to hold {target_name}."
            if kind == "grappled"
            else f"{hero} fails to shove {target_name}."
        )
    elif add_effect(
        target,
        kind,
        source=combat_action.source,
        round_applied=int(combat_state.get("round", 1) or 1),
    ):
        rec.round_effects.append(kind)
        if kind == "grappled":
            extra_outcome = f"{hero} grapples {target_name} with {combat_action.source}."
        else:
            extra_outcome = f"{hero} shoves {target_name} prone."
    else:
        extra_outcome = f"{target_name} is already {'grappled' if kind == 'grappled' else 'prone'}."
    t.player_action = {
        "action": f"maneuver_{kind}",
        "target": stable_id(target),
        "won": bool(won),
        "effect_applied": bool(won and kind in rec.round_effects),
    }
    log_lines.append(f"{contest_line}. {extra_outcome}")
    t.extra_outcome = extra_outcome


def _attack(t: _Turn, target: dict, *, readied: bool = False) -> None:
    """One swing, or two with Action Surge (EDG-145), then the off-hand attack's bonus action (EDG-044).
    Each swing rolls its own engine d20."""
    rec, inp = t.rec, t.inp
    player, hero, character = rec.player, rec.hero, rec.character
    decl = inp.decl
    parsed, player_input = inp.parsed, inp.player_input
    dice_log, log_lines = rec.dice_log, rec.log_lines
    ranged = decl.attack_ranged
    off_weapon = decl.off_weapon
    is_spell = decl.is_spell
    any_hit = False
    swings = [False] * (2 if t.surge_swing else 1) + ([True] if off_weapon else [])
    for swing, off_hand_swing in enumerate(swings):
        if off_hand_swing:
            rec.round_effects.append("bonus_action")
            t.bonus_record = {"action": "off_hand_attack", "weapon": off_weapon.name}
        if swing and not is_standing(target):
            # An earlier swing finished the foe; the later one has nothing to hit.
            kind = "off-hand" if off_hand_swing else "extra"
            log_lines.append(f"{hero}'s {kind} attack finds no foe left standing.")
            break
        mode = attack_roll_mode(
            player,
            target,
            ranged=ranged,
            within_five_feet=ranged and band_of(target) == MELEE,
            long_range=t.long_range and not off_hand_swing,
        )
        attack = resolve_attack(
            character,
            int(target.get("ac", 12)),
            spell=is_spell,
            hint=off_weapon.name if off_hand_swing else player_input,
            roll_mode=mode,
        )
        rec.round_modes.append(mode)
        consume_mark(target)  # the attack roll spends a mark it was made against
        if attack.success and has_effect(target, "paralyzed"):
            weapon_name = (attack.weapon or choose_weapon(character, player_input)).name
            if paralyzed_melee_crit(target, weapon_name=weapon_name, ranged=ranged, spell=is_spell):
                attack.critical = True  # PHB Appendix A: a paralyzed foe's hit from 5 feet is a crit
        if t.check_result is None:
            t.check_result = attack
        dice_log.extend(attack.dice_log)
        swing_log = dice_log
        if swing:
            t.later_swing_dice.extend(attack.dice_log)
        with_text = f" with the off-hand {off_weapon.name}" if off_hand_swing else ""
        if attack.success:
            any_hit = True
            rage_bonus = rage_damage_bonus(character, attack, raging=bool(player.get("raging")))
            swing_dealt, dmg_roll = roll_attack_damage(
                character, attack, hint=player_input, bonus=rage_bonus, off_hand=off_hand_swing
            )
            swing_log.append(dmg_roll.to_log("player damage"))
            sneak = sneak_attack_roll(character, attack, mode=mode, used=t.sneak_used)
            notes = ""
            if sneak is not None:
                swing_log.append(sneak.to_log("sneak attack"))
                swing_dealt += sneak.total
                t.sneak_used = True
                rec.round_effects.append(SNEAK_ATTACK)
                notes += f" Sneak Attack adds {sneak.total}."
            if rage_bonus:
                notes += f" Rage adds {rage_bonus}."
            weapon = attack.weapon or choose_weapon(character, player_input)
            damage_type = weapon_damage_type(weapon.name)
            swing_dealt, resist_note = adjust_monster_damage(
                target, swing_dealt, damage_type, magical=False
            )
            t.dealt += swing_dealt
            status = _strike(target, swing_dealt, parsed, player_input, damage_type=damage_type)
            inp.turns.monster_concentration_damage(target, swing_dealt)
            crit = "Critical hit! " if attack.critical else ""
            swing_text = (
                f"{crit}{hero} hits {target['name']}{with_text} for {swing_dealt} "
                f"damage{resist_note} {status}{notes}"
            )
        else:
            swing_text = f"{hero} misses {target['name']}{with_text}."
        mode_text = "" if mode == "normal" else f" (at {mode})"
        log_lines.append(f"{attack.describe()}{mode_text}. {swing_text}")
        rec.events.append({
            "kind": "readied_attack" if readied else "player_attack",
            "text": ("Readied reaction: " if readied else "") + swing_text,
            "actor_id": stable_id(player), "target_id": stable_id(target),
        })
        t.extra_outcome = " ".join(part for part in (t.extra_outcome, swing_text) if part)
    t.player_action = {
        "action": "spell" if is_spell else "attack",
        "target": stable_id(target),
        "hit": any_hit,
        "damage": t.dealt,
        "target_disposition": disposition_of(target),
    }


def finish_hero_turn(rec: RoundRecord, turns: EnemyTurns) -> None:
    """The hero's own turn ends, after the adjudication (EDG-043, EDG-044): the end-of-turn checks,
    then the hero's slot in the legendary and initiative sequence."""
    player, enemies, character, hero = rec.player, rec.enemies, rec.character, rec.hero
    if not rec.fled:
        if feared := end_fear_of_defeated(player, enemies):
            rec.log_lines.append(
                f"{feared} is down; {hero}'s fear no longer imposes its attack, check or movement "
                "restrictions, but the condition and its repeat save remain."
            )
        for save in hero_end_of_turn_saves(player, character):
            rec.dice_log.append(save.outcome.to_log(f"{hero} {save.ability} save ({save.kind})"))
            rec.log_lines.append(save_line(save))
    player["surprised"] = False
    # Legendary actions: one option per other creature's turn, including the hero's.
    if not rec.fled:
        turns.another_creature_turn_ended(player)
    turns.after_initiative_slot(player)
