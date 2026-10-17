"""Building a combatant's exact numbers, and the hero's armour class (EDG-057 split from graph_combat).

`_exact_enemy` turns a verified creature record into a roster entry whose numbers come only from that
record. The NPC helpers keep allies, captives and dead foes out of a new fight, and `_player_ac`
approximates the hero's AC from the sheet. The encounter builder's model call and the creature lookup
stay in `agent.graph_combat`, which owns those seams.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, Field

from agent.combatants import INERT_NPC_DISPOSITIONS
from agent.dmg_thresholds import printed_challenge
from agent.monster_stats import (
    MonsterStatsError,
    combat_fields,
    parse_lair_actions,
    parse_monster_stats,
)
from agent.prompts import UNTRUSTED_INPUT_NOTICE
from agent.resolution import ability_modifier, carries_shield, character_class
from agent.state import AgentState
from db.vector.creatures import CreatureResolutionError
from tools.dice import parse_dice

_DEFAULT_ENEMY = {"hp_max": 9, "ac": 12, "attack_bonus": 3, "damage_dice": "1d6+1", "initiative_bonus": 1}


_DEFAULT_PLAYER_AC = 12


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
    shield = 2 if carries_shield(character) else 0
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
        "actor_surprise",
        "passage_id",
        "manifest_sha256",
        "lair_reference",
        "actor_rules_edition",
        "actor_source_sha256",
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
        if identity.get("lair_text"):
            actions = parse_lair_actions(str(identity["lair_text"]))
            declared = stats.declared + (
                "reviewed lair passage: point choice, range, persistent areas, pull, flight, and dismissal effects remain unexecuted",
            )
            if not actions:
                declared += (
                    "reviewed lair passage: no supported exact save action parsed; its effects are not executed",
                )
            stats = replace(stats, lair_actions=actions, declared=declared)
    except MonsterStatsError as exc:
        raise CreatureResolutionError(
            f"Creature record for {name!r} has no parseable stat block ({exc}). "
            "Review and reingest its source."
        ) from exc
    challenge = printed_challenge(str(identity.get("text") or ""))  # the record's own CR and XP
    return {
        "name": name[:60],
        "creature_id": identity["creature_id"],
        "source_reference": _creature_source_reference(identity),
        "stat_adaptation": "exact_record",
        "challenge_cr": challenge[0] if challenge else None,
        "challenge_xp": challenge[1] if challenge else None,
        **combat_fields(stats),
        "monster_stats": stats.to_dict(),
        "surprised": identity.get("actor_surprise") == "actor",
        "hero_surprised": identity.get("actor_surprise") == "hero",
        "is_boss": bool(is_boss),
        "is_player": False,
        "disposition": "active",
        "alive": True,
    }
