"""Narrator node: produce immersive narration from adjudicated outcome."""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[5]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config  # noqa: F401, E402

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.state import AgentState
from agent.prompts import NARRATOR


def narrator_node(state: AgentState) -> dict:
    """Generate narration from adjudication and lore."""
    player_input = state.get("player_input", "")
    adjudication = state.get("adjudication_result")
    retrieved_context = state.get("retrieved_context", [])
    messages = state.get("messages", [])

    context_parts = [f"Player said: {player_input}"]
    if adjudication:
        context_parts.append(f"Outcome: {adjudication.mechanical_summary}")
        if adjudication.dice_result is not None:
            context_parts.append(f"Dice: {adjudication.dice_result}")
    if retrieved_context:
        lore = "\n".join(c.get("text", "") for c in retrieved_context[:5])
        context_parts.append(f"Relevant lore:\n{lore}")

    llm = ChatOpenAI(model="gpt-5-mini", temperature=0.7, api_key=config.OPENAI_API_KEY)
    prompt = [
        SystemMessage(content=NARRATOR),
        HumanMessage(content="\n\n".join(context_parts)),
    ]
    if messages:
        prompt = [SystemMessage(content=NARRATOR)] + list(messages)[-4:] + [HumanMessage(content="\n\n".join(context_parts))]

    try:
        response = llm.invoke(prompt)
        narration = response.content if hasattr(response, "content") else str(response)
        updated_messages = list(messages)
        updated_messages.append(AIMessage(content=narration))
        return {"narration": narration, "messages": updated_messages}
    except Exception as e:
        return {"narration": "", "error": str(e)}
