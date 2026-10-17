"""Which foe a declared action refers to, from the living roster (EDG-057 split from graph_combat).

Pure selection over the roster order and the player's named targets. A refused or unclear target
becomes a question to the player in `agent.graph_combat`; nothing here rolls a die.
"""

from __future__ import annotations

from agent.combat_intent import _name_matches
from agent.combatants import has_effect, is_standing
from agent.conditions import humanoid_refusal
from agent.spells import SpellSpec


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


def additional_spell_targets(
    enemies_in_order: list[dict], named: list[str], first: dict, spec: SpellSpec, count: int
) -> list[dict]:
    """Up to `count` more standing foes for an upcast spell: those the player named, then roster order.

    A humanoid-only spell takes only foes whose record reads humanoid; none is refused, the cast
    simply reaches fewer foes.
    """
    eligible = [
        e
        for e in enemies_in_order
        if e is not first
        and is_standing(e)
        and not (spec.humanoid_only and humanoid_refusal(spec.name, e))
    ]
    # The sort is stable: named foes come first, the rest keep their roster order.
    eligible.sort(key=lambda e: not any(_name_matches(n, e["name"]) for n in named))
    return eligible[:count]


def _release_target(enemies_in_order: list[dict], named: list[str]) -> dict | None:
    """The restrained foe a player's declared release refers to (the only one when unnamed)."""
    bound = [e for e in enemies_in_order if has_effect(e, "restrained")]
    for n in named:
        for e in bound:
            if _name_matches(n, e["name"]):
                return e
    return bound[0] if bound and not named else None
