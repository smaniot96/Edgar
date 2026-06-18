"""Auto-complete a campaign when the adjudicator signals the adventure has concluded.

The RulesAdjudicator sets a `campaign_complete=true` flag at a true ending (see
`agent/prompts.py`). After the turn's writes commit, the turn endpoints call
`maybe_complete_campaign`, which marks the campaign `ended` and closes open character
assignments — the same lifecycle as the manual `/campaigns/{id}/end` endpoint. Returns True if
it ended the campaign this turn so the caller can surface it to the client.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.adjudication import AdjudicationResult
from db.postgres.models import Campaign, CharacterAssignment

_TRUTHY = {"true", "1", "yes", "complete", "completed", "victory"}


def _completion_flagged(adj: AdjudicationResult | None) -> bool:
    if adj is None:
        return False
    for fu in adj.flags_set:
        if fu.key == "campaign_complete" and str(fu.value).strip().lower() in _TRUTHY:
            return True
    return False


async def maybe_complete_campaign(
    db: AsyncSession, campaign_id: int, adj: AdjudicationResult | None
) -> bool:
    if not _completion_flagged(adj):
        return False

    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None or campaign.status == "ended":
        return False

    now = datetime.now(timezone.utc)
    campaign.status = "ended"
    campaign.ended_at = now
    await db.execute(
        update(CharacterAssignment)
        .where(
            CharacterAssignment.campaign_id == campaign_id,
            CharacterAssignment.ended_at.is_(None),
        )
        .values(ended_at=now)
    )
    await db.commit()
    return True
