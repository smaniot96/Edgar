"""Combat subgraph: one full round resolved per HTTP turn, in initiative order.

Design (solo-playable, finishable, engine-decided):
  * On the first combat message we *initialise* the encounter: build an enemy roster with
    per-combatant HP/AC/attack stats grounded in the Monster Manual (`retrieve_monster`) +
    adventure context, and roll initiative (d20 + DEX mod for the player, d20 + initiative
    bonus for each enemy). The roster (including the player, with `init`) is persisted in
    `combat_state.combatants` so HP and initiative carry across turns.
  * Every turn resolves ONE ROUND in initiative order: enemies whose initiative precedes the
    player act first, then the player's typed action is ALWAYS resolved this turn (unless the
    player was dropped before acting), then the remaining enemies act. The player's action is
    never consumed by an enemy slot.
  * Player attacks: d20 + attack bonus (ability mod + proficiency, from the sheet) vs the
    target's AC; nat 20 crits (double dice), nat 1 misses. Damage is engine-rolled.
  * Contested maneuvers (EDG-021): a grapple/pin/restrain or shove prone replaces the attack. The
    player's Athletics is contested by the foe's Dexterity; a won contest records a typed effect in
    `combatants[].effects`, which sets advantage/disadvantage on attacks (`attack_roll_mode`) until a
    declared release, the foe's defeat or the end of the encounter.
  * Action economy (EDG-044): one action and one bonus action per player turn, classified by
    `agent.action_economy` from a code-owned table. A second action or bonus action in the same
    turn is refused before any roll, and the round does not advance. Dodge (an action) gives the
    hero `dodging` until its next turn starts; Second Wind and Healing Word are bonus actions, and so
    is the off-hand attack of two-weapon fighting, rolled as a swing after the Attack action's.
  * HP only changes through engine rolls: enemy attacks, Second Wind (fighter; one use, restored
    by a short or long rest, persisted on the character sheet), or a healing consumable that is
    actually in inventory (and is then removed). Class features (EDG-145) add engine-rolled
    damage only: Action Surge's extra swing, Sneak Attack dice and Rage's bonus.
    The adjudicator LLM's `hp_delta` is ignored in combat (no double counting).
  * NPC dispositions are respected: friendly NPCs are never statted as enemies unless the
    player explicitly attacks them by name.
  * Combat ends deterministically: all enemies at 0 HP -> victory; player at 0 HP -> defeat;
    the player flees/surrenders -> fled; `MAX_COMBAT_ROUNDS` exceeded -> fled (stalemate).
  * Campaign completion is decided by `agent.resolution.decide_completion`: only a victory
    over an enemy flagged `is_boss` can accept/queue it; a non-killing swing never completes.

`combat_state` public keys (for the HUD): `round`, `ended`, `outcome`, `combatants`,
`initiative_order` (names), `initiative` ([{name, id, init, is_player, hp, hp_max, ac, alive,
disposition, can_act}], highest first), `current_turn` (display name of who acts next, i.e. the
player, or None when ended), `current_turn_index` (index into `initiative`), `player_ac`,
`second_wind_used` (derived: the fighter has no Second Wind use left) and
`second_wind_uses_remaining` (the sheet resource, authoritative). Disposition semantics live in
`agent.combatants`: `alive` means living (active, unconscious or captured), and only an `active`
combatant above 0 HP can act. A declared nonlethal knockout is `unconscious`, a declared lethal
one `dead`, and an undeclared one `defeated`. An unconscious foe becomes `captured` when the fight is won.

The subgraph is a single node; the multi-round loop is driven by repeated player turns through
the API. `combat_state.id` is preserved across turns by spreading the existing dict so
`services.combat.persist_combat` updates the same row.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.action_economy import (
    ACTION,
    BONUS_ACTION,
    SECOND_WIND,
    economy_record,
)
from agent.class_features import (
    ACTION_SURGE,
    RAGE,
    RAGE_ROUNDS,
    SNEAK_ATTACK,
    rage_damage_bonus,
    rage_resists,
    requested_features,
    resource_uses,
    sneak_attack_roll,
    spend_use,
)
from agent.combat_captives import _captive_result, captive_turn_for_state
from agent.combat_declaration import declare_action
from agent.combat_intent import (
    _name_matches,
    _named_targets,
    is_flee,
)
from agent.combat_narration import (
    PENDING_ROUND_NARRATION_KEY,
    PendingRoundNarration,
    finalize_round_narration,
)
from agent.combat_setup import (
    ENCOUNTER_BUILDER,
    EncounterSetup,
    EnemyStat as EnemyStat,
    _creature_source_reference as _creature_source_reference,
    _enemy_damage_expr,
    _exact_enemy,
    _inert_npc_names,
    _npc_groups,
    _player_ac,
    classify_disposition as classify_disposition,
)
from agent.combat_targeting import select_target as select_target  # re-exported (tests)
from agent.combat_view import _ask_player, _display, initiative_view, sort_by_initiative
from agent.combatants import (
    add_effect,
    apply_defeat,
    apply_player_disposition,
    attack_roll_mode,
    can_act,
    clear_effects,
    declared_lethality,
    disposition_of,
    finish_victory,
    has_effect,
    impaired_checks,
    is_standing,
    release_effect,
    save_auto_fails,
    save_roll_mode,
    stable_id,
)
from agent.conditions import (
    afflict,
    apply_spell_condition,
    concentration_save,
    consume_mark,
    drop_concentration,
    drop_concentration_if_incapacitated,
    end_fear_of_defeated,
    end_of_source_turn,
    end_of_turn_saves,
    expire_effects,
    fear_refusal,
    hero_end_of_turn_saves,
    paralyzed_melee_crit,
    rider_save,
    rider_save_line,
    save_line,
    save_modifier,
    settle_concentration,
    spell_condition_sentence,
    start_concentration,
    stun_ends_before_turn,
)
from agent.encounter_budget import adaptation_sentence, apply_solo_budget, clamp_level, solo_budget
from agent.llm import make_chat_model, message_text
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    AttackRider,
    AttackSave,
    MonsterAttack,
    adjust_monster_damage,
    attack_plan,
    declared_line,
    legendary_plan,
    recharge_turn_start,
    record_damage,
    regenerate,
    spend_recharge,
    stats_of,
    weapon_damage_type,
)
from agent.nodes import world_retriever_node
from agent.nodes.narrator import (
    NARRATION_FAILED_MESSAGE,
    deterministic_narration,
    narrator_request,
)
from agent.nodes.rules_adjudicator import adjudicate
from agent.positioning import (
    MELEE,
    RANGED,
    attacks_from_band,
    band_of,
    initial_band,
    primary_melee_attack,
    start_turn,
)
from agent.prompts import wrap_player_action
from agent.provider_trace import traced_call
from agent.resolution import (
    SECOND_WIND_REFUSAL,
    CheckResult,
    check_spec,
    choose_weapon,
    decide_completion,
    heal_from_consumable,
    initiative_bonus,
    resolve_attack,
    resolve_from_parsed,
    roll_attack_damage,
    second_wind_remaining,
    second_wind_status,
    spend_second_wind,
    wears_heavy_armor,
)
from agent.spells import (
    SpellSpec,
    catalog_spell,
    narrate,
    resolve_monster_spell,
    resolve_spell,
)
from agent.state import AgentState
from agent.supported_rules import (
    actions_for_state,
    parsed_feature_name,
    ruling_lines,
)
from db.vector.creatures import CreatureResolutionError, resolve_creature
from db.vector.references import normalize_reference
from edgar_core.log_safety import error_summary
from edgar_core.safe_errors import provider_error_code
from tools.dice import DiceOutcome, parse_dice, roll

log = logging.getLogger(__name__)

# Hard cap so a runaway encounter cannot loop forever; reaching it ends combat as a stalemate.
MAX_COMBAT_ROUNDS = 12

# Second Wind is a bonus action (2014 PHB) that the player may use in the same turn as an action
# (EDG-044). The rules have no automatic rally at 0 HP, so this code-owned switch stays False;
# enabling it requires a documented game mode (EDG-025).
SECOND_WIND_AUTO_AT_ZERO_HP = False

# Defaults used when the encounter builder LLM call is unavailable or returns nothing usable.
_MONSTER_LOOKUP_TIMEOUT_S = 8.0


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


async def _monster_context(state: AgentState, names: list[str]) -> list[dict]:
    """Full exact records per named foe. An outage or unresolved identity blocks setup."""
    out = []
    for name in dict.fromkeys(names[:6]):
        try:
            record = await asyncio.wait_for(
                asyncio.to_thread(
                    resolve_creature,
                    name,
                    client=state.get("qdrant_client"),
                    rules_edition=state.get("rules_edition", "2014"),
                    adventure_manifests=state.get("adventure_manifests"),
                    current_scene_id=state.get("current_scene_id"),
                    world_flags=state.get("world_flags"),
                ),
                timeout=_MONSTER_LOOKUP_TIMEOUT_S,
            )
        except CreatureResolutionError:
            raise
        except Exception as exc:
            log.info(
                "monster_lookup_failed",
                extra={"monster_name": name, "error": error_summary(exc)},
            )
            raise CreatureResolutionError(
                f"Creature source unavailable for {name!r}. Retry once the source is available."
            ) from exc
        out.append({**record, "encounter_name": record.get("actor_name") or record.get("creature_name") or name})
    return out


async def _build_enemy_roster(
    state: AgentState, targets: list[str], *, budget_summary: dict | None = None
) -> list[dict]:
    """Build only verified identities, then fit the roster to the solo budget (EDG-148).

    `budget_summary`, when given, receives the engine's adaptation record for the combat start.
    """
    adv_ctx = state.get("adventure_context", []) or []
    player_input = state.get("player_input", "")
    budget = solo_budget(clamp_level((state.get("character") or {}).get("level")))
    allies, hostiles = _npc_groups(state)
    inert = _inert_npc_names(state)
    # Allies the player explicitly attacks by name become fair game (player's choice).
    allies = [a for a in allies if not any(_name_matches(a, t) for t in targets)]

    candidate_names = [
        n
        for n in (targets or hostiles)
        if not any(_name_matches(n, a) for a in allies)
        and not any(_name_matches(n, i) for i in inert)
    ]
    if not candidate_names:
        return []
    monster_ctx = await _monster_context(state, candidate_names)
    identities = {normalize_reference(c["encounter_name"]): c for c in monster_ctx}

    ctx_lines = [
        f"Player's action this turn: {wrap_player_action(player_input)}",
        f"Foes the player named: {', '.join(targets) if targets else '(none named — infer the hostile creatures present from the scene)'}",
    ]
    if allies:
        ctx_lines.append(
            f"These are the player's ALLIES — never stat them as enemies: {', '.join(allies)}"
        )
    if hostiles:
        ctx_lines.append(
            f"Known hostile NPCs (enemies only if present in this fight): {', '.join(hostiles)}"
        )
    if adv_ctx:
        ctx_lines.append("Scene context (for flavour only; do not turn allies into enemies):")
        ctx_lines += [f"- {c.get('text', '')[:300]}" for c in adv_ctx[:2]]
    ctx_lines.append(
        f"Solo budget for this level-{budget.level} hero (enforced by the engine): {budget.describe()}."
    )
    ctx_lines.append("Resolved foes and complete source records (the engine applies their stats):")
    ctx_lines += [f"Foe: {c['encounter_name']}\n{c['text']}" for c in monster_ctx]

    enemies: list[dict] = []
    try:
        llm = make_chat_model(profile="play.combat_builder", temperature=0)
        structured = llm.with_structured_output(EncounterSetup, method="function_calling")
        setup = await traced_call(
            lambda: structured.ainvoke(
                [
                    SystemMessage(content=ENCOUNTER_BUILDER),
                    HumanMessage(content="\n".join(ctx_lines)),
                ]
            ),
            kind="combat_setup",
            profile="play.combat_builder",
        )
        for e in setup.enemies[:6]:
            if any(_name_matches(e.name, a) for a in allies):
                log.info("dropped_ally_from_roster", extra={"ally_name": e.name})
                continue
            identity = identities.get(normalize_reference(e.name))
            if identity is None:
                raise CreatureResolutionError(
                    "Encounter builder proposed an unverified creature identity. Review the exact foe names."
                )
            enemies.append(_exact_enemy(identity, is_boss=e.is_boss))
    except CreatureResolutionError:
        raise
    except Exception as e:
        log.warning("encounter_builder_failed", extra={"error": error_summary(e)})
        # Builder unavailable: every resolved foe still fights with its own exact record. A builder
        # that answers with no foe present starts no fight (EDG-126).
        enemies = [_exact_enemy(c) for c in monster_ctx]
    # Unique names so targeting and initiative stay unambiguous.
    counts: dict[str, int] = {}
    for e in enemies:
        key = e["name"].lower()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            e["name"] = f"{e['name']} {counts[key]}"
    # Fit to the solo budget last, so every name above is already unique when notes refer to it.
    enemies, summary = apply_solo_budget(enemies, budget.level)
    if summary["adapted"]:
        log.info("solo_budget_adapted", extra={"level": budget.level, "notes": summary["notes"]})
    if budget_summary is not None:
        budget_summary.update(summary)
    return enemies


def roll_initiative(combatants: list[dict], character: dict | None) -> list[dict]:
    """Roll d20 + DEX mod (player) / d20 + initiative_bonus (enemies); return sorted copy.

    Ties go to the higher bonus, then to the player.
    """
    for c in combatants:
        bonus = initiative_bonus(character) if c.get("is_player") else int(c.get("initiative_bonus", 0) or 0)
        outcome = roll(f"1d20{bonus:+d}")
        c["init_bonus"] = bonus
        c["init"] = outcome.total
        c["init_roll"] = outcome.rolls[0]
    return sort_by_initiative(combatants)


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


# --------------------------------------------------------------------------------------------
# d20 mode helpers (EDG-021)
#
# The player's attack goes through `resolve_attack(roll_mode=...)` (EDG-024). Enemy attacks have no
# character sheet, so `_roll_with_mode` rolls their d20 pair with engine dice; `_contest` is the
# maneuver contest against a foe's Dexterity bonus (a tie leaves the attacker without the effect,
# matching `resolution.resolve_contest`). Neither lets model output choose a die or a number.
# --------------------------------------------------------------------------------------------

# Mirrors the ranged test in resolution.resolve_attack, so the mode matches the weapon used.


def _roll_with_mode(expression: str, mode: str) -> tuple[DiceOutcome, list[int]]:
    """A d20 expression at `mode`: returns the kept roll and both natural d20 values."""
    first = roll(expression)
    if mode == "normal":
        return first, [first.rolls[0]]
    second = roll(expression)
    pick = max if mode == "advantage" else min
    kept = pick(first, second, key=lambda outcome: outcome.rolls[0])
    return kept, [first.rolls[0], second.rolls[0]]


def _mode_log(outcome: DiceOutcome, purpose: str, mode: str, draws: list[int]) -> dict:
    entry = outcome.to_log(purpose)
    if mode == "normal":
        return entry
    return {**entry, "rolls": list(draws), "kept": outcome.rolls[0], "roll_mode": mode}


def _contest(
    attacker_bonus: int, defender_bonus: int, *, attacker_mode: str = "normal"
) -> tuple[bool, DiceOutcome, DiceOutcome, list[int]]:
    """A contested check: the higher total wins, so a tie leaves the attacker without the effect."""
    mine, draws = _roll_with_mode(f"1d20{attacker_bonus:+d}", attacker_mode)
    theirs = roll(f"1d20{defender_bonus:+d}")
    return mine.total > theirs.total, mine, theirs, draws


# --------------------------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------------------------


async def combat_round_node(state: AgentState) -> dict:
    """Initialise the encounter if needed, then resolve exactly one round."""
    combat_state = dict(state.get("combat_state") or {})
    character = state.get("character") or {}
    player_input = state.get("player_input", "")
    parsed = state.get("parsed_input")
    hero = character.get("name", "The hero")
    player_hp = int(character.get("hp_current", combat_state.get("player_hp", 10) or 10))
    player_hp_max = int(character.get("hp_max", player_hp) or player_hp)

    log_lines: list[str] = []
    dice_log: list[dict] = []
    # Stage timings this round actually measures (EDG-031). A stage that does not run is absent.
    stage_ms: dict[str, int] = {}

    # --- Retrieve context (degrades to empty context on outage) ---
    t_stage = time.monotonic()
    retr = await world_retriever_node(state)
    stage_ms["retrieving_ms"] = int((time.monotonic() - t_stage) * 1000)
    if retr.get("error"):
        return retr
    state = {**state, **retr}

    named = _named_targets(state)
    fled = is_flee(player_input)

    # --- A captive named outside a fight: an engine ruling, never a silent no-op (EDG-146) ---
    if not (combat_state.get("combatants") and not combat_state.get("ended")):
        captive = captive_turn_for_state(state)
        if captive is not None:
            return {**retr, **_captive_result(state, captive)}

    # --- Initialise on first entry ---
    if not combat_state.get("combatants"):
        budget_summary: dict = {}
        try:
            enemies = await _build_enemy_roster(state, named, budget_summary=budget_summary)
        except CreatureResolutionError as exc:
            return {**retr, "error": str(exc), "error_code": "creature_identity_unresolved"}
        if not enemies:
            return _ask_player(state, "Who are you fighting? Name the foe you attack.")
        for e in enemies:  # EDG-044: where each foe starts (its record decides; see agent.positioning)
            e["band"] = initial_band(stats_of(e))
        player_ac = _player_ac(character)
        player_combatant = {
            "name": "player",
            "display_name": hero,
            "is_player": True,
            "hp_current": player_hp,
            "hp_max": player_hp_max,
            "ac": player_ac,
            "disposition": "active" if player_hp > 0 else "unconscious",
            "alive": player_hp > 0,
            # Per-encounter facts live on the player combatant: the table has no column for them.
            "solo_budget": budget_summary,
            "surprised": any(enemy.get("hero_surprised") for enemy in enemies),
        }
        ordered = roll_initiative([player_combatant] + enemies, character)
        combat_state.update(
            {
                "combatants": ordered,
                "initiative_order": [c["name"] for c in ordered],
                "round": 1,
                "current_turn_index": 0,
                "ended": False,
                "outcome": None,
                "solo_budget": budget_summary,
            }
        )
        log_lines.append(
            "Combat begins! Initiative: "
            + ", ".join(f"{_display(c)} {c['init']}" for c in ordered)
            + "."
        )
        if adaptation := adaptation_sentence(budget_summary):
            log_lines.append(adaptation)
        # What each exact record declares but the engine does not run is stated, never implied.
        log_lines += [line for e in enemies if (line := declared_line(e))]
    elif any("init" not in c for c in combat_state["combatants"]):
        # Encounter persisted before initiative was tracked: roll it now.
        combat_state["combatants"] = roll_initiative(list(combat_state["combatants"]), character)

    combatants = sort_by_initiative(combat_state["combatants"])
    combat_state["combatants"] = combatants
    combat_state["initiative_order"] = [c["name"] for c in combatants]
    enemies = [c for c in combatants if not c.get("is_player")]
    player = next((c for c in combatants if c.get("is_player")), None)
    if player is None:
        player = {"name": "player", "display_name": hero, "is_player": True, "init": 0, "init_bonus": 0}
        combatants.append(player)
    player_index = combatants.index(player)
    before = [c for c in combatants[:player_index] if not c.get("is_player")]
    after = [c for c in combatants[player_index + 1 :] if not c.get("is_player")]
    round_num = int(combat_state.get("round", 1) or 1)
    # Re-sync player HP from the authoritative character row each turn.
    player["hp_current"] = player_hp
    player["hp_max"] = player_hp_max
    player_ac = int(player.get("ac") or combat_state.get("player_ac") or _player_ac(character))
    player["ac"] = player_ac
    if ended_spell := drop_concentration_if_incapacitated(player, enemies):
        log_lines.append(f"{hero}'s concentration on {ended_spell} ends while incapacitated.")
    stun_will_end_before_turn = has_effect(player, "stunned") and stun_ends_before_turn(
        player, before, round_num
    )
    flee_after_stun_source = bool(stun_will_end_before_turn and fled)
    if (has_effect(player, "stunned") or has_effect(player, "paralyzed")) and not stun_will_end_before_turn:
        # Stunned and paralyzed are incapacitated (PHB Appendix A): nothing declared is acted on.
        player_input, parsed, named, fled = "", ParsedInput(), [], False
    if player.get("surprised"):
        # Reviewed encounter setup: surprise lasts through the hero's first turn.
        player_input, parsed, named, fled = "", ParsedInput(), [], False
    # Second Wind uses come from the sheet, never from the encounter: they persist across fights.
    sw_before = second_wind_remaining(character)
    sw_left = sw_before
    # Class feature uses (EDG-145) come from the sheet as well; a sheet without them is full.
    cf_before = resource_uses(character)
    cf_left = dict(cf_before)
    cf_requested = () if fled else requested_features(player_input, parsed_feature_name(parsed))
    sneak_used = False
    # Dice of Action Surge's extra swing. The adjudicator logs only the round's first attack, and the
    # merge below drops entries equal to one already logged, so these are appended unmerged.
    later_swing_dice: list[dict] = []

    # --- Decide the player's action BEFORE any dice (agent.combat_declaration, EDG-044) ---
    decl = declare_action(
        state,
        character=character,
        player_input=player_input,
        parsed=parsed,
        named=named,
        fled=fled,
        hero=hero,
        player=player,
        enemies=enemies,
        before=before,
        combat_state=combat_state,
        round_num=round_num,
        cf_left=cf_left,
        cf_requested=cf_requested,
    )
    if isinstance(decl, dict):
        return decl  # a refused declaration: no roll, no round, no state change
    is_attack, is_spell = decl.is_attack, decl.is_spell
    spell_declared, spell_plan, bonus_spell = decl.spell_declared, decl.spell_plan, decl.bonus_spell
    dodge, dash, disengage = decl.dodge, decl.dash, decl.disengage
    moving, standing_up, wants_second_wind = decl.moving, decl.standing_up, decl.wants_second_wind
    rage_declared, heal_item, missing_item = decl.rage_declared, decl.heal_item, decl.missing_item
    combat_action, declared, target = decl.combat_action, decl.declared, decl.target
    release_target, spell_target, spell_targets = decl.release_target, decl.spell_target, decl.spell_targets
    moves, attack_ranged, off_weapon = decl.moves, decl.attack_ranged, decl.off_weapon
    # Typed effects this round's engine applied; the boundary and narrator read them.
    round_effects: list[str] = []
    round_modes: list[str] = []  # d20 mode of every attack rolled this round

    # --- Shared HP bookkeeping (engine only) ---
    hp = player_hp

    def damage_player(amount: int) -> None:
        nonlocal hp, sw_left
        hp = max(0, hp - amount)
        # EDG-043: a caster who falls to 0 HP loses concentration; other damage needs a CON save.
        if hp <= 0:
            if ended_spell := drop_concentration(player, enemies):
                log_lines.append(f"{hero} falls and loses concentration on {ended_spell}.")
        elif (conc := concentration_save(player, enemies, character, amount)) is not None:
            dice_log.append(conc.outcome.to_log(f"{hero} Constitution save (concentration)"))
            if conc.kept:
                log_lines.append(
                    f"{hero} keeps concentration on {conc.spell} (rolled {conc.total} vs DC {conc.dc})."
                )
            else:
                log_lines.append(
                    f"{hero} loses concentration on {conc.spell} (rolled {conc.total} vs DC {conc.dc})."
                )
        # No automatic rally unless a documented game mode enables it (default off).
        if SECOND_WIND_AUTO_AT_ZERO_HP and hp <= 0 and not fled:
            sw = spend_second_wind(character, remaining=sw_left)
            if sw is not None:
                dice_log.append(sw.dice.to_log("second wind"))
                hp = sw.amount
                sw_left -= 1
                log_lines.append(f"{hero} digs deep and uses Second Wind, recovering {sw.amount} HP to stay in the fight!")

    def monster_concentration_damage(caster: dict, amount: int) -> None:
        """Apply a monster's Constitution concentration save after it takes damage."""
        if amount <= 0 or not caster.get("concentration"):
            return
        if int(caster.get("hp_current") or 0) <= 0:
            if ended := drop_concentration(caster, [player]):
                log_lines.append(f"{_display(caster)} falls and loses concentration on {ended}.")
            return
        stats = stats_of(caster)
        bonus = stats.save_bonus("CON") if stats is not None else 0
        dc = max(10, int(amount) // 2)
        outcome = roll(f"1d20{bonus:+d}" if bonus else "1d20")
        dice_log.append(outcome.to_log(f"{_display(caster)} Constitution save (concentration)"))
        if outcome.total < dc:
            ended = drop_concentration(caster, [player])
            if ended:
                log_lines.append(
                    f"{_display(caster)} loses concentration on {ended} "
                    f"(rolled {outcome.total} vs DC {dc})."
                )
        else:
            concentration = caster.get("concentration") or {}
            log_lines.append(
                f"{_display(caster)} keeps concentration on {concentration.get('spell')} "
                f"(rolled {outcome.total} vs DC {dc})."
            )

    round_actors: list[str] = []
    actors: list[dict] = []
    outcomes: list[dict] = []

    def note_actor(e: dict) -> None:
        round_actors.append(_display(e))
        if not any(a["id"] == stable_id(e) for a in actors):  # one entry per foe (multiattack)
            actors.append({"id": stable_id(e), "name": _display(e)})

    def enemy_attack(
        e: dict,
        *,
        label: str | None,
        bonus: int,
        expr: str,
        damage_type: str | None = None,
        exact: bool = False,
        rider: AttackRider | None = None,
        ranged: bool = False,
        within_five_feet: bool = False,
        reaction: bool = False,
    ) -> None:
        """One engine-rolled attack by an enemy. `exact` attacks keep their record's dice.

        `ranged` and `within_five_feet` (EDG-044) give the attack its reach disadvantage; `reaction`
        marks an opportunity attack, which spends the foe's reaction (see `opportunity_attack`).
        """
        note_actor(e)
        mode = attack_roll_mode(e, player, ranged=ranged, within_five_feet=within_five_feet)
        round_modes.append(mode)
        atk, draws = _roll_with_mode(f"1d20{bonus:+d}", mode)
        consume_mark(player)  # a Guiding Bolt mark grants advantage to the next attack roll only
        if reaction:
            purpose = f"{e['name']} opportunity attack"
        else:
            purpose = f"{e['name']} {label} attack" if label else f"{e['name']} attack"
        dice_log.append(_mode_log(atk, purpose, mode, draws))
        mode_text = "" if mode == "normal" else f" at {mode}"
        subject = f"{e['name']}'s {label}" if label else e["name"]
        if reaction:
            subject = f"{subject} (opportunity attack)"
        natural = atk.rolls[0]
        paralyzed_crit = within_five_feet and has_effect(player, "paralyzed")
        hit = natural == 20 or (natural != 1 and atk.total >= player_ac)
        critical = hit and (natural == 20 or paralyzed_crit)
        if hit:
            if critical:
                p = parse_dice(expr)
                count = p.count * 2 if exact else min(p.count * 2, 8)
                expr = f"{count}d{p.sides}{p.modifier:+d}" if p.modifier else f"{count}d{p.sides}"
            dmg_roll = roll(expr)
            dice_log.append(
                dmg_roll.to_log(f"{e['name']} {label} damage" if label else f"{e['name']} damage")
            )
            dmg = max(1, dmg_roll.total)
            resisted = ""
            if rage_resists(character, damage_type, raging=bool(player.get("raging"))):
                dmg //= 2
                resisted = ", halved by Rage resistance"
            crit = " Critical hit!" if critical else ""
            typed = f" {damage_type}" if damage_type else ""
            log_lines.append(
                f"{subject} hits {hero} for {dmg}{typed} damage{resisted} (rolled {atk.total}{mode_text} vs AC {player_ac}).{crit}"
            )
            outcomes.append(
                {
                    "actor": stable_id(e),
                    "action": "attack",
                    "attack": label,
                    "hit": True,
                    "roll": atk.total,
                    "roll_mode": mode,
                    "target_ac": player_ac,
                    "critical": critical,
                    "damage": dmg,
                    "damage_type": damage_type,
                    **({"reaction": "opportunity_attack"} if reaction else {}),
                }
            )
            damage_player(dmg)
            if rider is not None and hp > 0:
                # EDG-043: the exact record's rider on a hit: the hero's save, engine-rolled.
                rider_roll = rider_save(
                    player,
                    rider,
                    character=character,
                    source=_display(e),
                    source_id=stable_id(e),
                    round_applied=int(combat_state.get("round", 1) or 1),
                )
                dice_log.append(rider_roll.outcome.to_log(f"{hero} {rider.ability} save ({rider.kind})"))
                log_lines.append(rider_save_line(hero, rider_roll, attack=label or e["name"]))
                if rider_roll.failed:
                    round_effects.append(rider.kind)
                    if ended_spell := drop_concentration_if_incapacitated(player, enemies):
                        log_lines.append(
                            f"{hero}'s concentration on {ended_spell} ends while incapacitated."
                        )
        else:
            log_lines.append(f"{subject} misses {hero} (rolled {atk.total}{mode_text} vs AC {player_ac}).")
            outcomes.append(
                {
                    "actor": stable_id(e),
                    "action": "attack",
                    "attack": label,
                    "hit": False,
                    "roll": atk.total,
                    "roll_mode": mode,
                    "target_ac": player_ac,
                    "critical": False,
                    "damage": 0,
                    "damage_type": damage_type,
                    **({"reaction": "opportunity_attack"} if reaction else {}),
                }
            )

    def enemy_save_action(e: dict, attack: MonsterAttack, save: AttackSave) -> None:
        """A breath-weapon style action (EDG-043): the hero's engine-rolled save, then the damage.

        A failed save takes the rolled damage and records the action's condition, if it has one; a
        success takes half of the damage (rounded down) or none, as the record states. A dodging
        hero has advantage on a Dexterity save (PHB, PDF p.193); a stunned hero fails a Strength or
        Dexterity save without a roll (PHB, PDF p.293).
        """
        note_actor(e)
        if save_auto_fails(player, save.ability):
            saved, mode, total = False, "normal", None
            rolled = "automatic failure while stunned"
        else:
            mode = save_roll_mode(player, save.ability)
            bonus = save_modifier(character, save.ability)
            saving, draws = _roll_with_mode(f"1d20{bonus:+d}" if bonus else "1d20", mode)
            purpose = f"{hero} {save.ability} save ({attack.name})"
            dice_log.append(_mode_log(saving, purpose, mode, draws))
            saved, total = saving.total >= save.dc, saving.total
            rolled = f"rolled {total}" + ("" if mode == "normal" else f" at {mode}")
        dmg = 0
        if attack.damage_dice:
            dmg_roll = roll(attack.damage_dice)
            dice_log.append(dmg_roll.to_log(f"{e['name']} {attack.name} damage"))
            dmg = dmg_roll.total
            if saved:
                dmg = dmg // 2 if save.half_on_success else 0
        result = "succeeds on" if saved else "fails"
        effects = []
        if attack.damage_dice:
            effects.append(f"takes {dmg} {attack.damage_type} damage" if dmg else "takes no damage")
        if save.condition:
            effects.append(f"is not {save.condition}" if saved else f"is {save.condition}")
        log_lines.append(
            f"{hero} {result} the {save.ability} save against {e['name']}'s {attack.name} "
            f"({rolled} vs DC {save.dc}) and {' and '.join(effects)}."
        )
        outcomes.append(
            {
                "actor": stable_id(e),
                "action": "save",
                "attack": attack.name,
                "ability": save.ability,
                "dc": save.dc,
                "roll": total,
                "roll_mode": mode,
                "saved": saved,
                "damage": dmg,
                "damage_type": attack.damage_type,
                **({"condition": save.condition} if save.condition and not saved else {}),
            }
        )
        # Damage can trigger the usual concentration save first; a failed save that stuns the hero
        # then ends concentration regardless of whether that damage save succeeded.
        damage_player(dmg)
        if save.condition and not saved and hp > 0:
            if save.condition == "prone":
                add_effect(
                    player,
                    "prone",
                    source=_display(e),
                    round_applied=int(combat_state.get("round", 1) or 1),
                )
            else:
                afflict(
                    player,
                    save.condition,
                    source=_display(e),
                    round_applied=int(combat_state.get("round", 1) or 1),
                    ability=save.ability,
                    dc=save.dc,
                    source_id=stable_id(e),
                )
            round_effects.append(save.condition)
            if ended_spell := drop_concentration_if_incapacitated(player, enemies):
                log_lines.append(f"{hero}'s concentration on {ended_spell} ends while incapacitated.")

    def exact_attack(e: dict, attack: MonsterAttack, band: str) -> None:
        """One attack or save action of an exact record, made from the foe's band."""
        if attack.save is not None:
            enemy_save_action(e, attack, attack.save)
        else:
            enemy_attack(
                e,
                label=attack.name,
                bonus=attack.bonus,
                expr=attack.damage_dice,
                damage_type=attack.damage_type,
                exact=True,
                rider=attack.rider,
                ranged=attack.ranged,
                within_five_feet=band == MELEE,
            )

    def choose_monster_spell(e: dict, action_cost: str) -> tuple[SpellSpec, int] | None:
        """Choose an available exact catalog spell with the requested action cost."""
        stats = stats_of(e)
        casting = None if stats is None else stats.spellcasting
        if casting is None:
            return None
        spent = e.setdefault("monster_spell_slots_expended", {})
        if not isinstance(spent, dict):
            spent = e["monster_spell_slots_expended"] = {}
        candidates: list[tuple[int, int, SpellSpec]] = []
        for level, name in casting.spells:
            spec = catalog_spell(name)
            if spec is None or spec.level != level or spec.action != action_cost:
                continue
            if spec.condition and has_effect(player, spec.condition):
                continue
            if spec.humanoid_only and not player.get("humanoid", True):
                continue
            if spec.kind == "heal" and int(e.get("hp_current") or 0) >= int(e.get("hp_max") or 0):
                continue
            if level:
                available = int(casting.slots.get(str(level), 0) or 0)
                used = int(spent.get(str(level), 0) or 0)
                if used >= available:
                    continue
            # Prefer a new control effect, then the most powerful remaining spell. The fallback
            # attack is used once all recorded leveled slots are spent.
            priority = 2 if spec.kind == "condition" else 1 if spec.level else 0
            candidates.append((priority, level, spec))
        if not candidates:
            return None
        _priority, level, chosen = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        if level:
            spent[str(level)] = int(spent.get(str(level), 0) or 0) + 1
        return chosen, level

    def cast_monster_spell(e: dict, spec: SpellSpec) -> None:
        """Resolve a catalog spell using the exact record-owned numbers."""
        stats = stats_of(e)
        casting = None if stats is None else stats.spellcasting
        if casting is None:
            return
        note_actor(e)
        source = spec.name
        source_id = f"{stable_id(e)}:{spec.name.casefold()}"
        if spec.concentration:
            if previous := drop_concentration(e, [player]):
                log_lines.append(f"{_display(e)}'s concentration on {previous} ends.")

        save_bonus = save_modifier(character, spec.save_ability or "DEX")
        save_mode = save_roll_mode(player, spec.save_ability or "DEX")
        if spec.save_ability == "DEX" and has_effect(player, "restrained"):
            save_mode = "normal" if save_mode == "advantage" else "disadvantage"
        attack_mode = attack_roll_mode(
            e,
            player,
            ranged=True,
            within_five_feet=band_of(e) == MELEE,
        )
        result = resolve_monster_spell(
            spec,
            attack_bonus=casting.attack_bonus,
            save_dc=casting.save_dc,
            caster_level=casting.caster_level,
            target_ac=player_ac,
            target_save_bonus=save_bonus,
            healing_modifier=stats.abilities.get(casting.ability, 0),
            save_mode=save_mode,
            roll_mode=attack_mode,
            automatic_save_failure=(
                spec.save_ability is not None and save_auto_fails(player, spec.save_ability)
            ),
            critical_on_hit=band_of(e) == MELEE and has_effect(player, "paralyzed"),
        )
        if spec.kind == "attack":
            consume_mark(player)  # the spell attack roll spends a mark against its target
        dice_log.extend(result.dice_log)
        if result.healing:
            before_hp = int(e.get("hp_current") or 0)
            e["hp_current"] = min(int(e.get("hp_max") or before_hp), before_hp + result.healing)
            log_lines.append(
                f"{_display(e)} casts {spec.name} and regains {e['hp_current'] - before_hp} HP."
            )
        if result.damage:
            damage = result.damage
            if rage_resists(character, spec.damage_type, raging=bool(player.get("raging"))):
                damage //= 2
                log_lines.append(f"Rage resistance halves the {spec.damage_type} damage.")
            damage_player(damage)
        if result.condition and result.save_success is False and hp > 0:
            applied = apply_spell_condition(
                player,
                result.condition,
                source=source,
                round_applied=round_num,
                duration_rounds=spec.duration_rounds,
                repeat_save=(
                    {"ability": spec.save_ability, "dc": casting.save_dc}
                    if spec.save_ability
                    else None
                ),
                source_id=source_id,
            )
            if applied:
                round_effects.append(result.condition)
                if spec.concentration:
                    start_concentration(
                        e,
                        source,
                        round_applied=round_num,
                        duration_rounds=spec.duration_rounds,
                        source_id=source_id,
                    )
                if ended_spell := drop_concentration_if_incapacitated(player, enemies):
                    log_lines.append(f"{hero}'s concentration on {ended_spell} ends while incapacitated.")
            log_lines.append(
                f"{_display(e)} casts {spec.name}. "
                + spell_condition_sentence(result.condition, hero, applied=applied)
            )
        elif result.attack is not None:
            if spec.kind == "attack":
                round_modes.append(attack_mode)
                if result.attack.success:
                    log_lines.append(
                        f"{_display(e)} casts {spec.name}: the spell attack hits {hero} for "
                        f"{result.damage} {spec.damage_type} damage."
                    )
                    if result.condition and hp > 0:
                        applied = apply_spell_condition(
                            player,
                            result.condition,
                            source=source,
                            round_applied=round_num,
                            duration_rounds=spec.duration_rounds,
                            source_id=source_id,
                        )
                        if applied:
                            round_effects.append(result.condition)
                        log_lines.append(spell_condition_sentence(result.condition, hero, applied=applied))
                else:
                    log_lines.append(f"{_display(e)} casts {spec.name}, but the spell attack misses {hero}.")
            elif result.damage:
                log_lines.append(f"{_display(e)} casts {spec.name} at {hero} for {result.damage} damage.")
        elif spec.kind in {"save", "condition"}:
            outcome = "succeeds" if result.save_success else "fails"
            rolled = (
                "automatically fails while incapacitated"
                if save_auto_fails(player, spec.save_ability or "")
                else f"rolls against DC {casting.save_dc}"
            )
            log_lines.append(
                f"{_display(e)} casts {spec.name}; {hero} {outcome} the {spec.save_ability} save "
                f"({rolled})."
            )
            if result.damage:
                log_lines.append(f"{hero} takes {result.damage} {spec.damage_type} damage.")
        elif result.damage:
            log_lines.append(
                f"{_display(e)} casts {spec.name} at {hero} for {result.damage} "
                f"{spec.damage_type or 'damage'}."
            )
        else:
            log_lines.append(f"{_display(e)} casts {spec.name}.")

    def monster_spell_action(e: dict) -> bool:
        """Use an action spell, with a legal bonus spell where the catalog allows both."""
        selected = choose_monster_spell(e, ACTION)
        if selected is not None:
            action_spell, _level = selected
            cast_monster_spell(e, action_spell)
            # A bonus-action spell permits another spell only when it is a cantrip.
            if action_spell.level == 0:
                if bonus := choose_monster_spell(e, BONUS_ACTION):
                    cast_monster_spell(e, bonus[0])
            return True
        if bonus := choose_monster_spell(e, BONUS_ACTION):
            cast_monster_spell(e, bonus[0])
        # Bonus actions leave the ordinary action available for the weapon/Multiattack plan.
        return False

    def foe_attacks(e: dict) -> None:
        stats = stats_of(e)
        if stats is None:
            # Legacy or generic combatant: its stored numbers, one attack (EDG-148 clamps apply).
            # Its one attack is a melee attack, so at range it closes in first (EDG-044).
            if band_of(e) == RANGED:
                e["band"] = MELEE
                log_lines.append(f"{e['name']} closes to melee range.")
            enemy_attack(
                e,
                label=None,
                bonus=int(e.get("attack_bonus", 3)),
                expr=_enemy_damage_expr(e.get("damage_dice", "1d6+1")),
            )
            return
        if monster_spell_action(e):
            return
        # Exact record: make each listed attack in order after start-of-turn features resolve.
        plan = attack_plan(stats, spent=e.get("recharge_spent") or ())
        band = band_of(e)
        if band == RANGED and not any(a.ranged for a in plan):
            # A melee-only foe at range spends its movement to close in, then attacks (EDG-044).
            e["band"] = band = MELEE
            log_lines.append(f"{e['name']} closes to melee range.")
        plan = attacks_from_band(plan, band)
        if not plan:
            log_lines.append(f"{e['name']} has no supported attack and takes no action.")
        for attack in plan:
            if hp <= 0:
                break
            exact_attack(e, attack, band)
            spend_recharge(e, attack)  # a recharge attack waits for its d6 (no-op for the others)

    def opportunity_attack(e: dict) -> bool:
        """The foe's reaction: one melee attack against the hero as it leaves reach (PHB p.195).

        Uses the record's first melee attack, or the stored numbers of a legacy foe. Returns False
        when the foe has no melee attack to make.
        """
        if e.get("surprised"):
            return False
        stats = stats_of(e)
        if stats is None:
            enemy_attack(
                e,
                label=None,
                bonus=int(e.get("attack_bonus", 3)),
                expr=_enemy_damage_expr(e.get("damage_dice", "1d6+1")),
                reaction=True,
            )
        else:
            attack = primary_melee_attack(stats)
            if attack is None:
                return False
            enemy_attack(
                e,
                label=attack.name,
                bonus=attack.bonus,
                expr=attack.damage_dice,
                damage_type=attack.damage_type,
                exact=True,
                reaction=True,
            )
        e["reaction_used"] = True
        if "opportunity_attack" not in round_effects:
            round_effects.append("opportunity_attack")
        return True

    def legendary_action(e: dict) -> None:
        """Use at most one option at the end of this other creature's turn (MM, PDF p.12)."""
        stats = stats_of(e)
        if hp <= 0 or stats is None or not stats.legendary or not can_act(e) or e.get("surprised"):
            return
        band = band_of(e)
        left = stats.legendary_actions - int(e.get("legendary_spent") or 0)
        option = next(
            (o for o in legendary_plan(stats, left) if attacks_from_band([o.attack], band)), None
        )
        if option is None:
            return
        e["legendary_spent"] = int(e.get("legendary_spent") or 0) + option.cost
        log_lines.append(
            f"{e['name']} uses a legendary action: {option.name} (costs {option.cost})."
        )
        exact_attack(e, option.attack, band)
        if option.flight_after and is_standing(e):
            e["band"] = RANGED
            log_lines.append(f"{e['name']} flies away after its Wing Attack.")

    def lair_action(e: dict) -> None:
        """Resolve one reviewed lair option at initiative 20, with ties lost."""
        stats = stats_of(e)
        if fled or hp <= 0 or stats is None or not stats.lair_actions or not can_act(e) or e.get("surprised"):
            return
        previous = e.get("lair_last_action") if int(e.get("lair_last_round") or 0) == round_num - 1 else None
        option = next((a for a in stats.lair_actions if a.name != previous), None)
        if option is None:
            return
        e["lair_last_action"] = option.name
        e["lair_last_round"] = round_num
        note_actor(e)
        if save_auto_fails(player, option.ability):
            saved, total, mode = False, None, "automatic failure while incapacitated"
        else:
            mode = save_roll_mode(player, option.ability)
            bonus = save_modifier(character, option.ability)
            saving, draws = _roll_with_mode(f"1d20{bonus:+d}" if bonus else "1d20", mode)
            dice_log.append(_mode_log(saving, f"{hero} {option.ability} save ({option.name})", mode, draws))
            total = saving.total
            saved = total >= option.dc
        dealt = 0
        if option.damage_dice:
            damage = roll(option.damage_dice)
            dice_log.append(damage.to_log(f"{e['name']} {option.name} damage"))
            dealt = damage.total if not saved else damage.total // 2 if option.half_on_success else 0
        log_lines.append(
            f"At initiative 20, {e['name']} uses its lair action: {option.name}. "
            f"{hero} {'succeeds' if saved else 'fails'} the {option.ability} save"
            f"{'' if total is None else f' (rolled {total} vs DC {option.dc})'}."
        )
        if option.condition == "prone" and not saved and hp > 0:
            add_effect(player, "prone", source=option.name, round_applied=round_num)
            round_effects.append("prone")
            log_lines.append(f"{hero} is knocked prone.")
        if dealt:
            log_lines.append(f"{hero} takes {dealt} {option.damage_type} damage from {option.name}.")
            damage_player(dealt)

    def another_creature_turn_ended(actor: dict) -> None:
        """Give every other able foe one legendary-action opportunity at this turn boundary."""
        if fled or hp <= 0 or not is_standing(actor):
            return
        for e in enemies:
            if e is not actor:
                legendary_action(e)

    lair_done = False

    def lair_boundary() -> None:
        nonlocal lair_done
        if lair_done:
            return
        lair_done = True
        for foe in enemies:
            lair_action(foe)

    def before_initiative_slot(actor: dict) -> None:
        if not lair_done and int(actor.get("init", 0) or 0) < 20:
            lair_boundary()

    def after_initiative_slot(actor: dict) -> None:
        if lair_done or int(actor.get("init", 0) or 0) < 20:
            return
        index = combatants.index(actor)
        if not any(int(next_actor.get("init", 0) or 0) >= 20 for next_actor in combatants[index + 1 :]):
            lair_boundary()

    def enemy_turn(e: dict) -> None:
        start_turn(e)  # its reaction comes back at the start of its turn (PHB p.190)
        e.pop("legendary_spent", None)  # and its spent legendary actions (MM, PDF p.12)
        if ended_spell := drop_concentration_if_incapacitated(e, [player]):
            log_lines.append(f"{_display(e)}'s concentration on {ended_spell} ends while incapacitated.")
        # A foe that is down takes no turn. A paralyzed foe is still in the fight: it has its turn
        # (its recharge and its repeat save) but makes no attack.
        if not is_standing(e) or hp <= 0 or fled:
            return
        for name, outcome, recharged in recharge_turn_start(e):
            dice_log.append(outcome.to_log(f"{e['name']} {name} recharge"))
            state_word = "recharges" if recharged else "does not recharge yet"
            log_lines.append(f"{e['name']}'s {name} {state_word} (rolled {outcome.total} on the d6).")
        # Regeneration is a start-of-turn trait; it still applies if the creature cannot act.
        if stats_of(e) is not None:
            _healed, regen_note = regenerate(e)
            if regen_note:
                log_lines.append(regen_note)
        if can_act(e) and not e.get("surprised"):
            foe_attacks(e)
        # EDG-043: a save-ending spell condition is repeated at the end of the foe's own turn.
        for save in end_of_turn_saves(e):
            dice_log.append(save.outcome.to_log(f"{save.name} {save.ability} save ({save.kind})"))
            log_lines.append(save_line(save))
        # EDG-043: a hero condition that lasts until the end of this foe's next turn ends with it.
        for kind in end_of_source_turn(player, e, int(combat_state.get("round", 1) or 1)):
            log_lines.append(f"{hero} is no longer {kind}.")
        e["surprised"] = False

    # A predicted source-turn expiry lets a stunned hero keep a flee declaration. Resolve foes ahead
    # first; restore the flee state before the player slot so it succeeds only if the stun ends.
    if flee_after_stun_source:
        fled = False

    # --- Enemies ahead of the player in initiative ---
    for e in before:
        before_initiative_slot(e)
        enemy_turn(e)
        another_creature_turn_ended(e)
        after_initiative_slot(e)

    if flee_after_stun_source:
        fled = hp > 0 and not has_effect(player, "stunned")

    # --- Player's action ---
    before_initiative_slot(player)
    check_result: CheckResult | None = None
    dealt = 0
    inventory_used: list[str] = []
    # An enemy ahead in initiative may have stunned the hero just now (EDG-043).
    player_acted = (
        hp > 0
        and not has_effect(player, "stunned")
        and not has_effect(player, "paralyzed")
        and not player.get("surprised")
    )
    runtime_fear_blocked = False
    extra_outcome = ""
    player_action: dict = {"action": "none", "result": "down"}
    spell_expended: dict[str, int] | None = None
    # EDG-044: a Dodge lasts until the hero's next turn starts, which is here (after the foes that
    # act ahead of the hero in initiative). A Dodge declared this turn applies to the foes after.
    release_effect(player, "dodging")
    if player_acted and standing_up and release_effect(player, "prone"):
        log_lines.append(f"{hero} stands up and is no longer prone.")
    # A rage lasts 1 minute (PHB, PDF p.49), counted from the round it began in.
    if player.get("raging") and (
        int(combat_state.get("round", 1) or 1) - int(player.get("rage_round") or 1) >= RAGE_ROUNDS
    ):
        player.pop("raging")
        log_lines.append(f"{hero}'s rage ends after 1 minute.")
    # Bonus action first (Second Wind, Healing Word): it sits beside the turn's action, not in place of it.
    bonus_text = ""
    bonus_record: dict | None = None
    if player_acted and wants_second_wind:
        sw = spend_second_wind(character, remaining=sw_left)
        if sw is None:
            # Refused without a roll: no uses left until a short or long rest. No bonus action is spent.
            bonus_text = SECOND_WIND_REFUSAL.format(hero=hero)
            bonus_record = {"action": "second_wind", "result": "already_used"}
            declared = [d for d in declared if d is not SECOND_WIND]
        else:
            dice_log.append(sw.dice.to_log("second wind"))
            hp = min(player_hp_max, hp + sw.amount)
            sw_left -= 1
            bonus_text = f"{hero} uses Second Wind and regains {sw.amount} HP."
            bonus_record = {"action": "second_wind", "result": "used", "recovered": sw.amount}
            round_effects.append("bonus_action")
        log_lines.append(bonus_text)
    if player_acted and bonus_spell is not None:
        heal_result = resolve_spell(character, bonus_spell)
        dice_log += heal_result.dice_log
        spell_expended = heal_result.expended
        hp = min(player_hp_max, hp + heal_result.healing)
        bonus_text = narrate(heal_result, hero=hero, target=None)
        bonus_record = {
            "action": "catalog_spell",
            "spell": heal_result.spec.name,
            "healing": heal_result.healing,
        }
        round_effects.append("bonus_action")
        log_lines.append(bonus_text)
    # Class features (EDG-145). Rage is a bonus action (EDG-044): it sits beside the turn's attack.
    # Action Surge adds a second swing, and only when the player attacks a foe.
    if player_acted and not fled and RAGE in cf_requested and not player.get("raging"):
        if cf_left.get(RAGE, 0) > 0:
            cf_left[RAGE] = spend_use(character, RAGE, cf_left[RAGE])
            player["raging"] = True
            player["rage_round"] = int(combat_state.get("round", 1) or 1)
            round_effects.append(RAGE)
            log_lines.append(
                f"{hero} flies into a rage!"
                + (" Heavy armor blocks its benefits." if wears_heavy_armor(character) else "")
            )
            bonus_record = bonus_record or {"action": "rage", "result": "started"}
    surge_swing = bool(
        player_acted
        and not fled
        and is_attack
        and target is not None
        and ACTION_SURGE in cf_requested
        and cf_left.get(ACTION_SURGE, 0) > 0
    )
    if surge_swing:
        cf_left[ACTION_SURGE] = spend_use(character, ACTION_SURGE, cf_left[ACTION_SURGE])
        round_effects.append(ACTION_SURGE)
        log_lines.append(f"{hero} uses Action Surge and gets one more attack.")
    # EDG-044: declared moves happen before the action. Leaving a foe's melee band provokes one
    # opportunity attack from it, unless the hero took Disengage this turn (PHB p.192, p.195).
    if player_acted and not fled:
        actual_steps: list[tuple[str, list[dict]]] = []
        for band, foes in moves.steps:
            allowed: list[dict] = []
            for foe in foes:
                refusal_after_enemy_turn = (
                    fear_refusal(hero, player, [(band, [foe])])
                    if band == MELEE and band_of(foe) == RANGED
                    else None
                )
                if refusal_after_enemy_turn:
                    runtime_fear_blocked = True
                    moves.bands[id(foe)] = band_of(foe)
                    log_lines.append(f"{refusal_after_enemy_turn.split('. ')[0]}; {foe['name']} remains beyond 5 feet.")
                    continue
                allowed.append(foe)
                provoked = (
                    band == RANGED
                    and band_of(foe) == MELEE
                    and not disengage
                    and hp > 0
                    and can_act(foe)
                    and not foe.get("surprised")
                    and not foe.get("reaction_used")
                )
                if provoked:
                    opportunity_attack(foe)
                foe["band"] = band
                moves.bands[id(foe)] = band
            if allowed:
                actual_steps.append((band, allowed))
        moves.steps = actual_steps
        if dash:
            round_effects.append("dash")
        if disengage:
            round_effects.append("disengage")
    if not player_acted:
        if hp > 0:
            if player.get("surprised"):
                player_action = {"action": "none", "result": "surprised"}
                round_effects.append("surprised")
                log_lines.append(f"{hero} is surprised and cannot act on this first turn.")
            elif has_effect(player, "paralyzed"):
                player_action = {"action": "none", "result": "paralyzed"}
                round_effects.append("paralyzed")
                log_lines.append(f"{hero} is paralyzed and cannot act.")
            else:
                player_action = {"action": "none", "result": "stunned"}
                round_effects.append("stunned")
                log_lines.append(f"{hero} is stunned and cannot act.")
        else:
            log_lines.append(f"{hero} is down before they can act.")
    elif fled:
        player_action = {"action": "flee", "result": "fled"}
        log_lines.append(f"{hero} disengages and flees the fight.")
    elif missing_item:
        wanted = getattr(parsed, "uses_item", None) or "that item"
        extra_outcome = f"{hero} reaches for {wanted}, but does not have it."
        player_action = {"action": "item", "result": "not_carried"}
        log_lines.append(extra_outcome)
    elif heal_item:
        heal = heal_from_consumable(character, heal_item)
        if heal is not None:
            if heal.dice is not None:
                dice_log.append(heal.dice.to_log(f"healing ({heal_item})"))
            hp = min(player_hp_max, hp + heal.amount)
            inventory_used.append(heal_item)
            extra_outcome = f"{hero} uses {heal_item} and regains {heal.amount} HP."
            player_action = {"action": "item", "item": heal_item, "result": "healed", "healed": heal.amount}
            log_lines.append(extra_outcome)
        else:
            player_action = {"action": "item", "item": heal_item, "result": "no_effect"}
    elif dodge:
        add_effect(player, "dodging", source="dodge", round_applied=int(combat_state.get("round", 1) or 1))
        round_effects.append("dodge")
        extra_outcome = f"{hero} takes the Dodge action: attacks against {hero} have disadvantage until {hero}'s next turn."
        log_lines.append(extra_outcome)
    elif spell_plan is not None and spell_plan.spell.action == ACTION:
        # Catalog spell: the engine rolls it; the slot is spent, and damage (if any) lands on the
        # chosen foe the same way a weapon hit does. An upcast spell reaches its extra foes, each
        # with its own save, from the one slot.
        spec = spell_plan.spell
        if spec.concentration:
            # Casting a concentration spell ends the caster's earlier one (EDG-043).
            if previous := drop_concentration(player, enemies):
                log_lines.append(f"{hero}'s concentration on {previous} ends.")
        applied_condition: str | None = None
        outcomes: list[str] = []
        for spell_target in spell_targets:
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
            round_modes.append(spell_mode)
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
            dice_log += spell_result.dice_log
            spell_expended = spell_result.expended
            target_name = spell_target["name"] if spell_target else None
            extra_outcome = narrate(spell_result, hero=hero, target=target_name)
            if spell_result.healing:
                hp = min(player_hp_max, hp + spell_result.healing)
            if spell_result.damage and spell_target is not None:
                spell_type = spell_plan.spell.damage_type
                dealt, resist_note = adjust_monster_damage(
                    spell_target, spell_result.damage, spell_type, magical=True
                )
                status = _strike(spell_target, dealt, parsed, player_input, damage_type=spell_type)
                monster_concentration_damage(spell_target, dealt)
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
                    round_effects.append(spell_result.condition)
                    applied_condition = spell_result.condition
                    if spec.concentration:
                        start_concentration(
                            player,
                            spec.name,
                            round_applied=int(combat_state.get("round", 1) or 1),
                            duration_rounds=spec.duration_rounds,
                        )
                    if ended_spell := drop_concentration_if_incapacitated(
                        spell_target, [player]
                    ):
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
            outcomes.append(extra_outcome)
        extra_outcome = " ".join(outcomes)
        player_action = {
            "action": "catalog_spell",
            "spell": spell_result.spec.name,
            "target": stable_id(spell_targets[0]) if spell_targets[0] else None,
            "slot_level": spell_result.slot_level,
            "damage": spell_result.damage,
            "healing": spell_result.healing,
            "condition": applied_condition,
        }
        if spell_result.attack is not None:
            check_result = spell_result.attack
            log_lines.append(f"{check_result.describe()}. {extra_outcome}")
        else:
            log_lines.append(extra_outcome)
    elif spell_declared:
        # Narrative ruling only; appended to the round summary below.
        extra_outcome = ""
        player_action = {"action": "declared_action", "result": "narrative_only"}
    elif combat_action is not None and combat_action.kind == "release":
        player_action = {"action": "release", "result": "nothing_bound"}
        if release_target is None:
            extra_outcome = f"{hero} has nothing bound to release."
        else:
            release_effect(release_target, "restrained")
            player_action = {
                "action": "release",
                "target": stable_id(release_target),
                "result": "released",
            }
            extra_outcome = f"{hero} releases {release_target['name']}; it is no longer restrained."
        log_lines.append(extra_outcome)
    elif (
        runtime_fear_blocked
        and target is not None
        and not is_spell
        and not attack_ranged
        and (is_attack or combat_action is not None)
        and band_of(target) == RANGED
    ):
        action_name = (
            f"maneuver_{combat_action.kind}" if combat_action is not None else "attack"
        )
        player_action = {
            "action": action_name,
            "target": stable_id(target),
            "result": "out_of_reach",
        }
        extra_outcome = (
            f"{hero} cannot reach {target['name']} with a melee attack from beyond 5 feet."
        )
        log_lines.append(extra_outcome)
    elif combat_action is not None and target is not None:
        # Contested Athletics against the foe's Dexterity (EDG-021). Engine dice decide it.
        check_mode = "disadvantage" if impaired_checks(player) else "normal"
        won, mine, theirs, mine_draws = _contest(
            check_spec(character, "athletics").modifier,
            int(target.get("initiative_bonus", 0) or 0),
            attacker_mode=check_mode,
        )
        dice_log += [
            _mode_log(mine, "athletics contest", check_mode, mine_draws),
            theirs.to_log(f"{target['name']} contest"),
        ]
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
            extra_outcome = f"{hero} fails to hold {target_name}."
        elif add_effect(
            target,
            kind,
            source=combat_action.source,
            round_applied=int(combat_state.get("round", 1) or 1),
        ):
            round_effects.append(kind)
            if kind == "restrained":
                extra_outcome = f"{hero} restrains {target_name} with {combat_action.source}."
            else:
                extra_outcome = f"{hero} shoves {target_name} prone."
        else:
            extra_outcome = f"{target_name} is already {'restrained' if kind == 'restrained' else 'prone'}."
        player_action = {
            "action": f"maneuver_{kind}",
            "target": stable_id(target),
            "won": bool(won),
            "effect_applied": bool(won and kind in round_effects),
        }
        log_lines.append(f"{contest_line}. {extra_outcome}")
    elif is_attack and target is not None:
        ranged = attack_ranged
        # One swing, or two with Action Surge (EDG-145), then the off-hand attack's bonus action
        # (EDG-044). Each swing rolls its own engine d20.
        any_hit = False
        swings = [False] * (2 if surge_swing else 1) + ([True] if off_weapon else [])
        for swing, off_hand_swing in enumerate(swings):
            if off_hand_swing:
                round_effects.append("bonus_action")
                bonus_record = {"action": "off_hand_attack", "weapon": off_weapon.name}
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
            )
            attack = resolve_attack(
                character,
                int(target.get("ac", 12)),
                spell=is_spell,
                hint=off_weapon.name if off_hand_swing else player_input,
                roll_mode=mode,
            )
            round_modes.append(mode)
            consume_mark(target)  # the attack roll spends a mark it was made against
            if attack.success and has_effect(target, "paralyzed"):
                weapon_name = (attack.weapon or choose_weapon(character, player_input)).name
                if paralyzed_melee_crit(target, weapon_name=weapon_name, ranged=ranged, spell=is_spell):
                    attack.critical = True  # PHB Appendix A: a paralyzed foe's hit from 5 feet is a crit
            if check_result is None:
                check_result = attack
            swing_log = dice_log if swing == 0 else later_swing_dice
            if swing:
                later_swing_dice.extend(attack.dice_log)
            with_text = f" with the off-hand {off_weapon.name}" if off_hand_swing else ""
            if attack.success:
                any_hit = True
                rage_bonus = rage_damage_bonus(character, attack, raging=bool(player.get("raging")))
                swing_dealt, dmg_roll = roll_attack_damage(
                    character, attack, hint=player_input, bonus=rage_bonus, off_hand=off_hand_swing
                )
                swing_log.append(dmg_roll.to_log("player damage"))
                sneak = sneak_attack_roll(character, attack, mode=mode, used=sneak_used)
                notes = ""
                if sneak is not None:
                    swing_log.append(sneak.to_log("sneak attack"))
                    swing_dealt += sneak.total
                    sneak_used = True
                    round_effects.append(SNEAK_ATTACK)
                    notes += f" Sneak Attack adds {sneak.total}."
                if rage_bonus:
                    notes += f" Rage adds {rage_bonus}."
                weapon = attack.weapon or choose_weapon(character, player_input)
                damage_type = weapon_damage_type(weapon.name)
                swing_dealt, resist_note = adjust_monster_damage(
                    target, swing_dealt, damage_type, magical=False
                )
                dealt += swing_dealt
                status = _strike(target, swing_dealt, parsed, player_input, damage_type=damage_type)
                monster_concentration_damage(target, swing_dealt)
                crit = "Critical hit! " if attack.critical else ""
                swing_text = (
                    f"{crit}{hero} hits {target['name']}{with_text} for {swing_dealt} "
                    f"damage{resist_note} {status}{notes}"
                )
            else:
                swing_text = f"{hero} misses {target['name']}{with_text}."
            mode_text = "" if mode == "normal" else f" (at {mode})"
            log_lines.append(f"{attack.describe()}{mode_text}. {swing_text}")
            extra_outcome = " ".join(part for part in (extra_outcome, swing_text) if part)
        player_action = {
            "action": "spell" if is_spell else "attack",
            "target": stable_id(target),
            "hit": any_hit,
            "damage": dealt,
            "target_disposition": disposition_of(target),
        }
    elif (
        dash
        or disengage
        or moving
        or standing_up
        or wants_second_wind
        or bonus_spell is not None
        or rage_declared
    ):
        # A bonus action, a move, Dash or Disengage was declared and is resolved above (or costs no
        # roll): the action is that, not a parsed check.
        if dash or disengage:
            player_action = {"action": "dash" if dash else "disengage", "result": "taken"}
        elif moving:
            player_action = {"action": "move", "result": "moved" if moves.steps else "already_there"}
        elif standing_up:
            player_action = {"action": "stand_up", "result": "stood_up"}
    else:
        check_result, extra = resolve_from_parsed(
            character, parsed, player_input=player_input, impaired=impaired_checks(player)
        )
        dice_log += extra
        player_action = {
            "action": "check",
            "success": check_result.success if check_result is not None else None,
        }
        if check_result is not None:
            log_lines.append(check_result.describe() + ".")

    # --- Adjudicator: describes consequences of the known outcome (flags/scene/items) ---
    extra_outcome = " ".join(part for part in (bonus_text, extra_outcome) if part)
    if player_acted and not fled:
        t_stage = time.monotonic()
        adj = await adjudicate(
            {**state, "engine_effects": tuple(round_effects)},
            engine_check=check_result,
            in_combat=True,
            extra_outcome=extra_outcome or None,
        )
        stage_ms["adjudicating_ms"] = int((time.monotonic() - t_stage) * 1000)
        if adj.mechanical_summary and check_result is None:
            log_lines.append(adj.mechanical_summary)
    else:
        adj = AdjudicationResult(success=not fled and player_acted, mechanical_summary="")

    # EDG-043: the hero's own turn ends: a rider's condition is repeated with the sheet's save.
    if not fled:
        if feared := end_fear_of_defeated(player, enemies):
            log_lines.append(
                f"{feared} is down; {hero}'s fear no longer imposes its attack, check or movement "
                "restrictions, but the condition and its repeat save remain."
            )
        for save in hero_end_of_turn_saves(player, character):
            dice_log.append(save.outcome.to_log(f"{hero} {save.ability} save ({save.kind})"))
            log_lines.append(save_line(save))

    player["surprised"] = False

    # --- Legendary actions: one option per other creature's turn, including the hero's ---
    if not fled:
        another_creature_turn_ended(player)
    after_initiative_slot(player)

    # --- Enemies after the player ---
    for e in after:
        before_initiative_slot(e)
        enemy_turn(e)
        another_creature_turn_ended(e)
        after_initiative_slot(e)
    if not lair_done:
        # The encounter has no actor below initiative 20; the lair acts after the final tie.
        lair_boundary()
    # EDG-043: a timed spell condition ends after its last round (a mark, the caster's next turn).
    for foe_name, expired in expire_effects(combatants, int(combat_state.get("round", 1) or 1)):
        log_lines.append(f"{foe_name} is no longer {expired}.")
    # EDG-043: concentration ends with its minute, or once no foe still carries its effect.
    if ended_spell := settle_concentration(player, enemies, int(combat_state.get("round", 1) or 1)):
        log_lines.append(f"{hero}'s concentration on {ended_spell} ends.")
    for foe in enemies:
        if ended_spell := settle_concentration(foe, [player], int(combat_state.get("round", 1) or 1)):
            log_lines.append(f"{_display(foe)}'s concentration on {ended_spell} ends.")

    player["hp_current"] = hp
    apply_player_disposition(player, subdued=False)
    # Derived mirror for the HUD only; the sheet resource above is the authority.
    second_wind_used = bool(second_wind_status(character)) and sw_left <= 0
    player["second_wind_used"] = second_wind_used
    active_enemies = [e for e in enemies if is_standing(e)]

    # --- End conditions ---
    round_num = int(combat_state.get("round", 1) or 1)
    ended, outcome = False, None
    if fled:
        ended, outcome = True, "fled"
        log_lines.append("You break away from the fight and escape.")
    elif not active_enemies:
        ended, outcome = True, "victory"
        log_lines.append("All enemies are defeated. Victory!")
    elif hp <= 0:
        ended, outcome = True, "defeat"
        log_lines.append(f"{hero} is overwhelmed and falls unconscious, subdued by the enemy.")
    elif round_num >= MAX_COMBAT_ROUNDS:
        ended, outcome = True, "fled"
        log_lines.append("The fight drags on; you disengage to regroup.")

    if outcome == "defeat":
        # Solo campaigns have no human GM to adjudicate death, so a "defeat" means the player
        # is subdued / knocked unconscious (and the story continues — often captured), rather
        # than a permanent dead-end. Leave them at 1 HP.
        hp = 1
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
    spent = declared if player_acted and not fled else []
    player["action_economy"] = economy_record(round_num, spent, dodging=has_effect(player, "dodging"))
    view = initiative_view(combatants)
    player_view_index = next((i for i, c in enumerate(view) if c["is_player"]), 0)
    combat_state.update(
        {
            "combatants": combatants,
            "initiative_order": [c["name"] for c in combatants],
            "initiative": view,
            "economy": player["action_economy"],
            "round": round_num if ended else round_num + 1,
            "ended": ended,
            "outcome": outcome,
            "player_ac": player_ac,
            "second_wind_used": second_wind_used,
            "second_wind_uses_remaining": sw_left,
            "raging": bool(player.get("raging")),
            "current_turn_index": player_view_index,
            "current_turn": None if ended else _display(player),
        }
    )

    # --- The adjudication the API persists: engine HP + validated flags/scene/items ---
    cu = adj.character_update or CharacterUpdate()
    cu.hp_delta = hp - player_hp
    cu.damage_dice = None
    if sw_left != sw_before:
        # Only a spent use is written; a sheet without the key keeps deriving its full uses.
        cu.second_wind_uses_remaining = sw_left
    cf_changed = {f: left for f, left in cf_left.items() if left != cf_before.get(f)}
    if cf_changed:
        # Class feature uses (EDG-145): only the spent features are written, as for Second Wind.
        cu.class_feature_uses = cf_changed
    cu.spell_slots_expended = spell_expended
    for item in inventory_used:
        if item not in cu.inventory_remove:
            cu.inventory_remove.append(item)
    adj.character_update = cu
    adj.damage = dealt or None
    adj.dice_log = [
        *adj.dice_log,
        *[d for d in dice_log if d not in adj.dice_log],
        *later_swing_dice,
    ]
    # Declared unsupported mechanics are ruled on in the round summary (no engine effect).
    summary_text = " ".join(line for line in log_lines if line)
    engine_effects = tuple(round_effects)
    round_roll_mode = (
        "advantage"
        if "advantage" in round_modes
        else "disadvantage"
        if "disadvantage" in round_modes
        else "normal"
    )
    actions = actions_for_state(
        {**state, "engine_effects": engine_effects, "engine_roll_mode": round_roll_mode}
    )
    for ruling in ruling_lines(actions):
        if ruling not in summary_text:
            log_lines.append(ruling)
    adj.mechanical_summary = " ".join(line for line in log_lines if line)

    boss_victory = outcome == "victory" and any(e.get("is_boss") for e in enemies)
    decide_completion(
        adj,
        world_flags=state.get("world_flags"),
        parsed=parsed,
        player_input=player_input,
        combat_active=outcome != "victory",
        boss_victory=boss_victory,
    )

    # --- Finalise the mechanics, then narrate the whole round in one call (EDG-079) ---
    # Everything the narrator is given and everything persisted is decided above. The narrator
    # text is the only thing left to produce, so it is the only thing that can be deferred.
    combat_state["round_actors"] = round_actors
    narr_state = {
        **state,
        "adjudication_result": adj,
        "combat_state": combat_state,
        "engine_effects": engine_effects,
        "engine_roll_mode": round_roll_mode,
    }
    pending = PendingRoundNarration(
        narr_state=narr_state,
        combatants=combatants,
        adjudication=adj,
        outcome=outcome,
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
            "round_num": round_num,
            "ended": ended,
            "outcome": outcome,
            "actors": actors,
            "player_action": {**player_action, **({"bonus_action": bonus_record} if bonus_record else {})},
            "outcomes": outcomes,
            "dispositions": [
                {
                    "id": stable_id(c),
                    "name": _display(c),
                    "is_player": bool(c.get("is_player")),
                    "disposition": disposition_of(c),
                    "hp_current": c.get("hp_current"),
                }
                for c in combatants
            ],
            "dice_log": adj.dice_log,
        },
        stage_ms=dict(stage_ms),
    )
    text = deterministic_narration(narr_state)
    if text is None and state.get("defer_narration"):
        # The SSE runner streams the one narrator call, then calls finalize_round_narration.
        return {**pending.base_update, PENDING_ROUND_NARRATION_KEY: pending}
    bound = text is None
    t_stage = time.monotonic()
    if text is None:
        llm, prompt = narrator_request(narr_state, pending.profile)
        try:
            text = message_text(
                await traced_call(lambda: llm.ainvoke(prompt), kind="narrate", profile=pending.profile)
            )
        except Exception as e:
            return {
                **retr,
                "error": NARRATION_FAILED_MESSAGE,
                "error_code": provider_error_code(e),
            }
    narrating_ms = int((time.monotonic() - t_stage) * 1000)
    return finalize_round_narration(pending, text, bound=bound, narrating_ms=narrating_ms)


def build_combat_subgraph():
    """Compile the single-node combat subgraph (one full round per invocation)."""
    from langgraph.graph import END, START, StateGraph

    combat_graph = StateGraph(AgentState)
    combat_graph.add_node("round", combat_round_node)
    combat_graph.add_edge(START, "round")
    combat_graph.add_edge("round", END)
    return combat_graph.compile()
