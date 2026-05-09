"""Generate the player-facing narration from the adjudicated outcome.

`build_narrator_prompt` is exported separately so the SSE turn runner can stream tokens with
`llm.astream` while still using the same context-building logic. The narrator gets:
  - the player's literal message
  - the active character's HP/stats so it does not describe a fresh PC as bleeding
  - the authoritative scene id so it does not skip ahead to a different location
  - the latest world flags so it cannot un-unlock a door
  - the adjudication outcome (mechanical_summary, dice_result)
  - rules context (for mechanical fidelity) and adventure context (for story flavour)
  - the last few messages so the prose stays continuous
"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.prompts import NARRATOR
from agent.state import AgentState

_NARRATOR_TEMPERATURE = 0.7
_HISTORY_TAIL = 4
_RAG_BLOCK_TOP_K = 5


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

    context_parts = [f"Player said: {player_input}"]
    character = state.get("character")
    if character:
        context_parts.append(
            f"Player character: {character['name']}, level {character['level']} {character['class']}, "
            f"HP {character['hp_current']}/{character['hp_max']}, stats {character['stats']}, "
            f"inventory {character.get('inventory')}. Calibrate wounds and fatigue to current HP "
            f"(do not describe the PC as unhurt if HP is low, or dying if HP is still moderate); "
            f"do not invent status conditions that contradict these numbers."
        )
    if current_scene_id:
        context_parts.append(
            f"Authoritative campaign position (do not contradict or skip ahead): scene={current_scene_id}"
        )
    if world_flags:
        context_parts.append(f"World flags (persisted state): {world_flags}")
    if adjudication:
        context_parts.append(f"Outcome: {adjudication.mechanical_summary}")
        if adjudication.dice_result is not None:
            context_parts.append(f"Dice: {adjudication.dice_result}")
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
    llm = make_chat_model(temperature=_NARRATOR_TEMPERATURE)
    prompt = build_narrator_prompt(state)
    messages = state.get("messages", [])

    try:
        response = await llm.ainvoke(prompt)
    except Exception as e:
        return {"narration": "", "error": str(e)}

    raw = response.content if hasattr(response, "content") else str(response)
    narration = raw if isinstance(raw, str) else str(raw)
    updated_messages = list(messages)
    updated_messages.append(AIMessage(content=narration))
    return {"narration": narration, "messages": updated_messages}
