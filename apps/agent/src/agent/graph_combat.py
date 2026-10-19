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

from langchain_core.messages import HumanMessage, SystemMessage

from agent.class_features import (
    requested_features,
    resource_uses,
)
from agent.combat_captives import _captive_result, captive_turn_for_state
from agent.combat_confirmation import (
    ALWAYS_KEY,
    PENDING_KEY,
    control_word,
    needs_confirmation,
    pending_of,
    stored_declaration,
    summary_of,
)
from agent.combat_declaration import declare_action
from agent.combat_enemy_turns import EnemyTurns
from agent.combat_finalise import (
    SettlementInput,
    build_pending_narration,
    close_round_effects,
    settle_adjudication,
    settle_round,
)
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
from agent.combat_player_phase import (
    PlayerTurnInput,
    finish_hero_turn,
    fire_readied,
    run_player_turn,
)
from agent.combat_round_record import RoundRecord
from agent.combat_setup import (
    ENCOUNTER_BUILDER,
    EncounterSetup,
    EnemyStat as EnemyStat,
    _creature_source_reference as _creature_source_reference,
    _exact_enemy,
    _inert_npc_names,
    _npc_groups,
    _player_ac,
    classify_disposition as classify_disposition,
)
from agent.combat_targeting import select_target as select_target  # re-exported (tests)
from agent.combat_view import _ask_player, _display, initiative_view, sort_by_initiative
from agent.combatants import (
    has_effect,
)
from agent.conditions import (
    drop_concentration_if_incapacitated,
    stun_ends_before_turn,
)
from agent.death import DEATH_MODE_SAVES, DEATH_MODE_SUBDUED
from agent.encounter_budget import adaptation_sentence, apply_solo_budget, clamp_level, solo_budget
from agent.llm import make_chat_model, message_text
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    declared_line,
    stats_of,
)
from agent.nodes import world_retriever_node
from agent.nodes.narrator import (
    NARRATION_FAILED_MESSAGE,
    deterministic_narration,
    narrator_request,
)
from agent.nodes.rules_adjudicator import adjudicate
from agent.positioning import (
    initial_band,
)
from agent.prompts import wrap_player_action
from agent.provider_trace import traced_call
from agent.resolution import (
    DEATH_SAVES_KEY,
    initiative_bonus,
    second_wind_remaining,
)
from agent.state import AgentState
from agent.supported_rules import (
    parsed_feature_name,
)
from db.vector.creatures import CreatureResolutionError, resolve_creature
from db.vector.references import normalize_reference
from edgar_core.log_safety import error_summary
from edgar_core.safe_errors import provider_error_code
from tools.dice import roll

# Public names other modules and tests import from here. Kept explicit so a future import
# cleanup cannot drop one; each is defined in the combat_* modules above or in this file.
__all__ = [
    "ENCOUNTER_BUILDER",
    "EncounterSetup",
    "EnemyStat",
    "MAX_COMBAT_ROUNDS",
    "PENDING_ROUND_NARRATION_KEY",
    "PendingRoundNarration",
    "SECOND_WIND_AUTO_AT_ZERO_HP",
    "_build_enemy_roster",
    "_creature_source_reference",
    "_exact_enemy",
    "_inert_npc_names",
    "_monster_context",
    "_name_matches",
    "_player_ac",
    "build_combat_subgraph",
    "captive_turn_for_state",
    "classify_disposition",
    "combat_round_node",
    "finalize_round_narration",
    "initiative_view",
    "is_flee",
    "narrator_request",
    "resolve_creature",
    "roll",
    "roll_initiative",
    "select_target",
    "world_retriever_node",
]

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


# --------------------------------------------------------------------------------------------
# d20 mode helpers (EDG-021)
#
# The player's attack goes through `resolve_attack(roll_mode=...)` (EDG-024). Enemy attacks have no
# character sheet, so `_roll_with_mode` rolls their d20 pair with engine dice; `_contest` is the
# maneuver contest against a foe's Dexterity bonus (a tie leaves the attacker without the effect,
# matching `resolution.resolve_contest`). Neither lets model output choose a die or a number.
# --------------------------------------------------------------------------------------------

# Mirrors the ranged test in resolution.resolve_attack, so the mode matches the weapon used.


# --------------------------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------------------------


def _enemy_slot(turns: EnemyTurns, e: dict) -> None:
    """A foe's slot in initiative: its turn, the other creatures' legendary options, then its close."""
    turns.before_initiative_slot(e)
    turns.enemy_turn(e)
    turns.another_creature_turn_ended(e)
    turns.after_initiative_slot(e)


async def _open_encounter(
    state: AgentState,
    retr: dict,
    combat_state: dict,
    named: list[str],
    character: dict,
    hero: str,
    player_hp: int,
    player_hp_max: int,
    log_lines: list[str],
) -> dict | None:
    """First entry: build the roster, roll initiative and open the encounter (EDG-044, EDG-057).

    Returns the reply when the round cannot start (an unresolved foe, or no foe named), else None.
    """
    budget_summary: dict = {}
    try:
        enemies = await _build_enemy_roster(state, named, budget_summary=budget_summary)
    except CreatureResolutionError as exc:
        return {**retr, "error": str(exc), "error_code": "creature_identity_unresolved"}
    if not enemies:
        return _ask_player(state, "Who are you fighting? Name the foe you attack.")
    for e in enemies:  # EDG-044: where each foe starts (its record decides; see agent.positioning)
        e["band"] = initial_band(stats_of(e))
    player_combatant = {
        "name": "player",
        "display_name": hero,
        "is_player": True,
        "hp_current": player_hp,
        "hp_max": player_hp_max,
        "ac": _player_ac(character),
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
    return None


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
        opened = await _open_encounter(
            state, retr, combat_state, named, character, hero, player_hp, player_hp_max, log_lines
        )
        if opened is not None:
            return opened
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
    # EDG-182: a reply to a pending confirmation, or the "always confirm" option. No die is rolled here.
    confirmed = False
    word = control_word(player_input)
    if word in ("always_on", "always_off") and not fled:
        player[ALWAYS_KEY] = word == "always_on"
        state_word = "on" if word == "always_on" else "off"
        return _ask_player(
            state,
            f"Always confirm is {state_word} for {hero}. No roll is made and no turn is spent.",
            combat_state=combat_state,
        )
    if (pending := pending_of(player)) is not None:
        if word == "confirm":
            # The stored declaration, exactly as it was proposed: nothing said after it changes it.
            player_input = str(pending["player_input"])
            restored = pending.get("parsed")
            parsed = ParsedInput.model_validate(restored) if restored is not None else None
            named = _named_targets({**state, "parsed_input": parsed})
            fled = is_flee(player_input)
            confirmed = True
            player.pop(PENDING_KEY, None)
        elif word == "cancel":
            player.pop(PENDING_KEY, None)
            return _ask_player(
                state,
                f"Cancelled: nothing was rolled and no turn was spent. Say what {hero} does instead.",
                combat_state=combat_state,
            )
        else:
            player.pop(PENDING_KEY, None)  # superseded: this reply is a new declaration
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
    # Class feature uses (EDG-145) come from the sheet as well; a sheet without them is full.
    cf_before = resource_uses(character)
    cf_left = dict(cf_before)
    cf_requested = () if fled else requested_features(player_input, parsed_feature_name(parsed))

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
    # EDG-182: a multi-part turn is repeated back before any die; the stored copy is what runs.
    if not confirmed and not fled and needs_confirmation(decl, always=bool(player.get(ALWAYS_KEY))):
        summary = summary_of(hero, decl)
        stored_declaration(
            player, player_input=player_input, parsed=parsed, summary=summary, round_num=round_num
        )
        return _ask_player(state, summary, combat_state=combat_state)

    # EDG-045: the death mode of the campaign and the hero's death record (agent.death). The record
    # lives on the hero's combatant row for the encounter; a new encounter takes it from the sheet.
    death_mode = str(state.get("death_mode") or DEATH_MODE_SUBDUED)
    if death_mode == DEATH_MODE_SAVES and DEATH_SAVES_KEY not in player:
        sheet_stats = (character or {}).get("stats") or {}
        player[DEATH_SAVES_KEY] = sheet_stats.get(DEATH_SAVES_KEY)

    # --- Round record and enemy phases (agent.combat_enemy_turns, EDG-057) ---
    # Typed effects this round's engine applied; the boundary and narrator read them.
    round_effects: list[str] = []
    round_modes: list[str] = []  # d20 mode of every attack rolled this round
    rec = RoundRecord(
        hero=hero,
        character=character,
        player=player,
        enemies=enemies,
        combatants=combatants,
        player_ac=player_ac,
        round_num=round_num,
        combat_state=combat_state,
        hp=player_hp,
        sw_left=sw_before,
        fled=fled,
        log_lines=log_lines,
        dice_log=dice_log,
        round_effects=round_effects,
        round_modes=round_modes,
        second_wind_auto_at_zero_hp=SECOND_WIND_AUTO_AT_ZERO_HP,
        death_mode=death_mode,
    )
    turns = EnemyTurns(rec)
    # EDG-044c: a foe that closes into melee on its own turn may trigger the hero's readied attack.
    turns.on_closed = lambda foe: fire_readied(rec, turns, foe)

    # A predicted source-turn expiry lets a stunned hero keep a flee declaration. Resolve foes ahead
    # first; restore the flee state before the player slot so it succeeds only if the stun ends.
    if flee_after_stun_source:
        rec.fled = False

    # --- Enemies ahead of the player in initiative ---
    for e in before:
        _enemy_slot(turns, e)

    if flee_after_stun_source:
        rec.fled = rec.hp > 0 and not has_effect(player, "stunned")

    # --- The hero's slot (agent.combat_player_phase, EDG-044, EDG-145) ---
    turn = run_player_turn(
        rec,
        PlayerTurnInput(
            decl=decl,
            player_input=player_input,
            parsed=parsed,
            hp_max=player_hp_max,
            cf_requested=cf_requested,
            cf_left=cf_left,
            turns=turns,
        ),
    )

    # --- Adjudicator: describes consequences of the known outcome (flags/scene/items) ---
    if turn.player_acted and not rec.fled:
        t_stage = time.monotonic()
        adj = await adjudicate(
            {**state, "engine_effects": tuple(round_effects)},
            engine_check=turn.check_result,
            in_combat=True,
            extra_outcome=turn.extra_outcome or None,
        )
        stage_ms["adjudicating_ms"] = int((time.monotonic() - t_stage) * 1000)
        if adj.mechanical_summary and turn.check_result is None:
            log_lines.append(adj.mechanical_summary)
    else:
        adj = AdjudicationResult(success=not rec.fled and turn.player_acted, mechanical_summary="")

    # --- The hero's turn ends, then the foes after the hero act (agent.combat_player_phase) ---
    finish_hero_turn(rec, turns)
    for e in after:
        _enemy_slot(turns, e)
    if not rec.lair_done:
        # The encounter has no actor below initiative 20; the lair acts after the final tie.
        turns.lair_boundary()
    close_round_effects(rec)
    end = settle_round(
        rec, declared=turn.declared, player_acted=turn.player_acted, max_rounds=MAX_COMBAT_ROUNDS
    )

    # --- The adjudication the API persists: engine HP + validated flags/scene/items (agent.combat_finalise) ---
    settlement = settle_adjudication(
        rec,
        SettlementInput(
            adjudication=adj,
            state=state,
            parsed=parsed,
            player_input=player_input,
            player_hp_start=player_hp,
            sw_before=sw_before,
            cf_before=cf_before,
            cf_left=cf_left,
            spell_expended=turn.spell_expended,
            inventory_used=turn.inventory_used,
            dealt=turn.dealt,
            later_swing_dice=turn.later_swing_dice,
            outcome=end.outcome,
        ),
    )

    # --- Finalise the mechanics, then narrate the whole round in one call (EDG-079) ---
    # Everything the narrator is given and everything persisted is decided above. The narrator
    # text is the only thing left to produce, so it is the only thing that can be deferred.
    pending = build_pending_narration(
        rec,
        state=state,
        retr=retr,
        settlement=settlement,
        end=end,
        player_action=turn.player_action,
        bonus_record=turn.bonus_record,
        stage_ms=stage_ms,
    )
    text = deterministic_narration(pending.narr_state)
    if text is None and state.get("defer_narration"):
        # The SSE runner streams the one narrator call, then calls finalize_round_narration.
        return {**pending.base_update, PENDING_ROUND_NARRATION_KEY: pending}
    bound = text is None
    t_stage = time.monotonic()
    if text is None:
        llm, prompt = narrator_request(pending.narr_state, pending.profile)
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
