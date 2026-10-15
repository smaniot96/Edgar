"""Combatant disposition: the single source of truth for who is living, who can act (EDG-022/023).

Stored on every combatant:
  * `disposition`: one of DISPOSITIONS. `active` (standing in the fight), `unconscious` (living,
    knocked out by a declared nonlethal blow), `captured` (living captive after a victory),
    `dead` (lethal blow), `defeated` (reduced to 0 HP with no declared intent; the cause is
    unknown, so it is never narrated as death or capture).
  * `alive`: kept for existing readers with the living-vs-not meaning. True only for living
    dispositions (active, unconscious, captured). False for dead and defeated.

Derived, never stored: `can_act` (active and above 0 HP). Only a combatant that can act attacks.

Legacy rows (written before `disposition` existed) carry only `alive`/`hp_current`. They are
read honestly: `alive` False means "defeated" (cause unknown). It is not treated as proof of
death or capture, and no nonlethal intent is invented. No stored data is rewritten.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, Literal

Lethality = Literal["lethal", "nonlethal"]

DISPOSITIONS: tuple[str, ...] = ("active", "unconscious", "captured", "dead", "defeated")
LIVING_DISPOSITIONS = frozenset({"active", "unconscious", "captured"})
# Display vocabulary for the narrator. Never claims death or capture for `defeated`.
_STATUS_TEXT = {
    "active": "active, can act",
    "unconscious": "unconscious, cannot act",
    "captured": "captured, cannot act",
    "dead": "dead, cannot act",
    "defeated": "defeated, cannot act",
}

# The player's own declared intent, read from their words. Nonlethal wins when both appear
# ("I try to knock him out, I don't want to kill him").
_NONLETHAL_TEXT = re.compile(
    r"\b(?:knock\w*\s+(?:\w+\s+){0,3}out|subdu\w*|non-?lethal\w*|take\s+\w+\s+(?:alive|prisoner)"
    r"|capture\w*|(?:don'?t|do\s+not|won'?t|will\s+not|without)\b[^.!?\n]{0,40}?\bkill\w*"
    r"|spare\s+(?:\w+\s+)?life"
    r"|stun\w*)\b",
    re.IGNORECASE,
)
_LETHAL_TEXT = re.compile(
    r"\b(?:kill|kills|killing|slay|slays|execute|executes|finish\s+(?:him|her|them|it)\s+off"
    r"|cut\s+(?:him|her|them|it)\s+down|butcher)\b",
    re.IGNORECASE,
)
# Post-combat NPC dispositions written for combatants (session NPC rows, not manual records).
NPC_DISPOSITION = {"captured": "captive", "dead": "dead"}
INERT_NPC_DISPOSITIONS = frozenset(NPC_DISPOSITION.values())
_ATTACK_VERBS = (
    r"hits?|strikes?|swings?|attacks?|lunges?|stabs?|shoots?|slashes?|bites?|slams?|cuts?|"
    r"thrusts?|fires|lashes?|smashes?|clubs?|jabs?|hurls?|charges?"
)
_OTHER_ATTACKER = re.compile(
    r"\b(?:other|second|another|additional|third|remaining)\s+([a-z][a-z-]*)\b", re.IGNORECASE
)


def display_name(c: Mapping[str, Any]) -> str:
    return str(c.get("display_name") or c.get("name") or "?")


def stable_id(c: Mapping[str, Any]) -> str:
    """Encounter-unique key (roster names are deduplicated when the roster is built)."""
    return str(c.get("name") or "?")


def _hp(c: Mapping[str, Any]) -> int:
    value = c.get("hp_current")
    return int(value) if value is not None else 1


def disposition_of(c: Mapping[str, Any]) -> str:
    stored = c.get("disposition")
    if stored in DISPOSITIONS:
        return str(stored)
    # Legacy row: no disposition was stored, so it is read from `alive` and HP only.
    if c.get("alive", True) is False or _hp(c) <= 0:
        return "defeated"
    return "active"


def is_living(c: Mapping[str, Any]) -> bool:
    return disposition_of(c) in LIVING_DISPOSITIONS


def can_act(c: Mapping[str, Any]) -> bool:
    return disposition_of(c) == "active" and _hp(c) > 0


def declared_lethality(parsed: Any, player_input: str | None) -> Lethality | None:
    """The player's declared intent for an attack: the parser's field, else their own words."""
    declared = getattr(parsed, "lethality", None)
    if declared in ("lethal", "nonlethal"):
        return declared
    text = player_input or ""
    if _NONLETHAL_TEXT.search(text):
        return "nonlethal"
    if _LETHAL_TEXT.search(text):
        return "lethal"
    return None


def apply_defeat(c: dict, lethality: Lethality | None) -> None:
    """Record a combatant reduced to 0 HP. Unknown intent is `defeated`, never `dead`."""
    c["hp_current"] = 0
    if lethality == "lethal":
        disposition = "dead"
    elif lethality == "nonlethal":
        disposition = "unconscious"
    else:
        disposition = "defeated"
    c["disposition"] = disposition
    c["alive"] = disposition in LIVING_DISPOSITIONS


def apply_player_disposition(player: dict, *, subdued: bool) -> None:
    """The player stands while above 0 HP; a subdued defeat leaves them unconscious (alive)."""
    disposition = "unconscious" if subdued or _hp(player) <= 0 else "active"
    player["disposition"] = disposition
    player["alive"] = disposition in LIVING_DISPOSITIONS


def finish_victory(combatants: Iterable[dict]) -> None:
    """Knocked-out foes are living captives once the fight is over."""
    for c in combatants:
        if not c.get("is_player") and disposition_of(c) == "unconscious":
            c["disposition"] = "captured"
            c["alive"] = True


def view_fields(c: Mapping[str, Any]) -> dict[str, Any]:
    """Public, shape-stable disposition fields for the roster, HUD and narrator."""
    disposition = disposition_of(c)
    return {
        "id": stable_id(c),
        "disposition": disposition,
        "can_act": can_act(c),
        "alive": disposition in LIVING_DISPOSITIONS,
    }


def narrator_roster(combatants: Iterable[Mapping[str, Any]], round_actors: Iterable[str]) -> str:
    """Authoritative roster text for the round narration. The engine decides; prose must agree."""
    enemies = [c for c in combatants if not c.get("is_player")]
    lines = [
        f'- id "{stable_id(c)}", name "{display_name(c)}": {_STATUS_TEXT[disposition_of(c)]}'
        for c in enemies
    ]
    able = [display_name(c) for c in enemies if can_act(c)]
    actors = list(dict.fromkeys(round_actors))
    return (
        "Combat roster (authoritative; the engine decided every outcome):\n"
        + "\n".join(lines)
        + f"\nCombatants able to act now: {', '.join(able) if able else 'none'}."
        + (
            f"\nEnemy attacks this round were made only by: {', '.join(actors)}."
            if actors
            else "\nNo enemy attacked this round."
        )
        + "\nName the attacker with its exact name above. Do not describe a dead, unconscious, "
        "captured or defeated combatant as attacking or acting, and do not invent another attacker "
        "(such as an unlisted 'other', 'second' or 'another' raider)."
    )


def roster_violations(text: str, combatants: Iterable[Mapping[str, Any]]) -> list[str]:
    """Deterministic post-check: prose that revives an inactive foe or invents an attacker."""
    enemies = [c for c in combatants if not c.get("is_player")]
    names = [display_name(c) for c in enemies]
    problems: list[str] = []
    for c in enemies:
        if can_act(c):
            continue
        name = display_name(c)
        # "Raider" must not match the start of "Raider Captain": exclude the longer names' tails.
        tails = [
            re.escape(n[len(name) + 1 :])
            for n in names
            if n != name and n.lower().startswith(name.lower() + " ")
        ]
        guard = f"(?!\\s+(?:{'|'.join(tails)})\\b)" if tails else ""
        pattern = re.compile(
            rf"(?<![\w]){re.escape(name)}{guard}(?![\w'’-])[^.!?\n]*?\b(?:{_ATTACK_VERBS})\b",
            re.IGNORECASE,
        )
        if pattern.search(text):
            problems.append(name)
    active_nouns = Counter(_noun(display_name(c)) for c in enemies if can_act(c))
    all_nouns = {_noun(display_name(c)) for c in enemies}
    for match in _OTHER_ATTACKER.finditer(text):
        noun = match.group(1).lower()
        if noun in all_nouns and active_nouns[noun] <= 1:
            problems.append(match.group(0))
    return problems


def _noun(name: str) -> str:
    """Base noun of a roster name ('Raider Captain' -> 'captain', 'Raider 2' -> 'raider')."""
    words = [w for w in name.split() if not w.isdigit()]
    return words[-1].lower() if words else ""
