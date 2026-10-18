"""Solo-or-shared decision for a campaign's adventure review (EDG-176 on EDG-046 membership).

The creator is the implicit owner and has no membership row, so a campaign is solo exactly when it
has no membership rows at all: every row is another user (a player, a spectator or a co-owner).
Shared campaigns keep the manual review flow; the spoiler-safe solo flow never applies to them.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import CampaignMembership


async def other_member_count(db: AsyncSession, campaign) -> int:
    """Members besides the creator (players, spectators and co-owners)."""
    count = await db.scalar(
        select(func.count())
        .select_from(CampaignMembership)
        .where(
            CampaignMembership.campaign_id == campaign.id,
            CampaignMembership.user_id != campaign.created_by,
        )
    )
    return int(count or 0)


async def is_solo_campaign(db: AsyncSession, campaign) -> bool:
    return await other_member_count(db, campaign) == 0
