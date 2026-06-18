"""Combat subgraph: one full round resolved per HTTP turn.

Design (solo-playable, finishable):
  * On the first combat message we *initialise* the encounter — roll initiative, and build an
    enemy roster with per-combatant HP/AC/attack stats grounded in the Monster Manual +
    adventure RAG context. The roster (including the player) is persisted in
    `combat_state.combatants` so HP carries across turns.
  * Every subsequent turn resolves ONE ROUND: the player's typed action is adjudicated against
    the rules (so it stays grounded and the dice tool decides the numbers), damage is applied to
    the targeted enemy, then every *living* enemy takes a deterministic attack against the
    player. This means the player's input is never silently consumed by an enemy's slot (the
    old single-actor design's central bug).
  * Combat ends deterministically: all enemies at 0 HP -> victory; player at 0 HP -> defeat;
    the player flees/surrenders -> fled; `MAX_COMBAT_ROUNDS` exceeded -> fled (stalemate).
    `combat_state.ended`/`outcome` are authoritative — no fragile substring matching.

The subgraph is a single node; the multi-round loop is driven by repeated player turns through
the API. `combat_state.id` is preserved across turns by spreading the existing dict so
`services.combat.persist_combat` updates the same row.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from tools import roll
from tools.dice import parse_dice

from agent.llm import make_chat_model
from agent.nodes import (
    narrator_node,
    rules_adjudicator_node,
    world_retriever_node,
)
from agent.state import AgentState

# Hard cap so a runaway encounter cannot loop forever; reaching it ends combat as a stalemate.
MAX_COMBAT_ROUNDS = 12

# Defaults used when the encounter builder LLM call is unavailable or returns nothing usable.
_DEFAULT_ENEMY = {"hp_max": 9, "ac": 12, "attack_bonus": 3, "damage_dice": "1d6+1"}
_DEFAULT_PLAYER_AC = 12

# First-person intent to leave the fight. Phrase-based so scene text like "blocks our escape"
# does not trigger a flee.
_FLEE_PATTERNS = (
    "i flee", "we flee", "i run", "we run", "run away", "i retreat", "we retreat",
    "i escape", "we escape", "try to escape", "try to flee", "i disengage", "we disengage",
    "i surrender", "we surrender", "i give up", "we give up", "back away and run",
)

ENCOUNTER_BUILDER = (
    "You are a D&D 5e encounter builder. Build the enemy roster for THIS fight from the "
    "player's action and the foes they are actually fighting (named below). Rules:\n"
    "- Include ONLY hostile enemies the player is fighting. NEVER include the player's own "
    "allies, companions, or rescued NPCs as enemies.\n"
    "- If the player names a specific antagonist, use that EXACT name for that enemy.\n"
    "- Scale for a SOLO level-1 hero: keep the total threat modest. A lone boss should be "
    "~18-30 HP; mooks ~5-11 HP. Avoid stacking many strong enemies.\n"
    "- hp_max 5-30, ac 10-16, attack_bonus 2-5, damage_dice in NdM or NdM+K form (e.g. 1d6+1)."
)


class EnemyStat(BaseModel):
    name: str
    hp_max: int = Field(ge=1, le=120)
    ac: int = Field(ge=5, le=20, default=12)
    attack_bonus: int = Field(ge=0, le=10, default=3)
    damage_dice: str = "1d6+1"


class EncounterSetup(BaseModel):
    enemies: list[EnemyStat] = Field(default_factory=list)


def _safe_roll(expr: str, fallback: str = "1d4") -> int:
    try:
        parse_dice(expr)
    except Exception:
        expr = fallback
    try:
        return roll(expr).total
    except Exception:
        return 1


def _ability_mod(score: Any) -> int:
    try:
        return (int(score) - 10) // 2
    except Exception:
        return 0


_WEAPON_DICE = {
    "dagger": "1d4",
    "club": "1d4",
    "shortsword": "1d6",
    "scimitar": "1d6",
    "rapier": "1d8",
    "longsword": "1d8",
    "warhammer": "1d8",
    "battleaxe": "1d8",
    "greatsword": "2d6",
    "greataxe": "1d12",
    "mace": "1d6",
    "spear": "1d6",
    "handaxe": "1d6",
}


def _player_damage(character: dict | None) -> int:
    """Roll the player's weapon damage (weapon die + STR mod). Defaults to 1d8 if unknown.

    We roll this ourselves rather than trusting the adjudicator's free-form `damage` field,
    which the LLM tends to under-fill (e.g. 1), making fights unwinnable.
    """
    dice = "1d8"
    str_mod = 0
    if character:
        stats = character.get("stats") or {}
        str_mod = _ability_mod(stats.get("STR", 10))
        inv = character.get("inventory") or {}
        weapons = inv.get("weapons") or []
        names = " ".join(str(w).lower() for w in weapons) if isinstance(weapons, list) else str(weapons).lower()
        for wname, wdice in _WEAPON_DICE.items():
            if wname in names:
                dice = wdice
                break
    expr = f"{dice}+{str_mod}" if str_mod >= 0 else f"{dice}{str_mod}"
    return max(1, _safe_roll(expr, fallback="1d8"))


def _player_ac(character: dict | None) -> int:
    """Approximate the player's AC from armor + DEX (good enough for solo play)."""
    if not character:
        return _DEFAULT_PLAYER_AC
    stats = character.get("stats") or {}
    dex_mod = _ability_mod(stats.get("DEX", 10))
    inv = character.get("inventory") or {}
    armor = str(inv.get("armor", "")).lower()
    if "plate" in armor:
        return 18
    if "chain mail" in armor:
        return 16
    if "chain shirt" in armor or "scale" in armor:
        return 13 + min(dex_mod, 2)
    if "leather" in armor:
        return 11 + dex_mod
    return 10 + dex_mod


def _targets_from_state(state: AgentState) -> list[str]:
    parsed = state.get("parsed_input")
    entities = parsed.entities if parsed else {}
    raw = entities.get("targets", entities.get("target"))
    if raw is None or raw == "":
        raw = "enemy"
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(t) for t in raw if t] or ["enemy"]
    return [str(raw)]


async def _build_enemy_roster(state: AgentState, targets: list[str]) -> list[dict]:
    """One grounded LLM call to stat the enemies; falls back to sensible defaults on any failure."""
    rules_ctx = state.get("rules_context", []) or []
    adv_ctx = state.get("adventure_context", []) or []
    player_input = state.get("player_input", "")
    npcs = state.get("npcs") or []
    ally_names = [n.get("name") for n in npcs if isinstance(n, dict) and n.get("name")]
    ctx_lines = [
        f"Player's action this turn: {player_input}",
        f"Foes the player named: {', '.join(targets)}",
    ]
    if ally_names:
        ctx_lines.append(
            f"These are the player's ALLIES — never stat them as enemies: {', '.join(ally_names)}"
        )
    if adv_ctx:
        ctx_lines.append("Scene context (for flavour only; do not turn allies into enemies):")
        ctx_lines += [f"- {c.get('text', '')[:300]}" for c in adv_ctx[:2]]
    if rules_ctx:
        ctx_lines.append("Monster/rules context:")
        ctx_lines += [f"- {c.get('text', '')[:300]}" for c in rules_ctx[:2]]

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
        for e in setup.enemies:
            enemies.append(
                {
                    "name": e.name,
                    "hp_max": e.hp_max,
                    "hp_current": e.hp_max,
                    "ac": e.ac,
                    "attack_bonus": e.attack_bonus,
                    "damage_dice": e.damage_dice,
                    "is_player": False,
                    "alive": True,
                }
            )
    except Exception:
        enemies = []

    if not enemies:
        # Deterministic fallback: one enemy per named target.
        seen: dict[str, int] = {}
        for t in targets:
            seen[t] = seen.get(t, 0) + 1
            name = t if seen[t] == 1 else f"{t} {seen[t]}"
            enemies.append(
                {
                    "name": name,
                    **_DEFAULT_ENEMY,
                    "hp_current": _DEFAULT_ENEMY["hp_max"],
                    "is_player": False,
                    "alive": True,
                }
            )
    return enemies


def _initiative(combatants: list[dict]) -> list[str]:
    rolled = []
    for c in combatants:
        init = roll("1d20").total
        rolled.append((init, c["name"]))
    rolled.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in rolled]


def _find_target(enemies: list[dict], named: list[str]) -> dict | None:
    living = [e for e in enemies if e["alive"] and e["hp_current"] > 0]
    if not living:
        return None
    for n in named:
        nl = n.lower()
        for e in living:
            if nl in e["name"].lower() or e["name"].lower() in nl:
                return e
    return living[0]


async def combat_round_node(state: AgentState) -> dict:
    """Initialise the encounter if needed, then resolve exactly one round."""
    combat_state = dict(state.get("combat_state") or {})
    character = state.get("character") or {}
    player_input = state.get("player_input", "")
    player_hp = int(character.get("hp_current", combat_state.get("player_hp", 10) or 10))
    player_hp_max = int(character.get("hp_max", player_hp) or player_hp)
    player_ac = combat_state.get("player_ac") or _player_ac(character)

    log_lines: list[str] = []

    # --- Retrieve context + adjudicate the player's action (grounded, dice-decided) ---
    retr = await world_retriever_node(state)
    state = {**state, **retr}

    # Initialise on first entry.
    if not combat_state.get("combatants"):
        targets = _targets_from_state(state)
        enemies = await _build_enemy_roster(state, targets)
        player_combatant = {
            "name": "player",
            "display_name": character.get("name", "You"),
            "is_player": True,
            "hp_current": player_hp,
            "hp_max": player_hp_max,
            "ac": player_ac,
            "alive": player_hp > 0,
        }
        combatants = [player_combatant] + enemies
        combat_state.update(
            {
                "combatants": combatants,
                "initiative_order": _initiative(combatants),
                "round": 1,
                "current_turn_index": 0,
                "ended": False,
                "outcome": None,
                "player_ac": player_ac,
            }
        )
        log_lines.append(
            "Combat begins! Initiative: " + ", ".join(combat_state["initiative_order"]) + "."
        )

    combatants = combat_state["combatants"]
    enemies = [c for c in combatants if not c.get("is_player")]
    player_combatant = next((c for c in combatants if c.get("is_player")), None)
    if player_combatant is not None:
        # Re-sync player HP from the authoritative character row each turn.
        player_combatant["hp_current"] = player_hp
        player_combatant["hp_max"] = player_hp_max

    round_num = combat_state.get("round", 1)
    player_damage_taken = 0
    outcome: str | None = None

    fled = any(p in player_input.lower() for p in _FLEE_PATTERNS)

    # --- Player's action ---
    if fled:
        log_lines.append(f"{character.get('name', 'The hero')} attempts to disengage and flee the fight.")
        adj = None
    else:
        adj_updates = await rules_adjudicator_node(state)
        if adj_updates.get("error"):
            return {"error": adj_updates["error"]}
        adj = adj_updates.get("adjudication_result")
        target = _find_target(enemies, _targets_from_state(state))
        dealt = 0
        if adj is not None and adj.success and target is not None:
            dealt = _player_damage(character)
            target["hp_current"] = max(0, target["hp_current"] - dealt)
            if target["hp_current"] <= 0:
                target["alive"] = False
        summary = (adj.mechanical_summary if adj and adj.mechanical_summary else player_input)
        if target is not None and dealt > 0:
            status = "and it falls!" if not target["alive"] else f"({target['hp_current']}/{target['hp_max']} HP left)"
            log_lines.append(f"{character.get('name', 'The hero')}: {summary} — {dealt} damage to {target['name']} {status}")
        else:
            log_lines.append(f"{character.get('name', 'The hero')}: {summary}")

    living_enemies = [e for e in enemies if e["alive"] and e["hp_current"] > 0]

    # --- Enemy actions (deterministic) ---
    if living_enemies and player_hp > 0 and not fled:
        for e in living_enemies:
            atk = roll(f"1d20+{int(e.get('attack_bonus', 3))}").total
            if atk >= player_ac:
                dmg = _safe_roll(e.get("damage_dice", "1d6+1"))
                player_damage_taken += dmg
                log_lines.append(f"{e['name']} hits {character.get('name', 'you')} for {dmg} damage (rolled {atk} vs AC {player_ac}).")
            else:
                log_lines.append(f"{e['name']} misses {character.get('name', 'you')} (rolled {atk} vs AC {player_ac}).")

    # Fold any self HP change from the player's own action (e.g. a healing potion) into the
    # running HP before applying enemy damage.
    base_delta = (
        adj.character_update.hp_delta
        if adj and adj.character_update and adj.character_update.hp_delta
        else 0
    )
    player_hp_pre_enemy = max(0, min(player_hp_max, player_hp + base_delta))
    player_hp_after = max(0, player_hp_pre_enemy - player_damage_taken)
    second_wind_heal = 0
    # Fighter's Second Wind: once per encounter, when a blow would drop them, they rally.
    # Gives a solo fighter a realistic fighting chance against a boss without removing stakes.
    is_fighter = "fighter" in str(character.get("class", "")).lower()
    if (
        player_hp_after <= 0
        and is_fighter
        and not combat_state.get("second_wind_used")
        and not fled
    ):
        second_wind_heal = roll("1d10").total + 1
        player_hp_after = second_wind_heal
        combat_state["second_wind_used"] = True
        log_lines.append(
            f"{character.get('name', 'You')} digs deep and uses Second Wind, recovering "
            f"{second_wind_heal} HP to stay in the fight!"
        )
    if player_combatant is not None:
        player_combatant["hp_current"] = player_hp_after
        player_combatant["alive"] = player_hp_after > 0

    living_enemies = [e for e in enemies if e["alive"] and e["hp_current"] > 0]

    # --- End conditions ---
    ended = False
    if fled:
        ended, outcome = True, "fled"
        log_lines.append("You break away from the fight and escape.")
    elif not living_enemies:
        ended, outcome = True, "victory"
        log_lines.append("All enemies are defeated. Victory!")
    elif player_hp_after <= 0:
        ended, outcome = True, "defeat"
        log_lines.append(f"{character.get('name', 'You')} is overwhelmed and falls unconscious, subdued by the enemy.")
    elif round_num >= MAX_COMBAT_ROUNDS:
        ended, outcome = True, "fled"
        log_lines.append("The fight drags on; you disengage to regroup.")

    next_round = round_num if ended else round_num + 1
    combat_state.update(
        {
            "combatants": combatants,
            "round": next_round,
            "ended": ended,
            "outcome": outcome,
        }
    )

    # --- Build the adjudication the API persists (player HP + any flags/scene from the action) ---
    from agent.models.adjudication import AdjudicationResult, CharacterUpdate

    if adj is None:
        adj = AdjudicationResult(success=not fled, mechanical_summary="")
    cu = adj.character_update or CharacterUpdate()
    if outcome == "defeat":
        # Solo campaigns have no human GM to adjudicate death, so a "defeat" means the player
        # is subdued / knocked unconscious (and the story continues — often captured), rather
        # than a permanent dead-end. Leave them at 1 HP.
        player_hp_after = 1
        if player_combatant is not None:
            player_combatant["hp_current"] = 1
    # Persist exactly the HP the combat resolved to (covers self-heal, enemy damage, Second
    # Wind, and the subdued-at-1 floor) as a single delta off the authoritative row.
    cu.hp_delta = player_hp_after - player_hp
    adj.character_update = cu
    adj.mechanical_summary = " ".join(log_lines)

    # --- Narrate the whole round in one call ---
    narr_state = {
        **state,
        "adjudication_result": adj,
        "combat_state": combat_state,
    }
    narr_updates = await narrator_node(narr_state)
    narration = narr_updates.get("narration", "")
    if outcome == "victory":
        narration += "\n\n**Victory!** The way forward is clear."
    elif outcome == "defeat":
        narration += "\n\n**Subdued.** You are overwhelmed and knocked unconscious — your story is not over, but you are at the enemy's mercy."
    elif outcome == "fled":
        narration += "\n\n*(You have left the fight.)*"

    out: dict[str, Any] = {
        "combat_state": combat_state,
        "messages": list(narr_updates.get("messages", state.get("messages", []))),
        "narration": narration,
        "adjudication_result": adj,
    }
    return out


def build_combat_subgraph():
    """Compile the single-node combat subgraph (one full round per invocation)."""
    from langgraph.graph import END, START, StateGraph

    combat_graph = StateGraph(AgentState)
    combat_graph.add_node("round", combat_round_node)
    combat_graph.add_edge(START, "round")
    combat_graph.add_edge("round", END)
    return combat_graph.compile()
