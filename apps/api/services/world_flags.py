"""Campaign-scoped world flag reads and writes.

Writes join the caller's transaction; these helpers never commit on their own.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import WorldFlag


async def load_world_flag(db: AsyncSession, campaign_id: int, key: str) -> WorldFlag | None:
    result = await db.execute(
        select(WorldFlag).where(WorldFlag.campaign_id == campaign_id, WorldFlag.key == key)
    )
    return result.scalar_one_or_none()


async def upsert_world_flag(db: AsyncSession, campaign_id: int, key: str, value: str) -> WorldFlag:
    """Atomically insert/update a campaign key without committing the caller's transaction.

    Sessions in the same campaign have distinct turn locks, so a read-then-insert would
    race on the unique campaign/key constraint. RETURNING also refreshes a flag already
    present in the session's identity map.
    """
    statement = insert(WorldFlag).values(campaign_id=campaign_id, key=key, value=value)
    statement = statement.on_conflict_do_update(
        constraint="uq_world_flags_campaign_id_key",
        set_={"value": statement.excluded.value},
    ).returning(WorldFlag)
    result = await db.execute(statement, execution_options={"populate_existing": True})
    return result.scalar_one()


async def load_world_flags(db: AsyncSession, campaign_id: int) -> dict[str, str]:
    """Return key -> value for all flags for this campaign."""
    result = await db.execute(select(WorldFlag).where(WorldFlag.campaign_id == campaign_id))
    rows = result.scalars().all()
    return {r.key: r.value for r in rows}
