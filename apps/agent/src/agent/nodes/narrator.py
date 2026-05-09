"""Narrator node: produce immersive narration from adjudicated outcome."""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.state import AgentState
from agent.prompts import NARRATOR


def build_narrator_prompt(state: AgentState) -> list[BaseMessage]:
    """Messages for the narrator LLM (shared by the graph node and SSE streaming)."""
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
        inv = character.get("inventory")
        context_parts.append(
            f"Player character: {character['name']}, level {character['level']} {character['class']}, "
            f"HP {character['hp_current']}/{character['hp_max']}, stats {character['stats']}, "
            f"inventory {inv}. Calibrate wounds and fatigue to current HP (do not describe the PC as "
            f"unhurt if HP is low, or dying if HP is still moderate); do not invent status conditions "
            f"that contradict these numbers."
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
        block = "\n".join(c.get("text", "") for c in rules_context[:5])
        context_parts.append(f"Official rules context:\n{block}")
    if adventure_context:
        block = "\n".join(c.get("text", "") for c in adventure_context[:5])
        context_parts.append(f"Adventure module context (story/locations/NPCs):\n{block}")

    prompt: list[BaseMessage] = [
        SystemMessage(content=NARRATOR),
        HumanMessage(content="\n\n".join(context_parts)),
    ]
    if messages:
        prompt = [SystemMessage(content=NARRATOR)] + list(messages)[-4:] + [
            HumanMessage(content="\n\n".join(context_parts))
        ]
    return prompt


async def narrator_node(state: AgentState) -> dict:
    """Generate narration from adjudication and lore."""
    llm = make_chat_model(temperature=0.7)
    prompt = build_narrator_prompt(state)
    messages = state.get("messages", [])

    try:
        response = await llm.ainvoke(prompt)
        raw = response.content if hasattr(response, "content") else str(response)
        narration = raw if isinstance(raw, str) else str(raw)
        updated_messages = list(messages)
        updated_messages.append(AIMessage(content=narration))
        return {"narration": narration, "messages": updated_messages}
    except Exception as e:
        return {"narration": "", "error": str(e)}
