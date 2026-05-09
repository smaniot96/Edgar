"""Load LangChain messages from event_log for turn continuity."""

from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import EventLog


async def load_session_messages(
    db: AsyncSession,
    session_id: int,
    *,
    limit_turns: int = 10,
) -> list[HumanMessage | AIMessage]:
    """Load last N narration turns (player_input + narration pairs) from event_log, oldest first."""
    result = await db.execute(
        select(EventLog)
        .where(
            EventLog.session_id == session_id,
            EventLog.event_type == "narration",
        )
        .order_by(EventLog.created_at.desc())
        .limit(limit_turns)
    )
    events = list(reversed(result.scalars().all()))

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
