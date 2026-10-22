"""Generate the player-facing narration from the adjudicated outcome.

`build_narrator_prompt` and `narrator_request` are exported separately so the SSE turn runner can
stream tokens with `llm.astream` while still using the same context-building logic. Combat rounds
use the same builder with the `play.combat_narrator` profile: the round's mechanics are finalised
first, then the narrator is called once, either here (sync) or by the streaming runner (EDG-079).
The narrator gets:
  - the player's literal message
  - the active character's HP/stats so it does not describe a fresh PC as bleeding
  - the authoritative scene id so it does not skip ahead to a different location
  - the latest world flags so it cannot un-unlock a door
  - the adjudication outcome (mechanical_summary, dice_result)
  - rules context (for mechanical fidelity) and adventure context (for story flavour)
  - the story-so-far summary (`memory_summary`) and NPC names/dispositions
  - every exchange after the summary boundary so the prose stays continuous, oldest dropped
    first (never the summary or committed facts) when the profile's `max_input_tokens` is exceeded

The player's text is wrapped in <player_action> tags (untrusted input). Output length is bounded
by `NARRATOR_MAX_TOKENS`; the prompt forbids HP/status blocks since the UI renders state.
"""

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.action_economy import economy_text
from agent.combatants import narrator_roster
from agent.history import fit_continuity, public_earned_facts
from agent.llm import NARRATOR_MAX_TOKENS, make_chat_model, message_text
from agent.prompts import NARRATOR, wrap_player_action, wrap_retrieved_excerpt
from agent.provider_trace import traced_call
from agent.state import AgentState
from agent.story_turn import resolved_scene
from agent.supported_rules import (
    actions_for_state,
    apply_narration_boundary,
    boundary_prompt_block,
    engine_applied_for_state,
)
from db.vector.retrieval import rule_policy
from edgar_core.safe_errors import provider_error_code
from edgar_core.sources import cited_excerpt, eligible_context
from edgar_core.story import player_story_effect

_NARRATOR_TEMPERATURE = 0.7
_RAG_BLOCK_TOP_K = 5
NARRATION_FAILED_MESSAGE = "Narration could not be completed."


def deterministic_narration(state: AgentState) -> str | None:
    """Shared sync/SSE rendering for exact quotations and sealed policy outcomes."""
    recall = state.get("exact_recall")
    if recall:
        page = f", p. {recall['page']}" if recall.get("page") else ""
        return f"From {recall['source']}{page}:\n\n{recall['text']}"
    if state.get("policy_rejection"):
        return "That approach is not available in the current circumstances."
    adjudication = state.get("adjudication_result")
    if (
        state.get("policy_narration")
        and adjudication is not None
        and not adjudication.success
        and not adjudication.accepted_story_effects
    ):
        # An empty failed inference has no authored consequence to quote. Let the
        # normal public-context narrator distinguish failure from visible facts,
        # previously earned knowledge and independent questions in a mixed action.
        # The engine outcome remains final; private evidence never reaches it.
        return None
    return state.get("policy_narration")


def make_narrator_model(profile: str = "play.narrator"):
    """Narrator chat model with the selected stage profile and bounded output."""
    return make_chat_model(
        profile=profile,
        temperature=_NARRATOR_TEMPERATURE,
        max_tokens=NARRATOR_MAX_TOKENS,
    )


def narration_profile(state: AgentState) -> str:
    """The stage profile a narration call uses: combat rounds have their own budget and model."""
    return "play.combat_narrator" if state.get("combat_state") else "play.narrator"


def narrator_request(state: AgentState, profile: str) -> tuple[Any, list[BaseMessage]]:
    """The chat model and prompt for one narrator call (shared by sync nodes and SSE streams)."""
    llm = make_narrator_model(profile)
    prompt = build_narrator_prompt(state, profile)
    return llm, prompt


def narrator_context(state: AgentState) -> tuple[list[dict], list[dict]]:
    """The same public evidence feeds narration and its persisted context references."""
    rules_context = state.get("rules_context", [])
    adventure_context = state.get("adventure_context", [])
    if not rules_context and not adventure_context:
        merged = state.get("retrieved_context", [])
        rules_context = [c for c in merged if c.get("kind") == "rules"]
        adventure_context = [c for c in merged if c.get("kind") == "adventure"]

    edition = state.get("rules_edition", "2014")
    rules_context = eligible_context(
        [c for c in rules_context if rule_policy(c) == "core"], edition
    )
    adventure_context = eligible_context(adventure_context, edition)
    if state.get("manifest_status") == "reviewed":
        adventure_context = [c for c in adventure_context if c.get("audience") == "player"]
        scene = resolved_scene(state)
        if scene and scene["moved"]:
            # The old scene's opening cannot reset an arrival or repeat an interruption.
            adventure_context = scene["public_context"]
    return rules_context[:_RAG_BLOCK_TOP_K], (
        adventure_context
        if state.get("manifest_status") == "reviewed"
        else adventure_context[:_RAG_BLOCK_TOP_K]
    )


def _live_consequence(public: dict) -> dict:
    """A validated visit proves position, not every actor assumption in arrival prose."""
    if public.get("provenance") != "module" or public.get("kind") not in {"move", "visit"}:
        return public
    return {
        **{key: value for key, value in public.items() if key != "text"},
        "scope": "Place visit only; authored arrival staging is conditional reference context.",
    }


def build_narrator_prompt(state: AgentState, profile: str = "play.narrator") -> list[BaseMessage]:
    player_input = state.get("player_input", "")
    adjudication = state.get("adjudication_result")
    edition = state.get("rules_edition", "2014")
    rules_context, adventure_context = narrator_context(state)
    world_flags = state.get("world_flags", {})
    current_scene_id = state.get("current_scene_id")

    context_parts = [f"Player's attempted action: {wrap_player_action(player_input)}"]
    context_parts.append(
        "Narration mode: resolved combat round. Use 1-3 focused paragraphs; describe only "
        "the recorded actions and their results, then leave the next decision to the player."
        if profile == "play.combat_narrator"
        else "Narration mode: exploration or conversation. Give the response and its grounded "
        "scene detail room to breathe, while keeping routine exchanges brief."
    )
    boundary = boundary_prompt_block(actions_for_state(state))
    if boundary:
        context_parts.append(boundary)
    if state.get("source_mode") == "homebrew":
        context_parts.append(
            "The player explicitly chose homebrew play. Continue creatively from saved story state; do not claim to follow an unavailable adventure module."
        )
    continuity_slot = len(context_parts)
    character = state.get("character")
    if character:
        context_parts.append(
            f"Player character: {character.get('name')}, level {character.get('level')} "
            f"{character.get('class')}, HP {character.get('hp_current')}/{character.get('hp_max')}, "
            f"inventory {character.get('inventory')}. Use HP only to calibrate wounds and fatigue "
            f"(do not describe the PC as unhurt if HP is low, or dying if HP is still moderate); "
            "never print the numbers, and do not invent status conditions. "
            "This inventory is authoritative for carried possessions. A spoken claim of "
            "being unarmed does not remove carried equipment. Do not narrate loss, transfer, "
            "consumption or expiry from a new scene's default staging; only a recorded "
            "outcome can establish that change, and make any established change explicit."
        )
    party = [m for m in (state.get("party") or []) if isinstance(m, dict) and m.get("name")]
    if len(party) > 1:
        # Multi-PC table (EDG-046): the roster is shared context; only the acting PC's action is resolved.
        roster = "; ".join(f"{m['name']} ({m.get('class')}, level {m.get('level')})" for m in party)
        context_parts.append(
            f"Party at the table: {roster}. Narrate for the whole party. The acting player character "
            f"is {character.get('name') if character else 'the spotlight character'}; do not resolve "
            "actions for the other party members."
        )
    npcs = [n for n in (state.get("npcs") or []) if isinstance(n, dict) and n.get("name")]
    if npcs:
        context_parts.append(
            "Known NPCs (name: disposition toward the player): "
            + "; ".join(f"{n['name']}: {n.get('disposition') or 'unknown'}" for n in npcs[:15])
        )
    scene = resolved_scene(state)
    if scene:
        context_parts.append(
            "Resolved campaign position (authoritative for this response): "
            f"place={scene['label']}; "
            f"accepted transition={'yes' if scene['moved'] else 'none'}. "
            "Only an accepted transition changes the location. Stop at this location even if "
            "the player's intended journey goes farther; describe a concrete next approach. "
            "Do not narrate arrival, entry, an accompanying actor, or a later destination "
            "that the validated outcome has not established. An event transition is not itself "
            "permission to move the player physically: preserve an explicitly stationary "
            "position, and describe contact across a doorway or at a distance when appropriate."
        )
        if scene["visible_description"]:
            context_parts.append(
                "Already public surroundings, available even when an optional check fails: "
                + str(scene["visible_description"])
                + ". Give the visible information needed for the next decision. A failed search "
                "only withholds the hidden detail it tested; it does not erase these surroundings. "
                "These excerpts are authored opening conditions, not a live snapshot. Reuse only "
                "details compatible with the resolved action and established continuity. Do not "
                "replay default actor presence, possession or behavior after this exchange has "
                "changed it; never restore a departed actor to satisfy opening prose. Do not "
                "claim interior details are visible from an exterior position."
            )
    elif current_scene_id:
        context_parts.append(
            f"Authoritative campaign position (do not contradict or skip ahead): scene={current_scene_id}"
        )
    if world_flags and state.get("manifest_status") != "reviewed":
        context_parts.append(f"World flags (persisted state): {world_flags}")
    earned = [_live_consequence(fact) for fact in public_earned_facts(state)]
    if earned:
        context_parts.append(f"Committed player-known story facts: {earned}")
    if adjudication:
        if adjudication.accepted_observations:
            context_parts.append(
                "Validated source observations and ordinary answers earned by this action "
                "(convey their concrete information naturally, including responsive NPC dialogue; "
                "these are approved answers, not private notes to recite): "
                + str(adjudication.accepted_observations)
            )
        accepted = [
            _live_consequence(public)
            for effect in adjudication.accepted_story_effects
            if (public := player_story_effect(effect)) is not None
        ]
        context_parts.append(
            f"Validated canonical story consequences this turn (persistent discoveries/accomplishments; validated source observations also answer this action): {accepted}"
        )
        if state.get("source_mode") == "homebrew" and accepted:
            context_parts.append(
                "Homebrew play: the validated consequences above are the only new named people, "
                "places and clues this turn. Use their names exactly as given and invent nothing else new."
            )
        context_parts.append(f"Engine outcome (final): {adjudication.mechanical_summary}")
        if adjudication.check and adjudication.dc is not None:
            verdict = "success" if adjudication.success else "failure"
            context_parts.append(
                f"Check: {adjudication.check} {adjudication.roll_expression} = "
                f"{adjudication.dice_result} vs {adjudication.against or 'DC'} {adjudication.dc} "
                f"-> {verdict}"
            )
        elif adjudication.dice_result is not None:
            context_parts.append(f"Dice: {adjudication.dice_result}")
    resolved = [
        {"id": outcome.get("id"), "outcome": outcome.get("outcome"), "round": outcome.get("round")}
        for outcome in state.get("resolved_combat_outcomes", [])
    ]
    if resolved:
        context_parts.append(
            f"Recorded outcomes of combats already fought (never reopen or contradict): {resolved}"
        )
    combat = state.get("combat_state") or {}
    if combat.get("round_events"):
        context_parts.append(
            "Chronological engine events this resolved round (preserve this order; enemy_attack "
            "is the foe's ordinary turn, and only opportunity_attack is an opportunity attack):\n"
            + "\n".join(
                f"{index}. [{event.get('kind')}] {event.get('text')}"
                for index, event in enumerate(combat["round_events"], 1)
            )
        )
    if combat.get("initiative"):
        order = ", ".join(
            f"{c.get('name')}{' ' + c['disposition'] if c.get('disposition') not in (None, 'active') else ''}"
            for c in combat["initiative"]
        )
        context_parts.append(f"Initiative order this round: {order}")
        if combat.get("combatants"):
            context_parts.append(
                narrator_roster(combat["combatants"], combat.get("round_actors") or [])
            )
            if turn_economy := economy_text(combat["combatants"]):
                context_parts.append(turn_economy)
    context_parts.append(
        f"Selected rules edition: {edition}. Reference labels are provenance; do not recite hashes or private IDs."
    )
    if rules_context:
        block = "\n".join(wrap_retrieved_excerpt(cited_excerpt(c, edition)) for c in rules_context)
        context_parts.append(f"Official rules context:\n{block}")
    # Exact-scene opening excerpts already appear once in the conditional baseline
    # above. Repeating them as module evidence gives stale staging undue weight.
    remaining_adventure_context = [
        chunk for chunk in adventure_context
        if not scene or chunk not in scene["public_context"]
    ]
    if remaining_adventure_context:
        block = "\n".join(
            wrap_retrieved_excerpt(cited_excerpt(c, edition))
            for c in remaining_adventure_context
        )
        context_parts.append(f"Adventure module context (story/locations/NPCs):\n{block}")
    if any(getattr(message, "type", None) == "ai" for message in state.get("messages") or []):
        context_parts.append(
            "Most recent established public scene continuity: use the newest DM narration "
            "retained in the budgeted conversation above. Preserve its resolved actor presence, "
            "departures and custody unless newer engine "
            "outcomes or validated live consequences change them. A place visit and its authored "
            "opening excerpt do not reset these facts. Resolve the current action from here."
        )

    if continuity := fit_continuity(state, profile, [NARRATOR, *context_parts]):
        context_parts.insert(continuity_slot, continuity)
    user_msg = HumanMessage(content="\n\n".join(context_parts))
    return [SystemMessage(content=NARRATOR), user_msg]


async def narrator_node(state: AgentState) -> dict:
    """Non-streaming narrator used by the linear graph and the combat subgraph.

    The SSE endpoint bypasses this node and streams tokens directly via `build_narrator_prompt`
    + `llm.astream`, which is why prompt construction lives in a free function above.
    """
    messages = state.get("messages", [])

    narration = deterministic_narration(state)
    if narration is None:
        profile = narration_profile(state)
        llm, prompt = narrator_request(state, profile)
        try:
            response = await traced_call(
                lambda: llm.ainvoke(prompt), kind="narrate", profile=profile
            )
        except Exception as e:
            return {
                "narration": "",
                "error": NARRATION_FAILED_MESSAGE,
                "error_code": provider_error_code(e),
            }
        narration = apply_narration_boundary(
            message_text(response),
            actions_for_state(state),
            applied=engine_applied_for_state(state),
        )

    updated_messages = list(messages)
    updated_messages.append(AIMessage(content=narration))
    return {"narration": narration, "messages": updated_messages}
