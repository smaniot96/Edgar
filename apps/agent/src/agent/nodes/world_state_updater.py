"""WorldStateUpdater node: persist changes to PostgreSQL."""

import asyncio
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[5]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from db.postgres import async_session_factory
from db.postgres.models import EventLog

from agent.state import AgentState


def _run_async(coro):
    """Run async code from sync node."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


async def _update_state(state: AgentState) -> dict:
    """Async logic."""
    session_id = state.get("session_id")
    adjudication = state.get("adjudication_result")
    if not session_id or not adjudication:
        return {}

    async with async_session_factory() as session:
        event = EventLog(
            session_id=session_id,
            event_type="adjudication",
            payload=adjudication.model_dump(),
        )
        session.add(event)
        await session.commit()
    return {}


def world_state_updater_node(state: AgentState) -> dict:
    """Apply adjudication_result to PostgreSQL."""
    try:
        return _run_async(_update_state(state))
    except Exception as e:
        return {"error": str(e)}
