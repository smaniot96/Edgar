"""MemorySummarizer node: compress long-term context."""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[5]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
import config  # noqa: F401, E402

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.state import AgentState

MEMORY_THRESHOLD = 10
MEMORY_SUMMARY_PROMPT = """Summarize the following conversation turns into a short "story so far" (2-3 sentences). Preserve key events, NPCs, and player decisions."""


def memory_summarizer_node(state: AgentState) -> dict:
    """Summarize old turns if history exceeds threshold."""
    messages = state.get("messages", [])

    if len(messages) < MEMORY_THRESHOLD:
        return {}

    # Keep first 2 and last 4, summarize the middle
    keep_first = 2
    keep_last = 4
    to_summarize = messages[keep_first:-keep_last]
    if len(to_summarize) < 2:
        return {}

    llm = ChatOpenAI(model="gpt-5-mini", temperature=0, api_key=config.OPENAI_API_KEY)
    summary_input = "\n".join(
        f"{m.type}: {m.content}" if hasattr(m, "content") else str(m)
        for m in to_summarize
    )
    summary = llm.invoke([
        SystemMessage(content=MEMORY_SUMMARY_PROMPT),
        HumanMessage(content=summary_input),
    ])
    summary_content = summary.content if hasattr(summary, "content") else str(summary)

    new_messages = (
        list(messages[:keep_first])
        + [AIMessage(content=f"[Story so far: {summary_content}]")]
        + list(messages[-keep_last:])
    )
    return {"messages": new_messages}
