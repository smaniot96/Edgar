"""MemorySummarizer node: compress long-term context."""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from agent.llm import make_chat_model
from agent.state import AgentState

MEMORY_THRESHOLD = 10
MEMORY_SUMMARY_PROMPT = """Summarize the following conversation turns into a short "story so far" (2-3 sentences). Preserve key events, NPCs, and player decisions."""


async def memory_summarizer_node(state: AgentState) -> dict:
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

    llm = make_chat_model(temperature=0)
    summary_input = "\n".join(
        f"{m.type}: {m.content}" if hasattr(m, "content") else str(m)
        for m in to_summarize
    )
    summary = await llm.ainvoke([
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
