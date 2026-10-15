"""Player actions on a captive after its fight (EDG-146). Pure: no dice, no database, no model.

A captive is a combat-owned NPC row (`combat:<id>:<hash>`, disposition `captive`). The engine
decides exactly one of these outcomes from the player's own words:

  * release: the captive becomes `released`. It is neutral, never an enemy again, and cannot be
    attacked as a foe.
  * kill: only on an explicit lethal declaration in the player's words ("kill", "execute"...).
    The captive becomes `dead`.
  * ruling: an attack on a captive is never a silent no-op and never starts a fight against a
    helpless captive. The engine asks which action the player means.

Talking to or questioning a captive is not intercepted here; the narrator handles it as before.
Parser lethality is deliberately not used, because only the player's own words declare a kill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.combatants import classify_combat_maneuver, declared_lethality
from db.vector.references import reference_matches

_ATTACK_WORDS = re.compile(
    r"\b(?:attack\w*|strik\w*|stab\w*|hit|hits|hitting|swing\w*|slash\w*|shoot\w*|fight\w*"
    r"|kill\w*|slay\w*|execut\w*|cut\w*\s+(?:him|her|them|it)\s+down|butcher\w*)\b",
    re.IGNORECASE,
)
_CAPTIVE_OUTCOME_ACTION = {"release": "released", "kill": "dead"}


@dataclass(frozen=True)
class CaptiveTurn:
    kind: str  # "release" | "kill" | "ruling"
    text: str  # the engine's deterministic narration for this turn
    outcome: dict[str, Any] | None = None  # persisted with the turn for release / kill


def is_attack_text(text: str | None) -> bool:
    return bool(_ATTACK_WORDS.search(text or ""))


def captive_turn(
    player_input: str | None,
    named: list[str],
    captives: list[dict[str, Any]],
    *,
    attack_intent: bool,
) -> CaptiveTurn | None:
    """The engine ruling for a captive the player names, or None when narration handles it.

    `captives` are the combat-owned NPC rows of this session: {name, disposition, combat_id,
    combatant_id}. Rows without that owner are never passed in, so manual NPCs are never touched.
    """
    text = player_input or ""
    matches = [c for c in captives if any(reference_matches(n, c["name"]) for n in named)]
    if not matches:
        return None
    if len(matches) > 1:
        options = ", ".join(c["name"] for c in matches)
        return CaptiveTurn(
            "ruling", f"Which one do you mean? ({options})? Name the one you act on."
        )
    target = matches[0]
    name = target["name"]
    manoeuvre = classify_combat_maneuver(text)
    wants_release = manoeuvre is not None and manoeuvre.kind == "release"
    # Only the player's words declare a kill; the parser's lethality field is not trusted here.
    wants_kill = declared_lethality(None, text) == "lethal"
    disposition = target.get("disposition")
    if disposition in ("dead", "released"):
        if not (wants_release or wants_kill or attack_intent):
            return None
        state = "already dead" if disposition == "dead" else "no longer your captive"
        return CaptiveTurn("ruling", f"{name} is {state}. No attack is made and no combat starts.")
    if disposition != "captive":
        return None
    if wants_release and wants_kill:
        return CaptiveTurn(
            "ruling",
            f"{name} is your captive. Say whether you release or kill them; the engine cannot "
            "decide both at once.",
        )
    if wants_release:
        return CaptiveTurn(
            "release",
            f"You release {name}. {name} is no longer your captive and is not an enemy in this "
            "fight.",
            _outcome("release", target),
        )
    if wants_kill:
        return CaptiveTurn(
            "kill",
            f"You kill {name}, who was your captive. {name} is dead.",
            _outcome("kill", target),
        )
    if attack_intent:
        return CaptiveTurn(
            "ruling",
            f"{name} is your captive; do you release, question, or kill them? No attack is made "
            "on a captive without a declared kill.",
        )
    return None


def _outcome(action: str, target: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "npc_disposition": _CAPTIVE_OUTCOME_ACTION[action],
        "combat_id": target["combat_id"],
        "combatant_id": target["combatant_id"],
        "combat_round": target.get("combat_round"),
        "name": target["name"],
    }
