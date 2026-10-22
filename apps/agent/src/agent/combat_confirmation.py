"""Confirmation of a multi-part combat declaration before any die (EDG-182, increment 2).

Owner decision (18 October 2026): a turn with more than one action, a move beside an action, or an
off-hand element is repeated back to the player, and no die is rolled until they reply "yes". A
simple turn skips the confirmation unless the player has chosen "always confirm" for this hero.

The pending declaration is stored on the hero's combatant row (`pending_declaration`), so it is
persisted with the encounter and survives a reload. Once confirmed it is taken from that stored
copy only: a reply that adds or changes anything is not a confirmation, so it is a new declaration
and the pending one is discarded. Nothing here rolls a die.
"""

from __future__ import annotations

import re
from typing import Any

from agent.combat_declaration import Declaration
from agent.combatants import stable_id

PENDING_KEY = "pending_declaration"
ALWAYS_KEY = "always_confirm"

_CONFIRM = re.compile(
    r"^\W*(?:yes|yep|yeah|confirm|confirmed|go ahead|do it|that'?s right|correct|ok|okay)\W*$",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^\W*(?:no|nope|cancel|change it|wait|never ?mind|not that)\W*$", re.IGNORECASE
)
_ALWAYS_ON = re.compile(r"^\W*always\s+confirm\W*$", re.IGNORECASE)
_ALWAYS_OFF = re.compile(
    r"^\W*(?:stop|don'?t|do not|never)\s+(?:always\s+)?confirm(?:ing)?\W*$", re.IGNORECASE
)


def control_word(text: str | None) -> str | None:
    """A reply that answers a pending confirmation or sets the option. Anything else is None."""
    value = str(text or "")
    if _CONFIRM.match(value):
        return "confirm"
    if _CANCEL.match(value):
        return "cancel"
    if _ALWAYS_ON.match(value):
        return "always_on"
    if _ALWAYS_OFF.match(value):
        return "always_off"
    return None


def pending_of(player: dict) -> dict[str, Any] | None:
    pending = player.get(PENDING_KEY)
    return pending if isinstance(pending, dict) and pending.get("player_input") else None


def needs_confirmation(decl: Declaration, *, always: bool) -> bool:
    """True when the turn must be repeated back before any die (owner rule, EDG-182).

    A multi-part turn (two or more actions, or a move beside an action) and a conditional turn (a
    contingency or a standing order) are confirmed on every path, typed or legacy. A player who chose
    "always confirm" is asked about any turn that declares an action.
    """
    if always:
        return bool(decl.declared) or decl.moving or decl.is_attack or decl.is_spell
    parts = len({d.kind for d in decl.declared}) + (1 if decl.moving else 0)
    standing = decl.ready is not None and decl.ready.standing
    return (
        parts >= 2
        or getattr(decl, "main_attack_count", 1) > 1
        or (decl.moving and decl.is_attack)
        or decl.contingency is not None
        or standing
    )


def summary_of(hero: str, decl: Declaration) -> str:
    """The turn as the engine will run it, repeated back to the player."""
    steps: list[str] = []
    ordered = decl.execution_order or tuple(
        (["move"] if decl.moving else []) + [d.kind for d in decl.declared]
    )
    for kind in ordered:
        if kind == "move":
            steps.append("move")
            continue
        d = next(d for d in decl.declared if d.kind == kind)
        if d.kind in ("attack", "off_hand_attack") and decl.target is not None:
            label = f"{d.label} {decl.target['name']}"
            if d.kind == "attack" and decl.main_attack_count > 1:
                label += f" {decl.main_attack_count} times using Action Surge"
            steps.append(label)
        else:
            steps.append(d.label)
    if decl.is_spell and decl.spell_plan is not None and decl.spell_plan.spell.name not in steps:
        steps.append(decl.spell_plan.spell.name)
    what = " then ".join(steps) if steps else "take the turn as you described it"
    if decl.contingency is not None:
        what += f"; {decl.contingency.describe()}, {decl.contingency.branch}"
    if decl.ready is not None and decl.ready.standing:
        what = f"keep a standing order to attack {decl.ready.foe_name} each time it closes in (until you cancel it)"
    return (
        f"Confirm before any dice: {hero} will {what}. Reply yes to commit, or no to change it. "
        "No roll is made and no turn is spent until you reply yes."
    )


def execution_plan(decl: Declaration) -> dict:
    """Persist the normalized engine plan so reload cannot silently reinterpret a confirmation."""
    return {
        "version": 1,
        "order": list(decl.execution_order),
        "main_attack_count": decl.main_attack_count,
        "actions": [{"kind": d.kind, "slot": d.slot} for d in decl.declared],
        "target_id": stable_id(decl.target) if decl.target is not None else None,
        "spell_target_ids": [stable_id(row) if row is not None else None for row in decl.spell_targets],
        "moves": [{"band": band, "target_ids": [stable_id(row) for row in foes]} for band, foes in decl.moves.steps],
        "distances": [{"target_id": stable_id(row), "feet": feet} for row, feet in decl.moves.distances],
    }


def stored_declaration(player: dict, *, player_input: str, parsed: Any, summary: str, round_num: int, declaration: Declaration | None = None) -> None:
    """Hold the declaration on the hero's combatant row, for confirmation or cancellation."""
    player[PENDING_KEY] = {
        **({"execution_plan": execution_plan(declaration)} if declaration is not None else {}),
        "player_input": player_input,
        # None stays None: a turn with no parser output takes the legacy path when it is restored.
        "parsed": parsed.model_dump() if hasattr(parsed, "model_dump") else None,
        "summary": summary,
        "round": round_num,
    }


def public_standing_orders(combatants: list[dict]) -> list[dict[str, str]]:
    """The hero's standing reaction orders, visible and cancellable (EDG-182): the foe each names."""
    from agent.combatants import effects_of

    player = next((c for c in combatants if c.get("is_player")), None)
    return [
        {"foe": str(e["foe_name"]), "summary": f"Attack {e['foe_name']} each time it closes in"}
        for e in effects_of(player or {})
        if e["kind"] == "readied" and e.get("standing") and e.get("foe_name")
    ]


def public_pending(combatants: list[dict]) -> dict[str, str] | None:
    """The client's view of a pending confirmation: the summary, nothing the engine re-uses."""
    # EDG-049: at a party table the pending reply belongs to whichever hero holds one.
    heroes = [c for c in combatants if c.get("is_player")]
    player = next((c for c in heroes if pending_of(c) is not None), None)
    pending = pending_of(player or {})
    return {"summary": str(pending["summary"])} if pending and pending.get("summary") else None
