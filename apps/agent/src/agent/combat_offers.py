"""Nonviolent combat offers: source-bound NPC response, never an attack or an escape."""

from __future__ import annotations

import re

from agent.combat_confirmation import stored_declaration
from agent.combat_identity import unique_roster_target
from agent.combat_view import _ask_player
from agent.combatants import clear_effects, stable_id
from agent.models.parsed_input import ParsedInput
from agent.scene_guidance import observation_span_catalog


def _actor_span(offer, actor, spans, enemies):
    """Bind pronoun conditions to a nearby explicit actor, never another passage or foe.

    Sentence slicing must not sever a source's 'Guz demands ... He refuses ...'.
    The model selects both sentences; offsets and named-foe checks bound that selection.
    """
    condition = spans.get(offer.source_span_id, {})
    labels = {
        str(value)
        for value in (
            actor.get("display_name"),
            actor.get("canonical_name"),
            actor["name"],
            *(actor.get("target_aliases") or []),
        )
        if value
    }

    def names(text, labels):
        return any(re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text, re.I) for label in labels)

    if names(condition.get("text", ""), labels):
        return True
    anchor = spans.get(offer.actor_source_span_id, {})
    if not anchor or anchor.get("reference_id") != condition.get("reference_id"):
        return False
    if not isinstance(anchor.get("end"), int) or not isinstance(condition.get("start"), int):
        return False
    if condition["start"] != anchor["end"]:
        return False
    if not re.match(r"\s*(?:he|she|they|his|her|their)\b", condition.get("text", ""), re.I):
        return False
    other_labels = {
        str(foe.get("display_name") or foe["name"]) for foe in enemies if foe is not actor
    }
    return names(anchor.get("text", ""), labels) and not names(
        condition.get("text", ""), other_labels
    )


async def resolve_offer(
    state, combat_state, player, enemies, *, text, parsed, confirmed, adjudicate
):
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
        stored_declaration(
            player,
            player_input=text,
            parsed=parsed,
            summary=summary,
            round_num=int(combat_state.get("round") or 1),
        )
        return _ask_player(state, summary, combat_state=combat_state)
    if sum(bool(row.get("is_player")) for row in combat_state.get("combatants", [])) > 1:
        return _ask_player(
            state,
            "The other heroes have not agreed to surrender. State individual terms or choose your next action. "
            "No roll is made and no turn is spent.",
            combat_state=combat_state,
        )
    spans = observation_span_catalog(state)
    fallback = (
        "Your offer has not been accepted: the current scene does not establish this foe's response "
        "to those terms. Offer surrender with a concrete exchange, or choose a defensive action. "
        "The fight remains active. "
        "You have not attacked or fled. No roll is made and no turn is spent."
    )
    if not spans:
        return _ask_player(state, fallback, combat_state=combat_state)
    offer_state = {
        **state,
        "player_input": text,
        "parsed_input": ParsedInput(intent="rp", target=getattr(parsed, "target", None)),
    }
    ruling = await adjudicate(
        offer_state,
        in_combat=True,
        extra_outcome=(
            "No attack, flight, check or damage occurred. Resolve this explicit nonviolent offer with "
            "combat_offer only. Select a current eligible source_span_id supporting the present foe's "
            "acceptance or refusal of these actual terms. If the source does not support a response or "
            "a required condition is unmet/unknown, return uncertain and state the specific condition "
            "in public_response. For rejected, explain the source-supported reason in public_response; "
            "do not substitute 'insufficient evidence' for an NPC response. For accepted, give the NPC's "
            "reply. If the condition uses a pronoun, also select actor_source_span_id naming this foe "
            "in a nearby preceding sentence of the same passage. Never use another actor's terms. "
            "Public responses must contain only information this present foe would say aloud; do not "
            "reveal private plans or future events. Do not add captured status, "
            "inventory transfer or travel; those require separate validated story actions."
        ),
    )
    offer = ruling.combat_offer
    actor = unique_roster_target(enemies, offer.actor_name) if offer else None
    valid = bool(
        offer
        and actor is not None
        and offer.source_span_id in spans
        and offer.action_quote.strip()
        and offer.action_quote.casefold() in text.casefold()
        and offer.achievement_basis.strip()
        and _actor_span(offer, actor, spans, enemies)
    )
    if not valid:
        return _ask_player(state, fallback, combat_state=combat_state)
    label = str(actor.get("display_name") or actor["name"])
    response = offer.public_response.strip()
    if offer.decision == "uncertain":
        clarification = (
            f"{label}: {response}"
            if response
            else (
                f"{label}'s terms are still unclear. Offer surrender with explicit terms, "
                "including whether you accept the demand already made."
            )
        )
        return _ask_player(
            state,
            clarification + " The fight remains active. No roll is made and no turn is spent.",
            combat_state=combat_state,
        )
    player["combat_offer_receipt"] = {
        "decision": offer.decision,
        "actor_id": stable_id(actor),
        "source_span_id": offer.source_span_id,
        "reference_id": spans[offer.source_span_id]["reference_id"],
        "action_quote": offer.action_quote,
        "achievement_basis": offer.achievement_basis,
        "actor_source_span_id": offer.actor_source_span_id,
        "public_response": response,
    }
    # Proposed world writes are discarded: stopping combat authorizes no unrelated mutation.
    if offer.decision == "rejected":
        reply = f"{label}: {response} " if response else ""
        return {
            "_offer_rejected": f"{reply}{label} refuses your offer to stop fighting on those terms. You spend your action making the offer; the encounter continues in initiative order."
        }
    clear_effects(combat_state.get("combatants", []))
    for row in combat_state.get("combatants", []):
        for key in ("concentration", "raging", "rage_round"):
            row.pop(key, None)
    event = {
        "kind": "surrender",
        "text": f"{label} accepts your offer to stop fighting.",
        "actor_id": stable_id(player),
        "target_id": stable_id(actor),
    }
    player["round_events"] = [event]
    combat_state.update(ended=True, outcome="surrendered", round_events=[event], current_turn=None)
    return _ask_player(
        state,
        (f"{label}: {response} " if response else "")
        + f"{label} accepts your offer to stop fighting. **Surrender agreed.** "
        "You remain here with your current HP and equipment; further terms must be resolved in play.",
        combat_state=combat_state,
    )
