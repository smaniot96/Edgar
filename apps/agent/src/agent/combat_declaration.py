"""The player's declared action for one round, decided before any die is rolled (EDG-044, EDG-057).

`declare_action` resolves what the player's words ask for into targets and action-economy slots. A
refusal (two actions of one slot, an unknown or unreachable foe, a refused cast) comes back as the
question the player is asked, with no roll, no round and no state change. Everything it does is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    dash_declared,
    disengage_declared,
    dodge_declared,
    has_attack_words,
    off_hand_declared,
    off_hand_refusal,
    refusal_for,
)
from agent.class_features import RAGE
from agent.combat_intent import _RANGED_HINT, _STAND_UP, _is_attack_action, _name_matches
from agent.combat_targeting import _release_target, additional_spell_targets, select_target
from agent.combat_view import _ask_player
from agent.combatants import CombatManeuver, can_act, classify_combat_maneuver, is_standing
from agent.conditions import condition_ends_before_turn, fear_refusal, humanoid_refusal
from agent.declarations import MANEUVER, MOVE, agreement_question, completeness_question
from agent.positioning import (
    RANGED,
    MovePlan,
    band_of,
    move_refusal,
    movement_steps,
    plan_moves,
    reach_refusal,
)
from agent.readied_action import (
    READY_PHRASE,
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
    off_hand_weapon,
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
) -> Declaration | dict[str, Any]:
    """Resolve the declared action, or return the refusal as the question the player is asked."""
    # --- Decide the player's action BEFORE any dice, so a bad target never burns a round ---
    is_attack, is_spell = (False, False) if fled else _is_attack_action(parsed, named)
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
    bonus_spell = spell_plan if spell_plan is not None and spell_plan.spell.action == BONUS_ACTION else None
    if spell_declared or (spell_plan is not None and bonus_spell is None):
        is_attack, is_spell = False, False
    dodge = not fled and dodge_declared(player_input)
    dash = not fled and dash_declared(player_input)
    disengage = not fled and disengage_declared(player_input)
    # A readied attack's trigger ("when the goblin closes in") is the foe's move, not the hero's.
    moves_text = movement_text(player_input)
    moving = not fled and bool(movement_steps(moves_text))
    standing_up = not fled and bool(_STAND_UP.search(player_input))
    off_hand = not fled and off_hand_declared(player_input)
    # Second Wind is a bonus action (EDG-044): it no longer replaces the attack this turn.
    wants_second_wind = (
        not fled and "second wind" in player_input.lower() and "fighter" in character_class(character)
    )
    rage_declared = (
        not fled
        and RAGE in cf_requested
        and not player.get("raging")
        and cf_left.get(RAGE, 0) > 0
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
    refusal = refusal_for(declared, hero)
    if refusal:
        return _ask_player(state, refusal, combat_state=combat_state)
    # EDG-182: the parser's proposed action list is adopted only where the engine detected the same
    # action. A disagreement is one question, asked before any target, roll or turn spend.
    if not fled:
        detected = {d.kind for d in declared}
        if moving:
            detected.add(MOVE.kind)
        if combat_action is not None:
            detected.add(MANEUVER.kind)
        actions = list(getattr(parsed, "actions", None) or [])
        question = agreement_question([(a.kind, a.text) for a in actions], detected, hero)
        if question is None:
            # EDG-182 completeness: a proposed attack needs a foe when several stand.
            living = [e["name"] for e in enemies if is_standing(e)]
            question = completeness_question(actions, living, player_input, hero)
        if question:
            return _ask_player(state, question, combat_state=combat_state)
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
    moves = plan_moves(
        None if fled else moves_text,
        named,
        [e for e in enemies if can_act(e)],
        pick=lambda n: next(
            (e for e in enemies if can_act(e) and _name_matches(n, e["name"])), None
        ),
    )
    if moves.question:
        return _ask_player(state, moves.question, combat_state=combat_state)
    fear_will_end_before_turn = condition_ends_before_turn(
        player, "frightened", before, round_num
    )
    refused_move = move_refusal(hero, len(moves.steps), dash=dash) or (
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
        ready = order
    # A melee attack or grapple cannot reach a foe beyond 5 feet; a ranged weapon or spell can.
    attack_weapon = choose_weapon(
        character, player_input, prefer_ranged=bool(_RANGED_HINT.search(player_input))
    )
    attack_ranged = bool(_RANGED_HINT.search(player_input)) or attack_weapon.ranged
    if target is not None and not is_spell and not attack_ranged:
        if moves.bands.get(id(target), band_of(target)) == RANGED and (
            is_attack or combat_action is not None
        ):
            return _ask_player(state, reach_refusal(hero, target["name"]), combat_state=combat_state)
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
    )
