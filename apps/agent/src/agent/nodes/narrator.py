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
  - the last few messages so the prose stays continuous

The player's text is wrapped in <player_action> tags (untrusted input). Output length is bounded
by `NARRATOR_MAX_TOKENS`; the prompt forbids HP/status blocks since the UI renders state.
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.llm import NARRATOR_MAX_TOKENS, make_chat_model, message_text
from agent.prompts import NARRATOR, wrap_player_action
from agent.state import AgentState

_NARRATOR_TEMPERATURE = 0.7
_HISTORY_TAIL = 4
_RAG_BLOCK_TOP_K = 5


def make_narrator_model():
    """Narrator chat model with a bounded `max_tokens`.

    Falls back to the plain factory signature for test fakes that only accept `temperature`.
    """
    try:
        return make_chat_model(temperature=_NARRATOR_TEMPERATURE, max_tokens=NARRATOR_MAX_TOKENS)
    except TypeError:
        return make_chat_model(temperature=_NARRATOR_TEMPERATURE)


def build_narrator_prompt(state: AgentState) -> list[BaseMessage]:
    player_input = state.get("player_input", "")
    adjudication = state.get("adjudication_result")

    rules_context = state.get("rules_context", [])
    adventure_context = state.get("adventure_context", [])
    if not rules_context and not adventure_context:
        merged = state.get("retrieved_context", [])
        rules_context = [c for c in merged if c.get("kind") == "rules"]
        adventure_context = [c for c in merged if c.get("kind") == "adventure"]

    world_flags = state.get("world_flags", {})
    current_scene_id = state.get("current_scene_id")
    messages = state.get("messages", [])

    context_parts = [f"Player's attempted action: {wrap_player_action(player_input)}"]
    summary = state.get("memory_summary")
    if summary:
        context_parts.append(f"Story so far: {summary}")
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
    if current_scene_id:
        context_parts.append(
            f"Authoritative campaign position (do not contradict or skip ahead): scene={current_scene_id}"
        )
    if world_flags:
        context_parts.append(f"World flags (persisted state): {world_flags}")
    if adjudication:
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
    combat = state.get("combat_state") or {}
    if combat.get("initiative"):
        order = ", ".join(
            f"{c.get('name')}{' (fallen)' if not c.get('alive', True) else ''}"
            for c in combat["initiative"]
        )
        context_parts.append(f"Initiative order this round: {order}")
    if rules_context:
        block = "\n".join(c.get("text", "") for c in rules_context[:_RAG_BLOCK_TOP_K])
        context_parts.append(f"Official rules context:\n{block}")
    if adventure_context:
        block = "\n".join(c.get("text", "") for c in adventure_context[:_RAG_BLOCK_TOP_K])
        context_parts.append(f"Adventure module context (story/locations/NPCs):\n{block}")

    user_msg = HumanMessage(content="\n\n".join(context_parts))
    if messages:
        return [SystemMessage(content=NARRATOR), *list(messages)[-_HISTORY_TAIL:], user_msg]
    return [SystemMessage(content=NARRATOR), user_msg]


async def narrator_node(state: AgentState) -> dict:
    """Non-streaming narrator used by the linear graph and the combat subgraph.

    The SSE endpoint bypasses this node and streams tokens directly via `build_narrator_prompt`
    + `llm.astream`, which is why prompt construction lives in a free function above.
    """
    llm = make_narrator_model()
    prompt = build_narrator_prompt(state)
    messages = state.get("messages", [])

    try:
        response = await llm.ainvoke(prompt)
    except Exception as e:
        return {"narration": "", "error": str(e)}

    narration = message_text(response)
    updated_messages = list(messages)
    updated_messages.append(AIMessage(content=narration))
    return {"narration": narration, "messages": updated_messages}
