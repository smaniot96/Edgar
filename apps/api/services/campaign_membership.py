"""Solo-or-shared decision for a campaign's adventure review (EDG-176 seam).

EDG-046 membership is not on main: no membership table or helper exists yet. Until it lands, the
campaign creator is the only member, so every campaign is solo. This module is the single seam to
replace: once membership exists, `other_member_count` returns the number of members besides the
creator and shared campaigns keep the manual review flow automatically.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


async def other_member_count(db: AsyncSession, campaign) -> int:
    """Members other than the creator. Always zero until EDG-046 membership is merged."""
    return 0


async def is_solo_campaign(db: AsyncSession, campaign) -> bool:
    return await other_member_count(db, campaign) == 0
