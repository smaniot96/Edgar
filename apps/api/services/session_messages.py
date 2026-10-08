"""Load LangChain messages from event_log for turn continuity, plus the rolling memory summary.

The narrator sees the last RECENT_TURNS turns verbatim. Turns older than that are folded into a
persisted "story so far" summary (an event_log row with event_type=MEMORY_SUMMARY_EVENT whose
payload records which narration row it covers up to), updated off the critical path after a
turn commits and only once SUMMARY_EVERY_N_TURNS new turns have aged out of the window.
"""

import structlog
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db.postgres.session as db_session_module
from agent.nodes.memory_summarizer import summarize_story
from db.postgres.models import EventLog

log = structlog.get_logger()

RECENT_TURNS = 10
SUMMARY_EVERY_N_TURNS = 5
MEMORY_SUMMARY_EVENT = "memory_summary"


def _events_to_messages(events: list[EventLog]) -> list[HumanMessage | AIMessage]:
    messages: list[HumanMessage | AIMessage] = []
    for e in events:
        payload = e.payload or {}
        player_input = payload.get("player_input")
        narration = payload.get("narration")
        if player_input:
            messages.append(HumanMessage(content=str(player_input)))
        if narration:
            messages.append(AIMessage(content=str(narration)))
    return messages


async def load_session_messages(
    db: AsyncSession,
    session_id: int,
    *,
    limit_turns: int = RECENT_TURNS,
) -> list[HumanMessage | AIMessage]:
    """Load last N narration turns (player_input + narration pairs) from event_log, oldest first."""
    result = await db.execute(
        select(EventLog)
        .where(
            EventLog.session_id == session_id,
            EventLog.event_type == "narration",
        )
        .order_by(EventLog.created_at.desc(), EventLog.id.desc())
        .limit(limit_turns)
    )
    return _events_to_messages(list(reversed(result.scalars().all())))


async def _latest_summary_row(db: AsyncSession, session_id: int) -> EventLog | None:
    result = await db.execute(
        select(EventLog)
        .where(
            EventLog.session_id == session_id,
            EventLog.event_type == MEMORY_SUMMARY_EVENT,
        )
        .order_by(EventLog.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_memory_summary(db: AsyncSession, session_id: int) -> str | None:
    """The latest persisted "story so far" for this session, or None if there is none yet."""
    row = await _latest_summary_row(db, session_id)
    if row is None:
        return None
    summary = (row.payload or {}).get("summary")
    return str(summary) if summary else None


async def maybe_update_memory_summary(session_id: int) -> None:
    """Fold turns that aged out of the verbatim window into the rolling summary.

    Runs after the turn has committed (as a response background task) in its own short-lived
    DB session. Never raises: a failed summary only means the narrator keeps the previous one.
    """
    try:
        async with db_session_module.async_session_factory() as db:
            latest = await _latest_summary_row(db, session_id)
            previous = (latest.payload or {}) if latest is not None else {}
            through_id = int(previous.get("through_event_id") or 0)

            result = await db.execute(
                select(EventLog)
                .where(
                    EventLog.session_id == session_id,
                    EventLog.event_type == "narration",
                    EventLog.id > through_id,
                )
                .order_by(EventLog.id.asc())
            )
            pending = list(result.scalars().all())
            # The newest RECENT_TURNS stay verbatim in the prompt; only older turns are summarised.
            aged_out = pending[:-RECENT_TURNS] if len(pending) > RECENT_TURNS else []
            if len(aged_out) < SUMMARY_EVERY_N_TURNS:
                return

            summary = await summarize_story(previous.get("summary"), _events_to_messages(aged_out))
            if not summary:
                return
            db.add(
                EventLog(
                    session_id=session_id,
                    event_type=MEMORY_SUMMARY_EVENT,
                    payload={
                        "summary": summary,
                        "through_event_id": aged_out[-1].id,
                        "turns_summarized": len(aged_out),
                    },
                )
            )
            await db.commit()
            log.info("memory_summary_updated", session_id=session_id, turns=len(aged_out))
    except Exception:
        log.warning("memory_summary_failed", session_id=session_id, exc_info=True)
