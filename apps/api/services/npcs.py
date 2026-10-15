"""NPC data helpers — load campaign NPCs for agent context injection."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import NPC


async def load_npcs(db: AsyncSession, campaign_id: int, session_id: int | None = None) -> list[NPC]:
    """Return all NPCs for a campaign (lightweight; used by the turn state loader)."""
    result = await db.execute(
        select(NPC).where(
            NPC.campaign_id == campaign_id,
            or_(NPC.session_id.is_(None), NPC.session_id == session_id),
        )
    )
    return list(result.scalars().all())
