"""Append a telemetry row to event_log for the adjudication result.

Source-of-truth writes (HP delta, world flags, scene changes, inventory) live in
`apps.api.services.world_writes.apply_adjudication`, which uses the API's transaction. This
node opens its own short-lived session because LangGraph nodes do not have a transaction
handle, and writing telemetry alongside the API write keeps both observable in event_log even
if the request is cancelled mid-flight.
"""

from db.postgres.models import EventLog

from agent.state import AgentState


async def world_state_updater_node(state: AgentState) -> dict:
    from db.postgres import session as db_session_module

    session_id = state.get("session_id")
    adjudication = state.get("adjudication_result")
    if not session_id or not adjudication:
        return {}

    try:
        async with db_session_module.async_session_factory() as session:
            session.add(
                EventLog(
                    session_id=session_id,
                    event_type="adjudication",
                    payload=adjudication.model_dump(),
                )
            )
            await session.commit()
    except Exception as e:
        return {"error": str(e)}
    return {}
