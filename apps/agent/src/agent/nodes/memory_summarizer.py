"""Rolling "story so far" summary so early-game context survives long sessions.

The narrator sees the last few turns verbatim (see `services.session_messages`). Turns that
fall out of that window are folded into one persisted summary by the API *after* a turn
commits (`services.session_messages.maybe_update_memory_summary`), so this LLM call is never
on the critical path of a turn. The summary is fed back to the agent as `state["memory_summary"]`.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import make_chat_model, message_text

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
    llm = make_chat_model(profile="play.memory_summarizer", temperature=0)
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
    return message_text(summary).strip()
