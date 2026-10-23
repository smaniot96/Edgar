"""The player's declared action for one round, decided before any die is rolled (EDG-044, EDG-057).

`declare_action` resolves what the player's words ask for into targets and action-economy slots. A
refusal (two actions of one slot, an unknown or unreachable foe, a refused cast) comes back as the
question the player is asked, with no roll, no round and no state change. Everything it does is pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from agent.action_economy import (
    ACTION,
    BONUS_ACTION,
    DASH,
    DISENGAGE,
    DODGE,
    OFF_HAND_ATTACK,
    SECOND_WIND,
    Declared,
    affirmative_matches,
    dash_declared,
    disengage_declared,
    dodge_declared,
    has_attack_words,
    off_hand_declared,
    off_hand_refusal,
    refusal_for,
)
from agent.battlefield import battlefield_of, square_move
from agent.class_features import ACTION_SURGE, RAGE, max_uses
from agent.combat_grid import (
    attack_reach_check,
    grid_active,
    grid_fear_refusal,
    hero_budget,
    plan_route,
    project,
    spell_reach_check,
)
from agent.combat_identity import unique_roster_target
from agent.combat_intent import _RANGED_HINT, _STAND_UP, _is_attack_action, _name_matches
from agent.combat_plan import attack_count_question, operation_order, sequence_question
from agent.combat_targeting import _release_target, additional_spell_targets, select_target
from agent.combat_view import _ask_player
from agent.combatants import (
    CombatManeuver,
    can_act,
    classify_combat_maneuver,
    has_effect,
    is_standing,
)
from agent.conditions import condition_ends_before_turn, fear_refusal, humanoid_refusal
from agent.contingency import TARGET_DOWN, Contingency
from agent.declarations import (
    CONTINGENCY,
    MANEUVER,
    MOVE,
    agreement_question,
    completeness_question,
    unsupported_ruling,
)
from agent.positioning import (
    RANGED,
    MovePlan,
    band_of,
    feet_refusal,
    move_refusal,
    movement_legs,
    movement_steps,
    plan_moves,
    reach_refusal,
)
from agent.readied_action import (
    READY_PHRASE,
    STANDING_PHRASE,
    ReadyOrder,
    foe_is_beyond_reach,
    movement_text,
    ready_order,
)
from agent.resolution import (
    Weapon,
    character_class,
    choose_weapon,
    find_healing_consumable,
    mentions_item_use,
    normalise_check,
    off_hand_weapon,
    second_wind_remaining,
)
from agent.spells import CastPlan, cast_request, target_count
from agent.state import AgentState
from agent.supported_rules import actions_for_state, has_declared_spell


@dataclass(frozen=True)
class Declaration:
    """What the player declared this round, resolved to its targets and action-economy slots."""

    is_attack: bool
    is_spell: bool
    spell_declared: bool
    spell_plan: CastPlan | None
    bonus_spell: CastPlan | None
    dodge: bool
    dash: bool
    disengage: bool
    moving: bool
    standing_up: bool
    wants_second_wind: bool
    rage_declared: bool
    heal_item: str | None
    missing_item: bool
    combat_action: CombatManeuver | None
    declared: list[Declared]
    target: dict | None
    release_target: dict | None
    spell_target: dict | None
    spell_targets: list[dict | None]
    moves: MovePlan
    attack_ranged: bool
    off_weapon: Weapon | None
    ready: ReadyOrder | None = None  # EDG-044c: a validated readied melee attack, held for its trigger
    contingency: Contingency | None = None  # EDG-182: the "if" clause that follows the action
    contingency_moves: MovePlan | None = None  # its branch, planned on the bands before the action
    # EDG-049: the other hero a healing spell or potion is aimed at (a party fight), else the caster.
    ally_target: dict | None = None
    execution_order: tuple[str, ...] = ()
    main_attack_count: int = 1
    # Combat grid: the weapon attack is made beyond its normal range (disadvantage, PHB p.195).
    attack_long_range: bool = False


def declare_action(
    state: AgentState,
    *,
    character: dict,
    player_input: str,
    parsed: Any,
    named: list[str],
    fled: bool,
    hero: str,
    player: dict,
    enemies: list[dict],
    before: list[dict],
    combat_state: dict,
    round_num: int,
    cf_left: dict[str, int],
    cf_requested: tuple[str, ...],
    contingency: Contingency | None = None,
    allies: list[dict] | None = None,
) -> Declaration | dict[str, Any]:
    """Resolve the declared action, or return the refusal as the question the player is asked."""
    # --- Decide the player's action BEFORE any dice, so a bad target never burns a round ---
    if not fled:
        # EDG-182: a mechanic the engine does not execute is refused with its ruling, before any roll.
        ruling = unsupported_ruling(
            player_input, [a.kind for a in getattr(parsed, "actions", None) or []]
        )
        if ruling:
            return _ask_player(state, ruling, combat_state=combat_state)
    is_attack, is_spell = (False, False) if fled else _is_attack_action(parsed, named, player_input)
    # A declared spell never becomes a weapon-style attack roll (EDG-028). A catalog spell the
    # character can cast resolves through agent.spells; any other declared spell keeps its ruling.
    actions = actions_for_state(state)
    spell_declared = has_declared_spell(actions)
    spell_plan: CastPlan | None = None
    request = None if fled else cast_request(player_input, parsed, character)
    if request is not None:
        _spell, spell_plan, refusal = request
        if refusal:
            # Refused cast: no roll, no round, no state change (a transparent engine refusal).
            return _ask_player(state, refusal, combat_state=combat_state)
    # EDG-044: a bonus-action spell (Healing Word) is not an action, so it leaves the attack alone.
    bonus_spell = (
        spell_plan if spell_plan is not None and spell_plan.spell.action == BONUS_ACTION else None
    )
    if spell_declared or (spell_plan is not None and bonus_spell is None):
        is_attack, is_spell = False, False
    dodge = not fled and dodge_declared(player_input)
    dash = not fled and dash_declared(player_input)
    disengage = not fled and disengage_declared(player_input)
    # A readied attack's trigger ("when the goblin closes in") is the foe's move, not the hero's.
    moves_text = movement_text(player_input)
    grid = grid_active(combat_state)
    # Combat grid: "move to D4" is a move too.
    moving = not fled and (
        bool(movement_steps(moves_text)) or (grid and square_move(moves_text) is not None)
    )
    standing_up = not fled and bool(affirmative_matches(_STAND_UP, player_input))
    off_hand = not fled and off_hand_declared(player_input)
    # Second Wind is a bonus action (EDG-044): it no longer replaces the attack this turn.
    wants_second_wind = (
        not fled
        and "second wind" in player_input.lower()
        and "fighter" in character_class(character)
    )
    if (
        not fled
        and ACTION_SURGE in cf_requested
        and max_uses(character, ACTION_SURGE) > 0
        and cf_left.get(ACTION_SURGE, 0) <= 0
    ):
        return _ask_player(
            state,
            "Action Surge has no uses left until a short or long rest. "
            "Choose one attack or another available action. No roll is made and no turn is spent.",
            combat_state=combat_state,
        )
    if wants_second_wind and second_wind_remaining(character) <= 0:
        return _ask_player(
            state,
            "Second Wind has no uses left until a short or long rest. "
            "Choose an available action or healing item. No roll is made and no turn is spent.",
            combat_state=combat_state,
        )
    rage_declared = (
        not fled and RAGE in cf_requested and not player.get("raging") and cf_left.get(RAGE, 0) > 0
    )
    if (
        dodge
        or dash
        or disengage
        or moving
        or standing_up
        or wants_second_wind
        or bonus_spell is not None
        or rage_declared
    ):
        # A bonus action, Dodge, Dash, Disengage or a move sits beside an attack only when the player's
        # own words name one.
        is_attack = is_attack and has_attack_words(player_input)
    heal_item = None
    missing_item = False
    if not fled and spell_plan is None:
        heal_item = find_healing_consumable(character, parsed=parsed, player_input=player_input)
        if heal_item or mentions_item_use(parsed, player_input):
            # Using an item is the action; if it isn't carried, the action simply fails.
            missing_item = heal_item is None
            is_attack = False
    # EDG-049: a healing spell or potion that names another hero of the party is aimed at that hero.
    ally_target: dict | None = None
    if (
        allies
        and not fled
        and ((spell_plan is not None and spell_plan.spell.kind == "heal") or heal_item)
    ):
        ally_target = next(
            (
                a
                for n in named
                for a in allies
                if _name_matches(n, str(a.get("display_name") or a["name"]))
            ),
            None,
        )
    # A supported contested maneuver (EDG-021) or a declared release replaces the attack this turn.
    combat_action = None
    if not (fled or spell_declared or heal_item or missing_item or dodge):
        combat_action = classify_combat_maneuver(player_input)
    if combat_action is not None:
        is_attack, is_spell = False, False
    # EDG-044c: Ready takes the action and replaces the attack. Its trigger and foe are checked after the
    # planned moves below, because a retreat that puts the foe at range makes a readied attack valid.
    ready_declared = not fled and bool(READY_PHRASE.search(player_input))
    if ready_declared:
        is_attack, is_spell = False, False
        combat_action = None
    # EDG-044: the slots this turn declares. Two of one slot are refused with no roll and no round.
    declared: list[Declared] = []
    if dodge:
        declared.append(DODGE)
    if dash:
        declared.append(DASH)
    if disengage:
        declared.append(DISENGAGE)
    if is_attack:
        declared.append(Declared("attack", ACTION, "attack"))
    if combat_action is not None and combat_action.kind != "release":
        declared.append(Declared("maneuver", ACTION, f"{combat_action.kind} maneuver"))
    if heal_item or missing_item:
        declared.append(Declared("item", ACTION, "item use"))
    if spell_plan is not None:
        declared.append(Declared("spell", spell_plan.spell.action, spell_plan.spell.name))
    elif spell_declared:
        declared.append(Declared("spell", ACTION, "spell"))
    if wants_second_wind:
        declared.append(SECOND_WIND)
    if rage_declared:
        declared.append(Declared("rage", BONUS_ACTION, "Rage"))
    if off_hand:
        declared.append(OFF_HAND_ATTACK)
    if ready_declared:
        declared.append(Declared("ready", ACTION, "Ready"))
    if is_attack:
        attack_count = attack_count_question(
            player_input,
            parsed,
            hero=hero,
            available=1
            + int(off_hand)
            + int(ACTION_SURGE in cf_requested and cf_left.get(ACTION_SURGE, 0) > 0),
        )
        if attack_count:
            return _ask_player(state, attack_count, combat_state=combat_state)
    execution_order = operation_order(declared, moving, parsed, player_input)
    if question := sequence_question(execution_order, player_input, hero):
        return _ask_player(state, question, combat_state=combat_state)
    declared.sort(key=lambda d: execution_order.index(d.kind))
    refusal = refusal_for(declared, hero)
    if refusal:
        return _ask_player(state, refusal, combat_state=combat_state)
    # EDG-182: the parser's proposed action list is adopted only where the engine detected the same
    # action. A disagreement is one question, asked before any target, roll or turn spend.
    if not fled:
        detected = {d.kind for d in declared}
        if ACTION_SURGE in cf_requested:
            detected.add(ACTION_SURGE)
        if moving:
            detected.add(MOVE.kind)
        if combat_action is not None:
            detected.add(MANEUVER.kind)
        if contingency is not None:
            detected.add(CONTINGENCY.kind)
        actions = list(getattr(parsed, "actions", None) or [])
        question = agreement_question([(a.kind, a.text) for a in actions], detected, hero)
        if question is None:
            # EDG-182 completeness: a proposed attack needs a foe when several stand.
            living = [e["name"] for e in enemies if is_standing(e)]
            question = completeness_question(actions, living, player_input, hero)
        if question:
            return _ask_player(state, question, combat_state=combat_state)
        idle = bool(
            re.search(
                r"\b(?:i|we)\s+(?:wait|do\s+nothing|hold\s+(?:my|our)\s+(?:action|turn))\b",
                player_input,
                re.I,
            )
        )
        nonattack_check = normalise_check(getattr(parsed, "check", None))
        if nonattack_check and nonattack_check[1] in ("attack", "spell_attack"):
            nonattack_check = None
        if (
            not declared
            and not moving
            and not standing_up
            and combat_action is None
            and player_input
            and not idle
            and not nonattack_check
        ):
            return _ask_player(
                state,
                "What do you do this turn? I have not declared an attack. You can speak, offer surrender, "
                "attack, Dodge, Disengage, or move. No roll is made and no turn is spent.",
                combat_state=combat_state,
            )
    for name in named:
        participant = next(
            (
                p
                for p in player.get("encounter_participants", [])
                if p.get("participation") == "visible_nonparticipant"
                and _name_matches(name, p.get("name", ""))
            ),
            None,
        )
        if participant is not None and (
            is_attack or combat_action is not None or spell_plan is not None
        ):
            return _ask_player(
                state,
                f"{participant['name']} is visible but is outside this encounter's active roster. The engine cannot add that actor to this fight automatically. Choose an active foe or a different approach. No roll is made and no turn is spent.",
                combat_state=combat_state,
            )
    target: dict | None = None
    release_target: dict | None = None
    if combat_action is not None and combat_action.kind == "release":
        release_target = _release_target(enemies, named)
    elif is_attack or combat_action is not None:
        target, question = select_target(enemies, named)
        if question:
            return _ask_player(state, question, combat_state=combat_state)
    spell_target: dict | None = None
    spell_targets: list[dict | None] = [None]  # a heal has no foe
    if spell_plan is not None and spell_plan.spell.kind != "heal":
        spell_target, question = select_target(enemies, named)
        if question:
            return _ask_player(state, question, combat_state=combat_state)
        if spell_target is None:
            return _ask_player(
                state, f"Who do you cast {spell_plan.spell.name} at?", combat_state=combat_state
            )
        if spell_plan.spell.humanoid_only:
            # Refused before any roll or slot: a foe whose record gives no humanoid type is not a target.
            refusal = humanoid_refusal(spell_plan.spell.name, spell_target)
            if refusal:
                return _ask_player(state, refusal, combat_state=combat_state)
        spell_targets = [spell_target]
        if spell_plan.spell.upcast_targets:
            spell_targets += additional_spell_targets(
                enemies,
                named,
                spell_target,
                spell_plan.spell,
                target_count(spell_plan.spell, spell_plan.slot_level) - 1,
            )
    # EDG-044: declared moves are free but change band at most once (twice with Dash). Planned on
    # copies only: a refused turn changes no foe and rolls no die.
    branch_text = contingency.branch if contingency is not None else ""
    feet_total = sum(
        f
        for _b, _p, f in movement_legs(None if fled else f"{moves_text or ''} {branch_text}")
        if f is not None
    )
    feet_refused = feet_refusal(hero, feet_total, dash=dash) if feet_total else None
    if feet_refused:
        return _ask_player(state, feet_refused, combat_state=combat_state)
    moves = plan_moves(
        None if fled else moves_text,
        named,
        [e for e in enemies if can_act(e)],
        pick=lambda n: unique_roster_target([e for e in enemies if can_act(e)], n),
    )
    if moves.question:
        return _ask_player(state, moves.question, combat_state=combat_state)
    branch_plan: MovePlan | None = None
    if contingency is not None:
        branch_refusal = _branch_refusal(hero, contingency, is_attack or combat_action is not None)
        if branch_refusal:
            return _ask_player(state, branch_refusal, combat_state=combat_state)
        branch_plan = plan_moves(
            contingency.branch,
            named,
            [e for e in enemies if can_act(e)],
            pick=lambda n: unique_roster_target([e for e in enemies if can_act(e)], n),
        )
        if branch_plan.question:
            return _ask_player(state, branch_plan.question, combat_state=combat_state)
    fear_will_end_before_turn = condition_ends_before_turn(player, "frightened", before, round_num)
    branch_steps = len(branch_plan.steps) if branch_plan is not None else 0
    if grid and not fled:
        # Combat grid: movement is spent in feet along a route, not counted in band changes.
        living = [e for e in enemies if can_act(e)]
        from agent.battlefield import speed_of, square_moves

        if standing_up and has_effect(player, "prone") and speed_of(player) == 0:
            return _ask_player(
                state,
                "Standing up requires movement, but your speed is zero. No turn is spent.",
                combat_state=combat_state,
            )
        if standing_up and has_effect(player, "prone"):
            move_offsets = [start for _, start, _ in square_moves(moves_text)] + [
                offset for _, offset, _ in movement_legs(moves_text)
            ]
            if (
                move_offsets
                and min(move_offsets) < affirmative_matches(_STAND_UP, player_input)[0].start()
            ):
                return _ask_player(
                    state,
                    "Declare standing up before moving, or crawl while prone. No turn is spent.",
                    combat_state=combat_state,
                )
        budget = hero_budget(player, dash=dash, standing_up=standing_up)
        route = plan_route(
            combat_state,
            player,
            moves_text,
            moves,
            living,
            budget_ft=budget,
            hero_name=hero,
            dash=dash,
            standing_up=standing_up,
        )
        if isinstance(route, str):
            return _ask_player(state, route, combat_state=combat_state)
        project(moves, route, living)
        if branch_plan is not None and contingency is not None:
            branch_route = plan_route(
                combat_state,
                player,
                contingency.branch,
                branch_plan,
                living,
                budget_ft=budget - route.cost_ft,
                hero_name=hero,
                dash=dash,
                start=route.destination,
                standing_up=standing_up,
            )
            if isinstance(branch_route, str):
                return _ask_player(state, branch_route, combat_state=combat_state)
            project(branch_plan, branch_route, living)
        refused_move = (
            None if fear_will_end_before_turn else grid_fear_refusal(combat_state, player, route)
        )
    else:
        refused_move = move_refusal(hero, len(moves.steps) + branch_steps, dash=dash) or (
            None if fear_will_end_before_turn else fear_refusal(hero, player, moves.steps)
        )
    if refused_move:
        return _ask_player(state, refused_move, combat_state=combat_state)
    ready: ReadyOrder | None = None
    if ready_declared:
        order = ready_order(player_input, [e for e in enemies if can_act(e)], hero=hero)
        if isinstance(order, str):
            return _ask_player(state, order, combat_state=combat_state)
        if not foe_is_beyond_reach(order.foe, moves.bands):
            return _ask_player(
                state,
                f"{order.foe_name} is already within 5 feet of {hero} once this turn's moves are made, "
                "so a readied attack would not trigger. Attack it now, or back away first. No roll is "
                "made and no turn is spent.",
                combat_state=combat_state,
            )
        standing = bool(STANDING_PHRASE.search(player_input))
        ready = replace(order, standing=standing) if standing else order
    # A melee attack or grapple cannot reach a foe beyond 5 feet; a ranged weapon or spell can.
    attack_weapon = choose_weapon(
        character, player_input, prefer_ranged=bool(_RANGED_HINT.search(player_input))
    )
    attack_ranged = bool(_RANGED_HINT.search(player_input)) or attack_weapon.ranged
    attack_kind = "attack" if is_attack else "maneuver"
    move_before_action = (
        "move" in execution_order
        and attack_kind in execution_order
        and execution_order.index("move") < execution_order.index(attack_kind)
    )
    attack_long_range = False
    if (
        grid
        and target is not None
        and not is_spell
        and (is_attack or (combat_action is not None and combat_action.kind != "release"))
    ):
        # Combat grid: reach and range are measured from the square the hero's moves end on.
        refusal, attack_long_range = attack_reach_check(
            hero,
            target,
            from_square=(moves.route.destination if move_before_action else moves.route.start),
            weapon_name=attack_weapon.name if combat_action is None else None,
            ranged=attack_ranged and combat_action is None,
            battlefield=battlefield_of(combat_state),
        )
        if refusal:
            return _ask_player(state, refusal, combat_state=combat_state)
    elif not grid and target is not None and not is_spell and not attack_ranged:
        attack_band = (
            moves.bands.get(id(target), band_of(target))
            if "move" in execution_order
            and execution_order.index("move")
            < execution_order.index("attack" if is_attack else "maneuver")
            else band_of(target)
        )
        if attack_band == RANGED and (is_attack or combat_action is not None):
            return _ask_player(
                state, reach_refusal(hero, target["name"]), combat_state=combat_state
            )
    if grid and spell_plan is not None:
        origin = (
            moves.route.destination
            if "move" in execution_order
            and "spell" in execution_order
            and execution_order.index("move") < execution_order.index("spell")
            else moves.route.start
        )
        targets = [ally_target or player] if spell_plan.spell.kind == "heal" else spell_targets
        for recipient in targets:
            if recipient is not None and (
                refusal := spell_reach_check(
                    combat_state, spell_plan.spell, player, recipient, from_square=origin
                )
            ):
                return _ask_player(state, refusal, combat_state=combat_state)
        if spell_plan.spell.name == "Hold Person" and len(targets) > 1:
            from agent.battlefield import distance_ft, pos_of

            first = pos_of(targets[0])
            if first and any(
                pos_of(row) and distance_ft(first, pos_of(row)) > 30 for row in targets[1:]
            ):
                return _ask_player(
                    state,
                    "Hold Person targets must be within 30 feet of the first target. No slot or turn is spent.",
                    combat_state=combat_state,
                )
    # EDG-044: the off-hand attack needs this turn's melee Attack action and a second light weapon.
    off_weapon = off_hand_weapon(character, attack_weapon) if off_hand else None
    if off_hand:
        refusal = off_hand_refusal(
            hero,
            attacked=is_attack and not is_spell and not attack_ranged and target is not None,
            pair=off_weapon is not None,
        )
        if refusal:
            return _ask_player(state, refusal, combat_state=combat_state)
    return Declaration(
        is_attack=is_attack,
        is_spell=is_spell,
        spell_declared=spell_declared,
        spell_plan=spell_plan,
        bonus_spell=bonus_spell,
        dodge=dodge,
        dash=dash,
        disengage=disengage,
        moving=moving,
        standing_up=standing_up,
        wants_second_wind=wants_second_wind,
        rage_declared=rage_declared,
        heal_item=heal_item,
        missing_item=missing_item,
        combat_action=combat_action,
        declared=declared,
        target=target,
        release_target=release_target,
        spell_target=spell_target,
        spell_targets=spell_targets,
        moves=moves,
        attack_ranged=attack_ranged,
        off_weapon=off_weapon,
        ready=ready,
        contingency=contingency,
        contingency_moves=branch_plan,
        ally_target=ally_target,
        execution_order=execution_order,
        main_attack_count=1
        + int(is_attack and ACTION_SURGE in cf_requested and cf_left.get(ACTION_SURGE, 0) > 0),
        attack_long_range=attack_long_range,
    )


def _branch_refusal(hero: str, contingency: Contingency, attacks: bool) -> str | None:
    """The refusal of an "if" clause the engine cannot run (EDG-182). None when the branch is a move."""
    if contingency.condition == TARGET_DOWN and not attacks:
        return (
            f"'{contingency.describe()}' needs an attack this turn, to say which foe it means. No roll is "
            "made and no turn is spent."
        )
    if movement_legs(contingency.branch) or square_move(contingency.branch) is not None:
        return None
    if has_attack_words(contingency.branch):
        return (
            f"A second attack needs its own action, and this turn spends one, so the '{contingency.describe()}' "
            f"clause cannot make it. Attack now, or make the move instead. No roll is made and no turn is spent."
        )
    return (
        f"The engine can run only a move as the 'if' part of a turn, and '{contingency.branch}' is not one. "
        "Choose: a move (for example 'move 10 feet away'), or say the action on its own. No roll is made "
        "and no turn is spent."
    )
