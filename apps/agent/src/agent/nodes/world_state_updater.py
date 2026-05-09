"""WorldStateUpdater node: persist changes to PostgreSQL."""

from db.postgres import async_session_factory
from db.postgres.models import EventLog

from agent.state import AgentState


async def world_state_updater_node(state: AgentState) -> dict:
    """Telemetry-only EventLog row for adjudication (see services.world_writes in API)."""
    session_id = state.get("session_id")
    adjudication = state.get("adjudication_result")
    if not session_id or not adjudication:
        return {}

    try:
        async with async_session_factory() as session:
            event = EventLog(
                session_id=session_id,
                event_type="adjudication",
                payload=adjudication.model_dump(),
            )
            session.add(event)
            await session.commit()
    except Exception as e:
        return {"error": str(e)}
    return {}
