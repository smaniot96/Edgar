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
    hero `dodging` until its next turn starts; Second Wind and Healing Word are bonus actions.
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
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.action_economy import (
    ACTION,
    BONUS_ACTION,
    DASH,
    DISENGAGE,
    DODGE,
    SECOND_WIND,
    Declared,
    dash_declared,
    disengage_declared,
    dodge_declared,
    economy_record,
    has_attack_words,
    refusal_for,
)
from agent.captives import CaptiveTurn, captive_turn, is_attack_text
from agent.class_features import (
    ACTION_SURGE,
    RAGE,
    SNEAK_ATTACK,
    rage_damage_bonus,
    requested_features,
    resource_uses,
    sneak_attack_roll,
    spend_use,
)
from agent.combat_telemetry import combat_round_event
from agent.combatants import (
    INERT_NPC_DISPOSITIONS,
    add_effect,
    apply_defeat,
    apply_player_disposition,
    attack_roll_mode,
    can_act,
    classify_combat_maneuver,
    clear_effects,
    declared_lethality,
    disposition_of,
    finish_victory,
    has_effect,
    is_standing,
    release_effect,
    roster_violations,
    stable_id,
    view_fields,
)
from agent.conditions import (
    apply_spell_condition,
    concentration_save,
    consume_mark,
    drop_concentration,
    end_of_turn_saves,
    expire_effects,
    hero_end_of_turn_saves,
    humanoid_refusal,
    paralyzed_melee_crit,
    rider_save,
    rider_save_line,
    save_line,
    settle_concentration,
    spell_condition_sentence,
    start_concentration,
)
from agent.encounter_budget import adaptation_sentence, apply_solo_budget, clamp_level, solo_budget
from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.monster_stats import (
    AttackRider,
    MonsterStatsError,
    adjust_monster_damage,
    attack_plan,
    combat_fields,
    declared_line,
    parse_monster_stats,
    recharge_turn_start,
    record_damage,
    regenerate,
    spend_recharge,
    stats_of,
    weapon_damage_type,
)
from agent.nodes import narrator_node, world_retriever_node
from agent.nodes.rules_adjudicator import adjudicate
from agent.positioning import (
    MELEE,
    RANGED,
    attacks_from_band,
    band_of,
    initial_band,
    move_refusal,
    movement_steps,
    plan_moves,
    primary_melee_attack,
    reach_refusal,
    start_turn,
)
from agent.prompts import UNTRUSTED_INPUT_NOTICE, wrap_player_action
from agent.resolution import (
    SECOND_WIND_REFUSAL,
    CheckResult,
    ability_modifier,
    character_class,
    check_spec,
    choose_weapon,
    decide_completion,
    find_healing_consumable,
    heal_from_consumable,
    initiative_bonus,
    mentions_item_use,
    normalise_check,
    resolve_attack,
    resolve_from_parsed,
    roll_attack_damage,
    second_wind_remaining,
    second_wind_status,
    spend_second_wind,
)
from agent.spells import CastPlan, cast_request, narrate, resolve_spell
from agent.state import AgentState
from agent.supported_rules import (
    actions_for_state,
    has_declared_spell,
    parsed_feature_name,
    ruling_lines,
)
from db.vector.creatures import CreatureResolutionError, resolve_creature
from db.vector.references import normalize_reference, reference_matches
from tools.dice import DiceOutcome, parse_dice, roll

log = logging.getLogger(__name__)

# Hard cap so a runaway encounter cannot loop forever; reaching it ends combat as a stalemate.
MAX_COMBAT_ROUNDS = 12

# Second Wind is a bonus action (2014 PHB) that the player may use in the same turn as an action
# (EDG-044). The rules have no automatic rally at 0 HP, so this code-owned switch stays False;
# enabling it requires a documented game mode (EDG-025).
SECOND_WIND_AUTO_AT_ZERO_HP = False

# Defaults used when the encounter builder LLM call is unavailable or returns nothing usable.
_DEFAULT_ENEMY = {"hp_max": 9, "ac": 12, "attack_bonus": 3, "damage_dice": "1d6+1", "initiative_bonus": 1}
_DEFAULT_PLAYER_AC = 12
_MONSTER_LOOKUP_TIMEOUT_S = 8.0

# First-person intent to leave the fight. Requires a flee verb with nothing aimed *at* someone,
# so "I run at the goblin" / "I run toward the door to block it" are attacks/moves, not flight.
_FLEE_NEGATION = re.compile(
    r"\b(?:i|we)\s+(?:won't|will not|don't|do not|never|refuse to|can't|cannot)\s+"
    r"(?:\w+\s+)?(?:flee|run|retreat|escape|surrender|give up)",
    re.IGNORECASE,
)
_FLEE_PATTERNS = (
    re.compile(
        # Disengage (EDG-044) and a retreat to range or away are moves, not leaving the fight.
        r"\b(?:i|we)\s+(?:try\s+to\s+|attempt\s+to\s+|decide\s+to\s+)?"
        r"(?:flee|surrender|give\s+up|yield|retreat(?!\s+(?:to\s+range|from|away|out\s+of\s+reach))"
        r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:try\s+to\s+|attempt\s+to\s+)?escape\b(?!\s+(?:from\s+)?"
        r"(?:the\s+|his\s+|her\s+|its\s+|their\s+)?(?:grapple|grip|hold|net|web|bonds|ropes|chains|shackles))",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+(?:turn\s+and\s+)?run\s+(?:away|off|for\s+(?:it|my\s+life|our\s+lives|the\s+exit))\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:i|we)\s+(?:turn\s+and\s+)?run\s*[.!]*\s*$", re.IGNORECASE),
    re.compile(r"\brun\s+away\b|\bback\s+away\s+and\s+run\b|\bmake\s+a\s+run\s+for\s+it\b", re.IGNORECASE),
)

_GENERIC_TARGETS = {
    "", "enemy", "enemies", "foe", "foes", "them", "it", "him", "her", "monster", "creature",
    "target", "opponent", "the enemy", "nearest enemy", "closest enemy", "someone",
}  # fmt: skip

_FRIENDLY_WORDS = ("friend", "ally", "allied", "helpful", "companion", "loyal", "trust", "grateful", "devoted")
_HOSTILE_WORDS = ("hostile", "enemy", "aggressive", "antagonist", "villain", "murderous", "evil", "attacks on sight")

ENCOUNTER_BUILDER = (
    "You are a D&D 5e encounter builder. Build the enemy roster for THIS fight from the "
    "player's action and the foes they are actually fighting. Rules:\n"
    "- Include ONLY hostile creatures the player is fighting. NEVER include the player's "
    "allies, companions, rescued or friendly NPCs as enemies.\n"
    "- If the player names a specific antagonist, use that EXACT name for that enemy.\n"
    "- Use ONLY the resolved foe names and their complete source records below. Never rename a "
    "creature or add an unlisted identity.\n"
    "- For an exact record the engine takes every number (HP, AC, attacks, traits) from that "
    "record. Your hp_max, ac, attack_bonus, damage_dice and initiative_bonus are ignored, so your "
    "only decision is which of the resolved foes take part.\n"
    "- Scale for a SOLO hero: keep the group modest and stay within the solo budget given in the "
    "context. The engine leaves out any foe beyond the foe-count limit.\n"
    "- is_boss: true ONLY for the adventure's final antagonist / climactic villain named in the "
    "adventure context; false for everyone else.\n\n" + UNTRUSTED_INPUT_NOTICE
)


class EnemyStat(BaseModel):
    name: str
    hp_max: int = Field(ge=1, le=120)
    ac: int = Field(ge=5, le=20, default=12)
    attack_bonus: int = Field(ge=0, le=10, default=3)
    damage_dice: str = "1d6+1"
    initiative_bonus: int = Field(ge=-2, le=5, default=1)
    is_boss: bool = False


class EncounterSetup(BaseModel):
    enemies: list[EnemyStat] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def is_flee(text: str | None) -> bool:
    """True when the player declares leaving the fight (not 'I run at the goblin')."""
    if not text:
        return False
    if _FLEE_NEGATION.search(text):
        return False
    return any(p.search(text) for p in _FLEE_PATTERNS)


def classify_disposition(disposition: Any) -> str:
    d = str(disposition or "").lower()
    if any(w in d for w in _HOSTILE_WORDS) or "unfriendly" in d:
        return "hostile"
    if any(w in d for w in _FRIENDLY_WORDS):
        return "ally"
    return "neutral"


def _enemy_damage_expr(expr: Any) -> str:
    """Validate/clamp an enemy damage expression (max 4 dice, d12, +5)."""
    try:
        p = parse_dice(str(expr))
    except ValueError:
        return _DEFAULT_ENEMY["damage_dice"]
    count, sides, mod = min(p.count, 4), min(p.sides, 12), max(-3, min(p.modifier, 5))
    return f"{count}d{sides}{mod:+d}" if mod else f"{count}d{sides}"


def _player_ac(character: dict | None) -> int:
    """Approximate the player's AC from armor + DEX + shield (good enough for solo play)."""
    if not character:
        return _DEFAULT_PLAYER_AC
    dex_mod = ability_modifier(character, "DEX")
    inv = character.get("inventory") or {}
    armor = str(inv.get("armor", "")).lower() if isinstance(inv, dict) else ""
    weapons = inv.get("weapons") if isinstance(inv, dict) else None
    shield = 2 if isinstance(weapons, list) and any("shield" in str(w).lower() for w in weapons) else 0
    # Match medium armor before the generic "plate" substring.
    if "half plate" in armor:
        base = 15 + min(dex_mod, 2)
    elif "breastplate" in armor:
        base = 14 + min(dex_mod, 2)
    elif "plate" in armor:
        base = 18
    elif "splint" in armor:
        base = 17
    elif "chain mail" in armor:
        base = 16
    elif "ring mail" in armor:
        base = 14
    elif "chain shirt" in armor or "scale" in armor:
        base = 13 + min(dex_mod, 2) + (1 if "scale" in armor else 0)
    elif re.search(r"\bhide\b", armor):
        base = 12 + min(dex_mod, 2)  # PHB Armor table (PDF p.146): medium, DEX capped at +2
    elif "studded" in armor:
        base = 12 + dex_mod
    elif "leather" in armor or "padded" in armor:
        base = 11 + dex_mod
    elif character_class(character) == "barbarian":
        # Unarmored Defense (PHB p.49): no armor, a shield still counts.
        base = 10 + dex_mod + ability_modifier(character, "CON")
    elif character_class(character) == "monk" and not shield:
        # Unarmored Defense (PHB p.79): no armor and no shield.
        base = 10 + dex_mod + ability_modifier(character, "WIS")
    else:
        base = 10 + dex_mod
    return base + shield


def _named_targets(state: AgentState) -> list[str]:
    parsed = state.get("parsed_input")
    names: list[str] = []
    if parsed is not None and getattr(parsed, "target", None):
        names.append(str(parsed.target))
    entities = getattr(parsed, "entities", None) or {}
    raw = entities.get("targets", entities.get("target")) if isinstance(entities, dict) else None
    if isinstance(raw, str):
        names.append(raw)
    elif isinstance(raw, list):
        names += [str(t) for t in raw if t]
    out: list[str] = []
    for n in names:
        n = n.strip()
        if n.lower() not in _GENERIC_TARGETS and n.lower() not in (o.lower() for o in out):
            out.append(n[:60])
    return out


def _name_matches(a: str, b: str) -> bool:
    return reference_matches(a, b)


def _npc_groups(state: AgentState) -> tuple[list[str], list[str]]:
    allies: list[str] = []
    hostiles: list[str] = []
    for n in state.get("npcs") or []:
        if not isinstance(n, dict) or not n.get("name"):
            continue
        kind = classify_disposition(n.get("disposition"))
        if kind == "ally":
            allies.append(n["name"])
        elif kind == "hostile":
            hostiles.append(n["name"])
    return allies, hostiles


def _inert_npc_names(state: AgentState) -> list[str]:
    """Captives and dead foes from earlier fights: they never join a new roster as attackers."""
    return [
        n["name"]
        for n in state.get("npcs") or []
        if isinstance(n, dict)
        and n.get("name")
        and str(n.get("disposition") or "").strip().lower() in INERT_NPC_DISPOSITIONS
    ]


def captive_turn_for_state(state: AgentState) -> CaptiveTurn | None:
    """EDG-146: the engine ruling for a captive the player names outside an active fight."""
    combat = state.get("combat_state") or {}
    if combat.get("combatants") and not combat.get("ended"):
        return None
    captives = [
        c
        for c in state.get("combat_captives") or []
        if isinstance(c, dict) and c.get("name") and c.get("combat_id") is not None
    ]
    if not captives:
        return None
    parsed = state.get("parsed_input")
    norm = normalise_check(getattr(parsed, "check", None)) if parsed is not None else None
    attack_intent = bool(
        (norm is not None and norm[1] in ("attack", "spell_attack"))
        or getattr(parsed, "intent", None) == "combat"
        or is_attack_text(state.get("player_input"))
    )
    return captive_turn(
        state.get("player_input", ""), _named_targets(state), captives, attack_intent=attack_intent
    )


def _captive_result(state: AgentState, turn: CaptiveTurn) -> dict:
    """A deterministic engine ruling: no dice, no round, no model narration."""
    adj = AdjudicationResult(success=True, mechanical_summary=turn.text, completion_status="none")
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=turn.text))
    out: dict[str, Any] = {
        "narration": turn.text,
        "adjudication_result": adj,
        "messages": messages,
        "source_status": state.get("source_status", []),
        "retrieval_degraded": bool(state.get("retrieval_degraded")),
    }
    if turn.outcome:
        out["captive_outcome"] = turn.outcome
    return out


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
            log.info("monster_lookup_failed", extra={"monster_name": name, "error": str(exc)[:200]})
            raise CreatureResolutionError(
                f"Creature source unavailable for {name!r}. Retry once the source is available."
            ) from exc
        out.append({**record, "encounter_name": record.get("actor_name") or record.get("creature_name") or name})
    return out


def _creature_source_reference(record: dict) -> dict:
    """Keep compact private provenance; canonical whole text stays in its source index."""
    fields = (
        "collection",
        "source",
        "page",
        "page_end",
        "source_pages",
        "source_sha256",
        "extraction_sha256",
        "rules_edition",
        "source_version",
        "index_version",
        "creature_name",
        "creature_key",
        "actor_id",
        "actor_collection",
        "actor_manifest_sha256",
        "passage_id",
        "manifest_sha256",
    )
    return {key: record[key] for key in fields if key in record}


def _exact_enemy(identity: dict, *, is_boss: bool = False) -> dict:
    """A roster entry whose numbers come only from its exact record (EDG-004).

    The record's stat block is parsed by code into `monster_stats`; the flat HP/AC/attack fields
    mirror it for older readers. The encounter builder never supplies these numbers.
    """
    name = str(identity["encounter_name"])
    try:
        stats = parse_monster_stats(str(identity.get("text") or ""), name=name)
    except MonsterStatsError as exc:
        raise CreatureResolutionError(
            f"Creature record for {name!r} has no parseable stat block ({exc}). "
            "Review and reingest its source."
        ) from exc
    return {
        "name": name[:60],
        "creature_id": identity["creature_id"],
        "source_reference": _creature_source_reference(identity),
        "stat_adaptation": "exact_record",
        **combat_fields(stats),
        "monster_stats": stats.to_dict(),
        "is_boss": bool(is_boss),
        "is_player": False,
        "disposition": "active",
        "alive": True,
    }


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
        setup = await structured.ainvoke(
            [
                SystemMessage(content=ENCOUNTER_BUILDER),
                HumanMessage(content="\n".join(ctx_lines)),
            ]
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
        log.warning("encounter_builder_failed", extra={"error": str(e)[:200]})
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


def sort_by_initiative(combatants: list[dict]) -> list[dict]:
    return sorted(
        combatants,
        key=lambda c: (int(c.get("init", 0)), int(c.get("init_bonus", 0)), bool(c.get("is_player"))),
        reverse=True,
    )


def _display(c: dict) -> str:
    return c.get("display_name") or c.get("name", "?")


def initiative_view(combatants: list[dict]) -> list[dict]:
    return [
        {
            "name": _display(c),
            "init": c.get("init"),
            "is_player": bool(c.get("is_player")),
            "hp": c.get("hp_current"),
            "hp_max": c.get("hp_max"),
            "ac": c.get("ac"),
            **view_fields(c),
        }
        for c in sort_by_initiative(combatants)
    ]


def select_target(enemies_in_order: list[dict], named: list[str]) -> tuple[dict | None, str | None]:
    """Pick a real living enemy for an attack, or return a question for the player."""
    living = [e for e in enemies_in_order if is_standing(e)]
    if not living:
        return None, None
    for n in named:
        for e in living:
            if _name_matches(n, e["name"]):
                return e, None
    if not named:
        return living[0], None
    foes = ", ".join(e["name"] for e in living)
    return None, f"You don't see {named[0]} among your foes. Who do you attack? ({foes})"


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
def _release_target(enemies_in_order: list[dict], named: list[str]) -> dict | None:
    """The restrained foe a player's declared release refers to (the only one when unnamed)."""
    bound = [e for e in enemies_in_order if has_effect(e, "restrained")]
    for n in named:
        for e in bound:
            if _name_matches(n, e["name"]):
                return e
    return bound[0] if bound and not named else None


# --------------------------------------------------------------------------------------------
# d20 mode helpers (EDG-021)
#
# The player's attack goes through `resolve_attack(roll_mode=...)` (EDG-024). Enemy attacks have no
# character sheet, so `_roll_with_mode` rolls their d20 pair with engine dice; `_contest` is the
# maneuver contest against a foe's Dexterity bonus (a tie leaves the attacker without the effect,
# matching `resolution.resolve_contest`). Neither lets model output choose a die or a number.
# --------------------------------------------------------------------------------------------

# Mirrors the ranged test in resolution.resolve_attack, so the mode matches the weapon used.
_RANGED_HINT = re.compile(r"\b(?:shoot|shoots|fire|fires|loose|looses)\b", re.IGNORECASE)


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


def _contest(attacker_bonus: int, defender_bonus: int) -> tuple[bool, DiceOutcome, DiceOutcome]:
    """A contested check: the higher total wins, so a tie leaves the attacker without the effect."""
    mine = roll(f"1d20{attacker_bonus:+d}")
    theirs = roll(f"1d20{defender_bonus:+d}")
    return mine.total > theirs.total, mine, theirs


def _is_attack_action(parsed: Any, named: list[str]) -> tuple[bool, bool]:
    """(is_attack, is_spell)."""
    norm = normalise_check(getattr(parsed, "check", None)) if parsed is not None else None
    if norm is not None:
        return norm[1] in ("attack", "spell_attack"), norm[1] == "spell_attack"
    if parsed is None:
        return True, False
    return (getattr(parsed, "intent", "combat") == "combat" or bool(named)), False


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
    # Re-sync player HP from the authoritative character row each turn.
    player["hp_current"] = player_hp
    player["hp_max"] = player_hp_max
    player_ac = int(player.get("ac") or combat_state.get("player_ac") or _player_ac(character))
    player["ac"] = player_ac
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
    moving = not fled and bool(movement_steps(player_input))
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
    refusal = refusal_for(declared, hero)
    if refusal:
        return _ask_player(state, refusal, combat_state=combat_state)
    target: dict | None = None
    release_target: dict | None = None
    if combat_action is not None and combat_action.kind == "release":
        release_target = _release_target(enemies, named)
    elif is_attack or combat_action is not None:
        target, question = select_target(enemies, named)
        if question:
            return _ask_player(state, question, combat_state=combat_state)
    spell_target: dict | None = None
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
    # EDG-044: declared moves are free but change band at most once (twice with Dash). Planned on
    # copies only: a refused turn changes no foe and rolls no die.
    moves = plan_moves(
        None if fled else player_input,
        named,
        [e for e in enemies if can_act(e)],
        pick=lambda n: next(
            (e for e in enemies if can_act(e) and _name_matches(n, e["name"])), None
        ),
    )
    if moves.question:
        return _ask_player(state, moves.question, combat_state=combat_state)
    refused_move = move_refusal(hero, len(moves.steps), dash=dash)
    if refused_move:
        return _ask_player(state, refused_move, combat_state=combat_state)
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

    round_actors: list[str] = []
    actors: list[dict] = []
    outcomes: list[dict] = []

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
        round_actors.append(_display(e))
        if not any(a["id"] == stable_id(e) for a in actors):  # one entry per foe (multiattack)
            actors.append({"id": stable_id(e), "name": _display(e)})
        mode = attack_roll_mode(e, player, ranged=ranged, within_five_feet=within_five_feet)
        round_modes.append(mode)
        atk, draws = _roll_with_mode(f"1d20{bonus:+d}", mode)
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
        if natural == 20 or (natural != 1 and atk.total >= player_ac):
            if natural == 20:
                p = parse_dice(expr)
                count = p.count * 2 if exact else min(p.count * 2, 8)
                expr = f"{count}d{p.sides}{p.modifier:+d}" if p.modifier else f"{count}d{p.sides}"
            dmg_roll = roll(expr)
            dice_log.append(
                dmg_roll.to_log(f"{e['name']} {label} damage" if label else f"{e['name']} damage")
            )
            dmg = max(1, dmg_roll.total)
            crit = " Critical hit!" if natural == 20 else ""
            typed = f" {damage_type}" if damage_type else ""
            log_lines.append(
                f"{subject} hits {hero} for {dmg}{typed} damage (rolled {atk.total}{mode_text} vs AC {player_ac}).{crit}"
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
                    "critical": natural == 20,
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
                    source=e["name"],
                    round_applied=int(combat_state.get("round", 1) or 1),
                )
                dice_log.append(rider_roll.outcome.to_log(f"{hero} {rider.ability} save ({rider.kind})"))
                log_lines.append(rider_save_line(hero, rider_roll, attack=label or e["name"]))
                if rider_roll.failed:
                    round_effects.append(rider.kind)
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
        # Exact record: regeneration at the start of its turn, then each listed attack in order.
        _healed, regen_note = regenerate(e)
        if regen_note:
            log_lines.append(regen_note)
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
            enemy_attack(
                e,
                label=attack.name,
                bonus=attack.bonus,
                expr=attack.damage_dice,
                damage_type=attack.damage_type,
                exact=True,
                rider=attack.rider,
                ranged=attack.ranged,
                within_five_feet=band == MELEE and attack.ranged,
            )
            spend_recharge(e, attack)  # a recharge attack waits for its d6 (no-op for the others)

    def opportunity_attack(e: dict) -> bool:
        """The foe's reaction: one melee attack against the hero as it leaves reach (PHB p.195).

        Uses the record's first melee attack, or the stored numbers of a legacy foe. Returns False
        when the foe has no melee attack to make.
        """
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

    def enemy_turn(e: dict) -> None:
        start_turn(e)  # its reaction comes back at the start of its turn (PHB p.190)
        # A foe that is down takes no turn. A paralyzed foe is still in the fight: it has its turn
        # (its recharge and its repeat save) but makes no attack.
        if not is_standing(e) or hp <= 0 or fled:
            return
        for name, outcome, recharged in recharge_turn_start(e):
            dice_log.append(outcome.to_log(f"{e['name']} {name} recharge"))
            state_word = "recharges" if recharged else "does not recharge yet"
            log_lines.append(f"{e['name']}'s {name} {state_word} (rolled {outcome.total} on the d6).")
        if can_act(e):
            foe_attacks(e)
        # EDG-043: a save-ending spell condition is repeated at the end of the foe's own turn.
        for save in end_of_turn_saves(e):
            dice_log.append(save.outcome.to_log(f"{save.name} {save.ability} save ({save.kind})"))
            log_lines.append(save_line(save))

    player_index = combatants.index(player)
    before = [c for c in combatants[:player_index] if not c.get("is_player")]
    after = [c for c in combatants[player_index + 1 :] if not c.get("is_player")]

    # --- Enemies ahead of the player in initiative ---
    for e in before:
        enemy_turn(e)

    # --- Player's action ---
    check_result: CheckResult | None = None
    dealt = 0
    inventory_used: list[str] = []
    player_acted = hp > 0
    extra_outcome = ""
    player_action: dict = {"action": "none", "result": "down"}
    spell_expended: dict[str, int] | None = None
    # EDG-044: a Dodge lasts until the hero's next turn starts, which is here (after the foes that
    # act ahead of the hero in initiative). A Dodge declared this turn applies to the foes after.
    release_effect(player, "dodging")
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
            round_effects.append(RAGE)
            log_lines.append(f"{hero} flies into a rage!")
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
        for band, foes in moves.steps:
            for foe in foes:
                provoked = (
                    band == RANGED
                    and band_of(foe) == MELEE
                    and not disengage
                    and hp > 0
                    and can_act(foe)
                    and not foe.get("reaction_used")
                )
                if provoked:
                    opportunity_attack(foe)
                foe["band"] = band
        if dash:
            round_effects.append("dash")
        if disengage:
            round_effects.append("disengage")
    if not player_acted:
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
        # chosen foe the same way a weapon hit does.
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
            extra_outcome = f"{extra_outcome}{resist_note} {status}"
        if spell_target is not None and spell_result.attack is not None:
            consume_mark(spell_target)  # the attack roll spends a mark it was made against
        applied_condition: str | None = None
        spec = spell_plan.spell
        if spec.concentration:
            # Casting a concentration spell ends the caster's earlier one (EDG-043).
            if previous := drop_concentration(player, enemies):
                log_lines.append(f"{hero}'s concentration on {previous} ends.")
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
            extra_outcome = " ".join(
                [
                    extra_outcome,
                    spell_condition_sentence(
                        spell_result.condition, spell_target["name"], applied=applied
                    ),
                ]
            )
        player_action = {
            "action": "catalog_spell",
            "spell": spell_result.spec.name,
            "target": stable_id(spell_target) if spell_target else None,
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
    elif combat_action is not None and target is not None:
        # Contested Athletics against the foe's Dexterity (EDG-021). Engine dice decide it.
        won, mine, theirs = _contest(
            check_spec(character, "athletics").modifier, int(target.get("initiative_bonus", 0) or 0)
        )
        dice_log += [mine.to_log("athletics contest"), theirs.to_log(f"{target['name']} contest")]
        verdict = "success" if won else "failure"
        contest_line = (
            f"contest: {hero} athletics {mine.expression} = {mine.total} "
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
        # One swing, or two with Action Surge (EDG-145). Each swing rolls its own engine d20.
        any_hit = False
        for swing in range(2 if surge_swing else 1):
            if swing and not is_standing(target):
                # The first swing finished the foe; the surged swing has nothing to hit.
                log_lines.append(f"{hero}'s extra attack finds no foe left standing.")
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
                hint=player_input,
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
            if attack.success:
                any_hit = True
                rage_bonus = rage_damage_bonus(character, attack, raging=bool(player.get("raging")))
                swing_dealt, dmg_roll = roll_attack_damage(
                    character, attack, hint=player_input, bonus=rage_bonus
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
                crit = "Critical hit! " if attack.critical else ""
                swing_text = (
                    f"{crit}{hero} hits {target['name']} for {swing_dealt} damage{resist_note} "
                    f"{status}{notes}"
                )
            else:
                swing_text = f"{hero} misses {target['name']}."
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
    else:
        check_result, extra = resolve_from_parsed(character, parsed, player_input=player_input)
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
        for save in hero_end_of_turn_saves(player, character):
            dice_log.append(save.outcome.to_log(f"{hero} {save.ability} save ({save.kind})"))
            log_lines.append(save_line(save))

    # --- Enemies after the player ---
    for e in after:
        enemy_turn(e)
    # EDG-043: a timed spell condition ends after its last round (a mark, the caster's next turn).
    for foe_name, expired in expire_effects(combatants, int(combat_state.get("round", 1) or 1)):
        log_lines.append(f"{foe_name} is no longer {expired}.")
    # EDG-043: concentration ends with its minute, or once no foe still carries its effect.
    if ended_spell := settle_concentration(player, enemies, int(combat_state.get("round", 1) or 1)):
        log_lines.append(f"{hero}'s concentration on {ended_spell} ends.")

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
        player.pop("concentration", None)  # EDG-043: the caster stops concentrating with the fight
        player.pop("raging", None)  # rage, too, lasts only for this encounter (EDG-145)

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

    # --- Narrate the whole round in one call ---
    combat_state["round_actors"] = round_actors
    narr_state = {
        **state,
        "adjudication_result": adj,
        "combat_state": combat_state,
        "engine_effects": engine_effects,
        "engine_roll_mode": round_roll_mode,
    }
    t_stage = time.monotonic()
    narr_updates = await narrator_node(narr_state)
    stage_ms["narrating_ms"] = int((time.monotonic() - t_stage) * 1000)
    if narr_updates.get("error"):
        return {**retr, "error": narr_updates["error"]}
    narration = narr_updates.get("narration", "")
    problems = roster_violations(narration, combatants)
    if problems:
        # Prose that revives an inactive foe or invents an attacker is replaced by the engine's log.
        log.warning("narration_roster_violation", extra={"problems": problems[:4]})
        narration = adj.mechanical_summary or ""
        messages = narr_updates.get("messages")
        if messages and isinstance(messages[-1], AIMessage):
            messages = [*messages[:-1], AIMessage(content=narration)]
            narr_updates = {**narr_updates, "messages": messages}
    if outcome == "victory":
        narration += "\n\n**Victory!** The way forward is clear."
    elif outcome == "defeat":
        narration += "\n\n**Subdued.** You are overwhelmed and knocked unconscious — your story is not over, but you are at the enemy's mercy."
    elif outcome == "fled":
        narration += "\n\n*(You have left the fight.)*"

    round_event = combat_round_event(
        turn_id=state.get("turn_id"),
        round_num=round_num,
        ended=ended,
        outcome=outcome,
        actors=actors,
        player_action={**player_action, **({"bonus_action": bonus_record} if bonus_record else {})},
        outcomes=outcomes,
        dispositions=[
            {
                "id": stable_id(c),
                "name": _display(c),
                "is_player": bool(c.get("is_player")),
                "disposition": disposition_of(c),
                "hp_current": c.get("hp_current"),
            }
            for c in combatants
        ],
        dice_log=adj.dice_log,
        stage_ms=stage_ms,
    )
    return {
        **retr,
        "combat_state": combat_state,
        "messages": list(narr_updates.get("messages", state.get("messages", []))),
        "narration": narration,
        "adjudication_result": adj,
        "engine_roll_mode": round_roll_mode,
        "rules_context": state.get("rules_context", []),
        "adventure_context": state.get("adventure_context", []),
        # Written in the turn transaction by persist_turn (EDG-031); never committed here.
        "telemetry_events": [round_event],
    }


def _ask_player(state: AgentState, question: str, *, combat_state: dict | None = None) -> dict:
    """No dice rolled, no round consumed: ask the player to clarify their target."""
    adj = AdjudicationResult(success=True, mechanical_summary=question, completion_status="none")
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=question))
    out: dict[str, Any] = {"narration": question, "adjudication_result": adj, "messages": messages}
    out["source_status"] = state.get("source_status", [])
    out["retrieval_degraded"] = bool(state.get("retrieval_degraded"))
    if combat_state:
        combat_state = dict(combat_state)
        combat_state["initiative"] = initiative_view(combat_state.get("combatants") or [])
        out["combat_state"] = combat_state
    return out


def build_combat_subgraph():
    """Compile the single-node combat subgraph (one full round per invocation)."""
    from langgraph.graph import END, START, StateGraph

    combat_graph = StateGraph(AgentState)
    combat_graph.add_node("round", combat_round_node)
    combat_graph.add_edge(START, "round")
    combat_graph.add_edge("round", END)
    return combat_graph.compile()
