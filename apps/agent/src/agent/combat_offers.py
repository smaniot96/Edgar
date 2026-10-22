"""Nonviolent combat offers: source-bound NPC response, never an attack or an escape."""

from __future__ import annotations

import re

from agent.combat_confirmation import stored_declaration
from agent.combat_intent import _name_matches
from agent.combat_view import _ask_player
from agent.combatants import clear_effects, stable_id
from agent.models.parsed_input import ParsedInput
from agent.scene_guidance import observation_span_catalog


async def resolve_offer(state, combat_state, player, enemies, *, text, parsed, confirmed, adjudicate):
    """Confirm actual terms, then authorize the NPC response with current source evidence.

    Agreement to stop combat implies no capture, inventory transfer or journey. Unknown/rejected
    unsupported terms leave mechanical values alone. A supported refusal spends the offered action.
    """
    hero = str((state.get("character") or {}).get("name") or "The hero")
    if not confirmed:
        summary = (
            f"Confirm before any dice: {hero} will offer surrender or a ceasefire on the terms stated. "
            "This does not mean fleeing, attacking, or automatic acceptance. Reply yes to offer it, "
            "or no to change it. A supported refusal spends your action and the enemies take their turns; "
            "unknown or unsupported terms are clarified without a turn. No roll is made until you confirm."
        )
        stored_declaration(player, player_input=text, parsed=parsed, summary=summary,
                           round_num=int(combat_state.get("round") or 1))
        return _ask_player(state, summary, combat_state=combat_state)
    if sum(bool(row.get("is_player")) for row in combat_state.get("combatants", [])) > 1:
        return _ask_player(
            state, "The other heroes have not agreed to surrender. State individual terms or choose your next action. "
            "No roll is made and no turn is spent.", combat_state=combat_state,
        )
    spans = observation_span_catalog(state)
    fallback = (
        "Your offer has not been accepted on the available evidence. The fight remains active; "
        "you can change your terms, keep defending yourself, or declare another action. "
        "You have not attacked or fled. No roll is made and no turn is spent."
    )
    if not spans:
        return _ask_player(state, fallback, combat_state=combat_state)
    offer_state = {
        **state, "player_input": text,
        "parsed_input": ParsedInput(intent="rp", target=getattr(parsed, "target", None)),
    }
    ruling = await adjudicate(
        offer_state, in_combat=True,
        extra_outcome=(
            "No attack, flight, check or damage occurred. Resolve this explicit nonviolent offer with "
            "combat_offer only. Select a current eligible source_span_id supporting the present foe's "
            "acceptance or refusal of these actual terms. If the source does not support a response or "
            "a required condition is unmet/unknown, return uncertain. Do not add captured status, "
            "inventory transfer or travel; those require separate validated story actions."
        ),
    )
    offer = ruling.combat_offer
    actor = next((foe for foe in enemies if offer and _name_matches(offer.actor_name, foe["name"])), None)
    valid = bool(
        offer and actor is not None and offer.source_span_id in spans
        and offer.action_quote.strip() and offer.action_quote.casefold() in text.casefold()
        and offer.achievement_basis.strip()
        and re.search(r"(?<!\w)" + re.escape(str(actor.get("display_name") or actor["name"])) + r"(?!\w)", spans[offer.source_span_id]["text"], re.I)
    )
    if not valid or offer.decision == "uncertain":
        return _ask_player(state, fallback, combat_state=combat_state)
    player["combat_offer_receipt"] = {
        "decision": offer.decision, "actor_id": stable_id(actor),
        "source_span_id": offer.source_span_id,
        "reference_id": spans[offer.source_span_id]["reference_id"],
        "action_quote": offer.action_quote, "achievement_basis": offer.achievement_basis,
    }
    # Proposed world writes are discarded: stopping combat authorizes no unrelated mutation.
    label = str(actor.get("display_name") or actor["name"])
    if offer.decision == "rejected":
        return {"_offer_rejected": f"{label} refuses your offer to stop fighting on those terms. You spend your action making the offer; the encounter continues in initiative order."}
    clear_effects(combat_state.get("combatants", []))
    for row in combat_state.get("combatants", []):
        for key in ("concentration", "raging", "rage_round"):
            row.pop(key, None)
    event = {"kind": "surrender", "text": f"{label} accepts your offer to stop fighting.",
             "actor_id": stable_id(player), "target_id": stable_id(actor)}
    player["round_events"] = [event]
    combat_state.update(ended=True, outcome="surrendered", round_events=[event], current_turn=None)
    return _ask_player(
        state, f"{label} accepts your offer to stop fighting. **Surrender agreed.** "
        "You remain here with your current HP and equipment; further terms must be resolved in play.",
        combat_state=combat_state,
    )
