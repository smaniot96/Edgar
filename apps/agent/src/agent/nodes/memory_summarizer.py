"""Rolling "story so far" summary so early-game context survives long sessions.

The narrator sees the last few turns verbatim (see `services.session_messages`). Turns that
fall out of that window are folded into one persisted summary by the API *after* a turn
commits (`services.session_messages.maybe_update_memory_summary`), so this LLM call is never
on the critical path of a turn. The summary is fed back to the agent as `state["memory_summary"]`.
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import make_chat_model
from agent.state import AgentState

log = logging.getLogger(__name__)

MEMORY_SUMMARY_PROMPT = (
    'Update the "story so far" for a solo D&D adventure. You get the previous summary (may be '
    "empty) and the turns that happened after it. Return a single updated summary of at most "
    "6 sentences. Preserve key events, NPCs, places, items, and the player's decisions; drop "
    "moment-to-moment detail."
)


def _render_turns(messages: list) -> str:
    return "\n".join(
        f"{m.type}: {m.content}" if hasattr(m, "content") else str(m) for m in messages
    )


async def summarize_story(previous_summary: str | None, messages: list) -> str:
    """Fold `messages` into `previous_summary`. Raises on LLM failure; callers decide policy."""
    llm = make_chat_model(temperature=0)
    summary = await llm.ainvoke(
        [
            SystemMessage(content=MEMORY_SUMMARY_PROMPT),
            HumanMessage(
                content=(
                    f"Previous summary:\n{previous_summary or '(none)'}\n\n"
                    f"New turns:\n{_render_turns(messages)}"
                )
            ),
        ]
    )
    content = summary.content if hasattr(summary, "content") else str(summary)
    return str(content).strip()


async def memory_summarizer_node(state: AgentState) -> dict:
    """Graph-node wrapper (not wired into the turn graph): fold `messages` into
    `memory_summary`. Failures are logged and leave the existing summary untouched."""
    messages = state.get("messages") or []
    if not messages:
        return {}
    try:
        summary = await summarize_story(state.get("memory_summary"), messages)
    except Exception:
        log.warning("memory_summary_failed", exc_info=True)
        return {}
    return {"memory_summary": summary}
