"""Generate the player-facing narration from the adjudicated outcome.

`build_narrator_prompt` is exported separately so the SSE turn runner can stream tokens with
`llm.astream` while still using the same context-building logic. The narrator gets:
  - the player's literal message
  - the active character's HP/stats so it does not describe a fresh PC as bleeding
  - the authoritative scene id so it does not skip ahead to a different location
  - the latest world flags so it cannot un-unlock a door
  - the adjudication outcome (mechanical_summary, dice_result)
  - rules context (for mechanical fidelity) and adventure context (for story flavour)
  - the story-so-far summary (`memory_summary`) and NPC names/dispositions
  - every exchange after the summary boundary so the prose stays continuous

The player's text is wrapped in <player_action> tags (untrusted input). Output length is bounded
by `NARRATOR_MAX_TOKENS`; the prompt forbids HP/status blocks since the UI renders state.
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.combatants import narrator_roster
from agent.history import continuity_context, public_earned_facts
from agent.llm import NARRATOR_MAX_TOKENS, make_chat_model, message_text
from agent.prompts import NARRATOR, wrap_player_action
from agent.state import AgentState
from agent.supported_rules import (
    actions_for_state,
    apply_narration_boundary,
    boundary_prompt_block,
    executed_spell_for_state,
)
from db.vector.retrieval import rule_policy
from edgar_core.sources import cited_excerpt, eligible_context
from edgar_core.story import player_story_effect

_NARRATOR_TEMPERATURE = 0.7
_RAG_BLOCK_TOP_K = 5


def deterministic_narration(state: AgentState) -> str | None:
    """Shared sync/SSE rendering for exact quotations and sealed policy outcomes."""
    recall = state.get("exact_recall")
    if recall:
        page = f", p. {recall['page']}" if recall.get("page") else ""
        return f"From {recall['source']}{page}:\n\n{recall['text']}"
    if state.get("policy_rejection"):
        return "That approach is not available in the current circumstances."
    return state.get("policy_narration")


def make_narrator_model():
    """Narrator chat model with a bounded `max_tokens`.

    Falls back to the plain factory signature for test fakes that only accept `temperature`.
    """
    try:
        return make_chat_model(temperature=_NARRATOR_TEMPERATURE, max_tokens=NARRATOR_MAX_TOKENS)
    except TypeError:
        return make_chat_model(temperature=_NARRATOR_TEMPERATURE)


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
    return rules_context[:_RAG_BLOCK_TOP_K], (
        adventure_context
        if state.get("manifest_status") == "reviewed"
        else adventure_context[:_RAG_BLOCK_TOP_K]
    )


def build_narrator_prompt(state: AgentState) -> list[BaseMessage]:
    player_input = state.get("player_input", "")
    adjudication = state.get("adjudication_result")
    edition = state.get("rules_edition", "2014")
    rules_context, adventure_context = narrator_context(state)
    world_flags = state.get("world_flags", {})
    current_scene_id = state.get("current_scene_id")

    context_parts = [f"Player's attempted action: {wrap_player_action(player_input)}"]
    boundary = boundary_prompt_block(actions_for_state(state))
    if boundary:
        context_parts.append(boundary)
    if state.get("source_mode") == "homebrew":
        context_parts.append(
            "The player explicitly chose homebrew play. Continue creatively from saved story state; do not claim to follow an unavailable adventure module."
        )
    continuity = continuity_context(state)
    if continuity:
        context_parts.append(continuity)
    character = state.get("character")
    if character:
        context_parts.append(
            f"Player character: {character.get('name')}, level {character.get('level')} "
            f"{character.get('class')}, HP {character.get('hp_current')}/{character.get('hp_max')}, "
            f"inventory {character.get('inventory')}. Use HP only to calibrate wounds and fatigue "
            f"(do not describe the PC as unhurt if HP is low, or dying if HP is still moderate); "
            f"never print the numbers, and do not invent status conditions."
        )
    npcs = [n for n in (state.get("npcs") or []) if isinstance(n, dict) and n.get("name")]
    if npcs:
        context_parts.append(
            "Known NPCs (name: disposition toward the player): "
            + "; ".join(f"{n['name']}: {n.get('disposition') or 'unknown'}" for n in npcs[:15])
        )
    if current_scene_id and state.get("manifest_status") != "reviewed":
        context_parts.append(
            f"Authoritative campaign position (do not contradict or skip ahead): scene={current_scene_id}"
        )
    if world_flags and state.get("manifest_status") != "reviewed":
        context_parts.append(f"World flags (persisted state): {world_flags}")
    earned = public_earned_facts(state)
    if earned:
        context_parts.append(f"Committed player-known story facts: {earned}")
    if adjudication:
        accepted = [
            public
            for effect in adjudication.accepted_story_effects
            if (public := player_story_effect(effect)) is not None
        ]
        context_parts.append(
            f"Validated story consequences this turn (describe only these as new discoveries or accomplishments): {accepted}"
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
    context_parts.append(
        f"Selected rules edition: {edition}. Reference labels are provenance; do not recite hashes or private IDs."
    )
    if rules_context:
        block = "\n".join(cited_excerpt(c, edition) for c in rules_context)
        context_parts.append(f"Official rules context:\n{block}")
    if adventure_context:
        block = "\n".join(cited_excerpt(c, edition) for c in adventure_context)
        context_parts.append(f"Adventure module context (story/locations/NPCs):\n{block}")

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
        llm = make_narrator_model()
        prompt = build_narrator_prompt(state)
        try:
            response = await llm.ainvoke(prompt)
        except Exception as e:
            return {"narration": "", "error": str(e)}
        narration = apply_narration_boundary(
            message_text(response),
            actions_for_state(state),
            executed_spell=executed_spell_for_state(state),
        )

    updated_messages = list(messages)
    updated_messages.append(AIMessage(content=narration))
    return {"narration": narration, "messages": updated_messages}
