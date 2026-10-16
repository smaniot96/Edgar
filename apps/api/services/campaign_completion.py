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
from agent.resolution import completion_proposed
from db.postgres.models import Campaign, CharacterAssignment


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
    return completion_proposed(adj)


async def lock_campaign(db: AsyncSession, campaign: Campaign) -> None:
    """Lock the campaign row until the caller's transaction ends, then reload its state.

    Lifecycle writers (close, reopen, assign, release) lock the campaign before reading its status
    or touching assignments. Turn persistence follows the same order (session row, campaign row,
    then assignments), so competing writers wait on each other instead of deadlocking.
    """
    await db.refresh(campaign, with_for_update=True)


async def close_campaign(db: AsyncSession, campaign: Campaign) -> bool:
    """End a campaign and its active assignments with one shared timestamp.

    Reopening uses that timestamp to distinguish these assignments from manual releases.
    Concurrent closers queue on the campaign lock, so only the first one closes it.
    The caller commits or flushes the change as part of its own transaction.
    """
    await lock_campaign(db, campaign)
    if campaign.status == "ended":
        return False
    now = datetime.now(UTC)
    campaign.status = "ended"
    campaign.ended_at = now
    await db.execute(
        update(CharacterAssignment)
        .where(
            CharacterAssignment.campaign_id == campaign.id,
            CharacterAssignment.ended_at.is_(None),
        )
        .values(ended_at=now)
    )
    return True


async def maybe_complete_campaign(
    db: AsyncSession, campaign_id: int, adj: AdjudicationResult | None
) -> bool:
    if not _completion_flagged(adj):
        return False

    result = await db.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one_or_none()
    if campaign is None or not await close_campaign(db, campaign):
        return False

    # Flush only: the caller (turn_runner.persist_turn) commits this with the rest of the turn.
    await db.flush()
    return True
