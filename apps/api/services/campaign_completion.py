"""Auto-complete a campaign when the adjudicator signals the adventure has concluded.

The LLM can only PROPOSE completion (a `campaign_complete` flag). The engine
(`agent.resolution.decide_completion`) decides and records the verdict in
`adj.completion_status`: "accepted" only after a victory over a boss-flagged enemy, or when the
player explicitly confirms on the turn after a proposal (stored as the pending
`campaign_completion_proposed` world flag). A single proposal is never enough. After the turn's
writes, the turn endpoints call
`maybe_complete_campaign`, which marks the campaign `ended` and closes open character
assignments — the same lifecycle as the manual `/campaigns/{id}/end` endpoint. Returns True if
it ended the campaign this turn so the caller can surface it to the client.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.adjudication import AdjudicationResult
from db.postgres.models import Campaign, CharacterAssignment

_TRUTHY = {"true", "1", "yes", "complete", "completed", "victory"}


def _completion_flagged(adj: AdjudicationResult | None) -> bool:
    """True only when the engine accepted completion this turn.

    Adjudications that went through the engine always carry `completion_status`; it is the
    sole authority. The raw-flag fallback only covers hand-built results (no engine verdict).
    """
    if adj is None:
        return False
    status = getattr(adj, "completion_status", None)
    if status is not None:
        return status == "accepted"
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

    now = datetime.now(UTC)
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
    # Flush only: the caller (turn_runner.persist_turn) commits this with the rest of the turn.
    await db.flush()
    return True
