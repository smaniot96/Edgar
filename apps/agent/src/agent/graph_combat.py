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
  * HP only changes through engine rolls: enemy attacks, Second Wind (fighter, once per
    encounter), or a healing consumable that is actually in inventory (and is then removed).
    The adjudicator LLM's `hp_delta` is ignored in combat (no double counting).
  * NPC dispositions are respected: friendly NPCs are never statted as enemies unless the
    player explicitly attacks them by name.
  * Combat ends deterministically: all enemies at 0 HP -> victory; player at 0 HP -> defeat;
    the player flees/surrenders -> fled; `MAX_COMBAT_ROUNDS` exceeded -> fled (stalemate).
  * Campaign completion is decided by `agent.resolution.decide_completion`: only a victory
    over an enemy flagged `is_boss` can accept/queue it; a non-killing swing never completes.

`combat_state` public keys (for the HUD): `round`, `ended`, `outcome`, `combatants`,
`initiative_order` (names), `initiative` ([{name, init, is_player, hp, hp_max, ac, alive}],
highest first), `current_turn` (display name of who acts next, i.e. the player, or None when
ended), `current_turn_index` (index into `initiative`), `player_ac`, `second_wind_used`.

The subgraph is a single node; the multi-round loop is driven by repeated player turns through
the API. `combat_state.id` is preserved across turns by spreading the existing dict so
`services.combat.persist_combat` updates the same row.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.llm import make_chat_model
from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.nodes import narrator_node, world_retriever_node
from agent.nodes.rules_adjudicator import adjudicate
from agent.prompts import UNTRUSTED_INPUT_NOTICE, wrap_player_action
from agent.resolution import (
    CheckResult,
    ability_modifier,
    character_class,
    decide_completion,
    find_healing_consumable,
    heal_from_consumable,
    initiative_bonus,
    mentions_item_use,
    normalise_check,
    resolve_attack,
    resolve_from_parsed,
    roll_attack_damage,
)
from agent.state import AgentState
from db.vector import retrieve_monster
from tools.dice import parse_dice, roll

log = logging.getLogger(__name__)

# Hard cap so a runaway encounter cannot loop forever; reaching it ends combat as a stalemate.
MAX_COMBAT_ROUNDS = 12

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
        r"\b(?:i|we)\s+(?:try\s+to\s+|attempt\s+to\s+|decide\s+to\s+)?"
        r"(?:flee|retreat|disengage|surrender|give\s+up|yield)\b",
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
    "- Use the Monster Manual excerpts when given for HP/AC/attack/damage.\n"
    "- Scale for a SOLO low-level hero: keep the total threat modest. A lone boss should be "
    "~18-30 HP; mooks ~5-11 HP. Avoid stacking many strong enemies.\n"
    "- hp_max 5-30, ac 10-16, attack_bonus 2-5, damage_dice in NdM or NdM+K form (e.g. 1d6+1), "
    "initiative_bonus = the creature's DEX modifier (-1..4).\n"
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
    if "plate" in armor:
        base = 18
    elif "splint" in armor:
        base = 17
    elif "chain mail" in armor:
        base = 16
    elif "ring mail" in armor:
        base = 14
    elif "half plate" in armor:
        base = 15 + min(dex_mod, 2)
    elif "breastplate" in armor:
        base = 14 + min(dex_mod, 2)
    elif "chain shirt" in armor or "scale" in armor:
        base = 13 + min(dex_mod, 2) + (1 if "scale" in armor else 0)
    elif "studded" in armor:
        base = 12 + dex_mod
    elif "leather" in armor or "padded" in armor:
        base = 11 + dex_mod
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
    a, b = a.lower().strip(), b.lower().strip()
    return bool(a and b) and (a in b or b in a)


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


async def _monster_context(state: AgentState, names: list[str]) -> list[dict]:
    """Monster Manual chunks per named foe; empty on any failure/timeout."""
    client = state.get("qdrant_client")
    out: list[dict] = []
    for name in names[:3]:
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(retrieve_monster, name, client=client),
                timeout=_MONSTER_LOOKUP_TIMEOUT_S,
            )
        except Exception as e:
            log.info("monster_lookup_failed", extra={"name": name, "error": str(e)[:200]})
            continue
        out += list(hits or [])
    return out


async def _build_enemy_roster(state: AgentState, targets: list[str]) -> list[dict]:
    """One grounded LLM call to stat the enemies; falls back to sensible defaults on failure."""
    rules_ctx = state.get("rules_context", []) or []
    adv_ctx = state.get("adventure_context", []) or []
    player_input = state.get("player_input", "")
    allies, hostiles = _npc_groups(state)
    # Allies the player explicitly attacks by name become fair game (player's choice).
    allies = [a for a in allies if not any(_name_matches(a, t) for t in targets)]

    monster_ctx = await _monster_context(state, targets or hostiles)

    ctx_lines = [
        f"Player's action this turn: {wrap_player_action(player_input)}",
        f"Foes the player named: {', '.join(targets) if targets else '(none named — infer the hostile creatures present from the scene)'}",
    ]
    if allies:
        ctx_lines.append(f"These are the player's ALLIES — never stat them as enemies: {', '.join(allies)}")
    if hostiles:
        ctx_lines.append(f"Known hostile NPCs (enemies only if present in this fight): {', '.join(hostiles)}")
    if adv_ctx:
        ctx_lines.append("Scene context (for flavour only; do not turn allies into enemies):")
        ctx_lines += [f"- {c.get('text', '')[:300]}" for c in adv_ctx[:2]]
    mm = monster_ctx or rules_ctx[:2]
    if mm:
        ctx_lines.append("Monster Manual / rules context:")
        ctx_lines += [f"- {c.get('text', '')[:500]}" for c in mm[:4]]

    enemies: list[dict] = []
    try:
        llm = make_chat_model(temperature=0)
        structured = llm.with_structured_output(EncounterSetup, method="function_calling")
        setup = await structured.ainvoke(
            [
                SystemMessage(content=ENCOUNTER_BUILDER),
                HumanMessage(content="\n".join(ctx_lines)),
            ]
        )
        for e in setup.enemies[:6]:
            if any(_name_matches(e.name, a) for a in allies):
                log.info("dropped_ally_from_roster", extra={"name": e.name})
                continue
            enemies.append(
                {
                    "name": e.name[:60],
                    "hp_max": e.hp_max,
                    "hp_current": e.hp_max,
                    "ac": e.ac,
                    "attack_bonus": e.attack_bonus,
                    "damage_dice": _enemy_damage_expr(e.damage_dice),
                    "initiative_bonus": e.initiative_bonus,
                    "is_boss": bool(e.is_boss),
                    "is_player": False,
                    "alive": True,
                }
            )
    except Exception as e:
        log.warning("encounter_builder_failed", extra={"error": str(e)[:200]})
        enemies = []

    if not enemies:
        # Deterministic fallback: one enemy per named target, else known hostile NPCs.
        names = targets or hostiles[:2]
        seen: dict[str, int] = {}
        for t in names:
            seen[t] = seen.get(t, 0) + 1
            name = t if seen[t] == 1 else f"{t} {seen[t]}"
            enemies.append(
                {
                    "name": name,
                    **_DEFAULT_ENEMY,
                    "hp_current": _DEFAULT_ENEMY["hp_max"],
                    "is_boss": False,
                    "is_player": False,
                    "alive": True,
                }
            )
    # Unique names so targeting and initiative stay unambiguous.
    counts: dict[str, int] = {}
    for e in enemies:
        key = e["name"].lower()
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > 1:
            e["name"] = f"{e['name']} {counts[key]}"
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
            "alive": bool(c.get("alive", True)),
        }
        for c in sort_by_initiative(combatants)
    ]


def select_target(
    enemies_in_order: list[dict], named: list[str]
) -> tuple[dict | None, str | None]:
    """Pick a real living enemy for an attack, or return a question for the player."""
    living = [e for e in enemies_in_order if e.get("alive") and e.get("hp_current", 0) > 0]
    if not living:
        return None, None
    for n in named:
        for e in living:
            if _name_matches(n, e["name"]):
                return e, None
    if not named or len(living) == 1:
        return living[0], None
    foes = ", ".join(e["name"] for e in living)
    return None, f"You don't see {named[0]} among your foes. Who do you attack? ({foes})"


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

    # --- Retrieve context (degrades to empty context on outage) ---
    retr = await world_retriever_node(state)
    if retr.get("error"):
        return {"error": retr["error"]}
    state = {**state, **retr}

    named = _named_targets(state)
    fled = is_flee(player_input)

    # --- Initialise on first entry ---
    if not combat_state.get("combatants"):
        enemies = await _build_enemy_roster(state, named)
        if not enemies:
            return _ask_player(state, "Who are you fighting? Name the foe you attack.")
        player_ac = _player_ac(character)
        player_combatant = {
            "name": "player",
            "display_name": hero,
            "is_player": True,
            "hp_current": player_hp,
            "hp_max": player_hp_max,
            "ac": player_ac,
            "alive": player_hp > 0,
            "second_wind_used": False,
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
            }
        )
        log_lines.append(
            "Combat begins! Initiative: "
            + ", ".join(f"{_display(c)} {c['init']}" for c in ordered)
            + "."
        )
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
    second_wind_used = bool(player.get("second_wind_used") or combat_state.get("second_wind_used"))

    # --- Decide the player's action BEFORE any dice, so a bad target never burns a round ---
    is_attack, is_spell = (False, False) if fled else _is_attack_action(parsed, named)
    heal_item = None
    missing_item = False
    if not fled:
        heal_item = find_healing_consumable(character, parsed=parsed, player_input=player_input)
        if heal_item or mentions_item_use(parsed, player_input):
            # Using an item is the action; if it isn't carried, the action simply fails.
            missing_item = heal_item is None
            is_attack = False
    wants_second_wind = (
        not fled and "second wind" in player_input.lower() and "fighter" in character_class(character)
    )
    if wants_second_wind and not heal_item and not missing_item:
        is_attack = False
    else:
        wants_second_wind = False
    target: dict | None = None
    if is_attack:
        target, question = select_target(enemies, named)
        if question:
            return _ask_player(state, question, combat_state=combat_state)

    # --- Shared HP bookkeeping (engine only) ---
    hp = player_hp

    def damage_player(amount: int) -> None:
        nonlocal hp, second_wind_used
        hp = max(0, hp - amount)
        # Fighter's Second Wind: once per encounter, when a blow would drop them, they rally.
        if hp <= 0 and "fighter" in character_class(character) and not second_wind_used and not fled:
            sw = roll(f"1d10+{int(character.get('level') or 1)}")
            dice_log.append(sw.to_log("second wind"))
            hp = sw.total
            second_wind_used = True
            log_lines.append(f"{hero} digs deep and uses Second Wind, recovering {sw.total} HP to stay in the fight!")

    def enemy_turn(e: dict) -> None:
        if not e.get("alive") or e.get("hp_current", 0) <= 0 or hp <= 0 or fled:
            return
        atk = roll(f"1d20{int(e.get('attack_bonus', 3)):+d}")
        dice_log.append(atk.to_log(f"{e['name']} attack"))
        natural = atk.rolls[0]
        if natural == 20 or (natural != 1 and atk.total >= player_ac):
            expr = _enemy_damage_expr(e.get("damage_dice", "1d6+1"))
            if natural == 20:
                p = parse_dice(expr)
                expr = f"{min(p.count * 2, 8)}d{p.sides}{p.modifier:+d}" if p.modifier else f"{min(p.count * 2, 8)}d{p.sides}"
            dmg_roll = roll(expr)
            dice_log.append(dmg_roll.to_log(f"{e['name']} damage"))
            dmg = max(1, dmg_roll.total)
            crit = " Critical hit!" if natural == 20 else ""
            log_lines.append(f"{e['name']} hits {hero} for {dmg} damage (rolled {atk.total} vs AC {player_ac}).{crit}")
            damage_player(dmg)
        else:
            log_lines.append(f"{e['name']} misses {hero} (rolled {atk.total} vs AC {player_ac}).")

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
    if not player_acted:
        log_lines.append(f"{hero} is down before they can act.")
    elif fled:
        log_lines.append(f"{hero} disengages and flees the fight.")
    elif missing_item:
        wanted = getattr(parsed, "uses_item", None) or "that item"
        extra_outcome = f"{hero} reaches for {wanted}, but does not have it."
        log_lines.append(extra_outcome)
    elif heal_item:
        heal = heal_from_consumable(character, heal_item)
        if heal is not None:
            if heal.dice is not None:
                dice_log.append(heal.dice.to_log(f"healing ({heal_item})"))
            hp = min(player_hp_max, hp + heal.amount)
            inventory_used.append(heal_item)
            extra_outcome = f"{hero} uses {heal_item} and regains {heal.amount} HP."
            log_lines.append(extra_outcome)
    elif wants_second_wind:
        if second_wind_used:
            extra_outcome = f"{hero} has already used Second Wind this encounter."
        else:
            sw = roll(f"1d10+{int(character.get('level') or 1)}")
            dice_log.append(sw.to_log("second wind"))
            hp = min(player_hp_max, hp + sw.total)
            second_wind_used = True
            extra_outcome = f"{hero} uses Second Wind and regains {sw.total} HP."
        log_lines.append(extra_outcome)
    elif is_attack and target is not None:
        check_result = resolve_attack(character, int(target.get("ac", 12)), spell=is_spell, hint=player_input)
        if check_result.success:
            dealt, dmg_roll = roll_attack_damage(character, check_result, hint=player_input)
            dice_log.append(dmg_roll.to_log("player damage"))
            target["hp_current"] = max(0, int(target["hp_current"]) - dealt)
            if target["hp_current"] <= 0:
                target["alive"] = False
            status = "and it falls!" if not target["alive"] else f"({target['hp_current']}/{target['hp_max']} HP left)"
            crit = "Critical hit! " if check_result.critical else ""
            extra_outcome = f"{crit}{hero} hits {target['name']} for {dealt} damage {status}"
        else:
            extra_outcome = f"{hero} misses {target['name']}."
        log_lines.append(f"{check_result.describe()}. {extra_outcome}")
    else:
        check_result, extra = resolve_from_parsed(character, parsed, player_input=player_input)
        dice_log += extra
        if check_result is not None:
            log_lines.append(check_result.describe() + ".")

    # --- Adjudicator: describes consequences of the known outcome (flags/scene/items) ---
    if player_acted and not fled:
        adj = await adjudicate(
            state, engine_check=check_result, in_combat=True, extra_outcome=extra_outcome or None
        )
        if adj.mechanical_summary and check_result is None:
            log_lines.append(adj.mechanical_summary)
    else:
        adj = AdjudicationResult(success=not fled and player_acted, mechanical_summary="")

    # --- Enemies after the player ---
    for e in after:
        enemy_turn(e)

    player["hp_current"] = hp
    player["alive"] = hp > 0
    player["second_wind_used"] = second_wind_used
    living_enemies = [e for e in enemies if e.get("alive") and e.get("hp_current", 0) > 0]

    # --- End conditions ---
    round_num = int(combat_state.get("round", 1) or 1)
    ended, outcome = False, None
    if fled:
        ended, outcome = True, "fled"
        log_lines.append("You break away from the fight and escape.")
    elif not living_enemies:
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

    view = initiative_view(combatants)
    player_view_index = next((i for i, c in enumerate(view) if c["is_player"]), 0)
    combat_state.update(
        {
            "combatants": combatants,
            "initiative_order": [c["name"] for c in combatants],
            "initiative": view,
            "round": round_num if ended else round_num + 1,
            "ended": ended,
            "outcome": outcome,
            "player_ac": player_ac,
            "second_wind_used": second_wind_used,
            "current_turn_index": player_view_index,
            "current_turn": None if ended else _display(player),
        }
    )

    # --- The adjudication the API persists: engine HP + validated flags/scene/items ---
    cu = adj.character_update or CharacterUpdate()
    cu.hp_delta = hp - player_hp
    cu.damage_dice = None
    for item in inventory_used:
        if item not in cu.inventory_remove:
            cu.inventory_remove.append(item)
    adj.character_update = cu
    adj.damage = dealt or None
    adj.dice_log = [*adj.dice_log, *[d for d in dice_log if d not in adj.dice_log]]
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
    narr_state = {**state, "adjudication_result": adj, "combat_state": combat_state}
    narr_updates = await narrator_node(narr_state)
    if narr_updates.get("error"):
        return {"error": narr_updates["error"]}
    narration = narr_updates.get("narration", "")
    if outcome == "victory":
        narration += "\n\n**Victory!** The way forward is clear."
    elif outcome == "defeat":
        narration += "\n\n**Subdued.** You are overwhelmed and knocked unconscious — your story is not over, but you are at the enemy's mercy."
    elif outcome == "fled":
        narration += "\n\n*(You have left the fight.)*"

    return {
        "combat_state": combat_state,
        "messages": list(narr_updates.get("messages", state.get("messages", []))),
        "narration": narration,
        "adjudication_result": adj,
        "rules_context": state.get("rules_context", []),
        "adventure_context": state.get("adventure_context", []),
    }


def _ask_player(state: AgentState, question: str, *, combat_state: dict | None = None) -> dict:
    """No dice rolled, no round consumed: ask the player to clarify their target."""
    adj = AdjudicationResult(success=True, mechanical_summary=question, completion_status="none")
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=question))
    out: dict[str, Any] = {"narration": question, "adjudication_result": adj, "messages": messages}
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
