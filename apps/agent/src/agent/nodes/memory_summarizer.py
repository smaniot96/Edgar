"""Compress long sessions so the narrator's prompt stays small.

Once `messages` reaches MEMORY_THRESHOLD turns, the middle is summarised into one AIMessage
("Story so far: ...") and the head + tail are kept verbatim. The narrator only ever sees the
last few messages anyway; this is here to preserve early-game context across many turns.
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.state import AgentState

MEMORY_THRESHOLD = 10
KEEP_FIRST = 2
KEEP_LAST = 4
MEMORY_SUMMARY_PROMPT = (
    'Summarize the following conversation turns into a short "story so far" (2-3 sentences). '
    "Preserve key events, NPCs, and player decisions."
)


async def memory_summarizer_node(state: AgentState) -> dict:
    messages = state.get("messages", [])
    if len(messages) < MEMORY_THRESHOLD:
        return {}

    to_summarize = messages[KEEP_FIRST:-KEEP_LAST]
    if len(to_summarize) < 2:
        return {}

    llm = make_chat_model(temperature=0)
    summary_input = "\n".join(
        f"{m.type}: {m.content}" if hasattr(m, "content") else str(m) for m in to_summarize
    )
    summary = await llm.ainvoke(
        [
            SystemMessage(content=MEMORY_SUMMARY_PROMPT),
            HumanMessage(content=summary_input),
        ]
    )
    summary_content = summary.content if hasattr(summary, "content") else str(summary)

    new_messages = (
        list(messages[:KEEP_FIRST])
        + [AIMessage(content=f"[Story so far: {summary_content}]")]
        + list(messages[-KEEP_LAST:])
    )
    return {"messages": new_messages}
