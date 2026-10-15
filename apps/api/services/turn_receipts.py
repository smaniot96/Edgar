"""Read the existing atomic narration receipt; no alternate consequence store."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import EventLog

from ..schemas.turn import TurnResponse


async def load_turn_receipt(
    db: AsyncSession, session_id: int, request_id: str
) -> tuple[str, TurnResponse] | None:
    row = await db.scalar(
        select(EventLog).where(
            EventLog.session_id == session_id,
            EventLog.event_type == "narration",
            EventLog.payload["turn_id"].astext == request_id,
        )
    )
    if row is None or not isinstance((row.payload or {}).get("done"), dict):
        return None
    # Response schema is the same allowlist used by the synchronous endpoint.
    return str(row.payload.get("player_input", "")), TurnResponse.model_validate(row.payload["done"])
