"""Load world_flags for a campaign as a dict."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import WorldFlag


async def load_world_flags(db: AsyncSession, campaign_id: int) -> dict[str, str]:
    """Return key -> value for all flags for this campaign."""
    result = await db.execute(select(WorldFlag).where(WorldFlag.campaign_id == campaign_id))
    rows = result.scalars().all()
    return {r.key: r.value for r in rows}
