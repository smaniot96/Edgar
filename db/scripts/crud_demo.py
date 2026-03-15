"""
Basic async CRUD demo (Tutorial 01, Step 8).

Run from the db directory so the db package is on the path:
    uv run python scripts/crud_demo.py

Or from the project root (Edgar/):
    PYTHONPATH=. uv run --project db python db/scripts/crud_demo.py
"""
import asyncio
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

# Add project root so "db" package is importable when run from db/ or from project root
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from sqlalchemy import select
from db.postgres import async_session_factory
from db.postgres.models import User, Campaign, Session


async def crud_1_user_basic(session) -> None:
    print("\n=== CRUD #1: User (Create -> Read -> Update -> Delete) ===")

    # CREATE
    user = User(email="crud1@example.com")
    session.add(user)
    await session.flush()  # assigns user.id
    user_id = user.id
    await session.commit()
    print(f"CREATE: user_id={user_id}")

    # READ
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one()
    print(f"READ:   user_id={user.id} email={user.email}")

    # UPDATE
    user.email = "crud1-updated@example.com"
    await session.commit()
    print("UPDATE: email -> crud1-updated@example.com")

    # READ (verify update)
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one()
    print(f"VERIFY: user_id={user.id} email={user.email}")

    # DELETE
    await session.delete(user)
    await session.commit()
    print("DELETE: user deleted")

    # READ (verify delete)
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    print(f"VERIFY: user exists? {bool(user)}")


async def crud_2_campaign_under_user(session) -> None:
    print("\n=== CRUD #2: Campaign under User (Create -> Read by FK -> Update -> Delete) ===")

    # CREATE parent
    user = User(email="crud2-owner@example.com")
    session.add(user)
    await session.flush()
    owner_id = user.id

    # CREATE campaign (FK: created_by -> user.id)
    campaign = Campaign(title="CRUD2 Campaign", system="D&D 5e", created_by=owner_id)
    session.add(campaign)
    await session.flush()
    campaign_id = campaign.id
    await session.commit()
    print(f"CREATE: user_id={owner_id}, campaign_id={campaign_id}")

    # READ campaigns by owner FK
    result = await session.execute(select(Campaign).where(Campaign.created_by == owner_id))
    campaigns = result.scalars().all()
    print("READ:   campaigns for owner:", [(c.id, c.title) for c in campaigns])

    # UPDATE campaign title
    result = await session.execute(select(Campaign).where(Campaign.id == campaign_id))
    campaign = result.scalar_one()
    campaign.title = "CRUD2 Campaign (updated)"
    await session.commit()
    print("UPDATE: campaign title -> CRUD2 Campaign (updated)")

    # DELETE campaign + user (cleanup)
    await session.delete(campaign)
    await session.delete(user)
    await session.commit()
    print("DELETE: campaign and owner user deleted")

    # VERIFY deletion (campaign)
    result = await session.execute(select(Campaign).where(Campaign.id == campaign_id))
    deleted_campaign = result.scalar_one_or_none()
    print(f"VERIFY: campaign exists? {bool(deleted_campaign)}")


async def crud_3_session_lifecycle(session) -> None:
    print("\n=== CRUD #3: Session lifecycle (Create -> Read -> Update ended_at -> Delete) ===")

    # Use a unique email so this demo can be run multiple times without UniqueViolationError
    unique_suffix = uuid.uuid4().hex[:8]
    owner_email = f"crud3-owner-{unique_suffix}@example.com"

    # Create minimal parent chain: User -> Campaign
    user = User(email=owner_email)
    session.add(user)
    await session.flush()

    campaign = Campaign(title="CRUD3 Campaign", system="D&D 5e", created_by=user.id)
    session.add(campaign)
    await session.flush()

    # CREATE session row
    session_obj = Session(
        campaign_id=campaign.id,
        started_at=datetime.now(timezone.utc),
        ended_at=None,
    )
    session.add(session_obj)
    await session.flush()
    session_id = session_obj.id
    await session.commit()
    print(f"CREATE: user_id={user.id}, campaign_id={campaign.id}, session_id={session_id}")

    # READ session
    result = await session.execute(select(Session).where(Session.id == session_id))
    session_obj = result.scalar_one()
    print(
        "READ:   "
        f"id={session_obj.id} campaign_id={session_obj.campaign_id} "
        f"started_at={session_obj.started_at} ended_at={session_obj.ended_at}"
    )

    # UPDATE: end the session
    session_obj.ended_at = datetime.now(timezone.utc)
    await session.commit()
    print("UPDATE: ended_at set")

    # READ (verify update)
    result = await session.execute(select(Session).where(Session.id == session_id))
    session_obj = result.scalar_one()
    print(f"VERIFY: ended_at={session_obj.ended_at}")

    # DELETE session + parents (cleanup): child first so FK constraints are satisfied
    await session.delete(session_obj)
    await session.flush()  # emit DELETE for sessions before deleting campaign
    await session.delete(campaign)
    await session.delete(user)
    await session.commit()
    print("DELETE: session, campaign, and user deleted")

    # VERIFY deletion (session)
    result = await session.execute(select(Session).where(Session.id == session_id))
    deleted_session = result.scalar_one_or_none()
    print(f"VERIFY: session exists? {bool(deleted_session)}")


async def main() -> None:
    # Use a fresh DB session per CRUD so they’re isolated and easier to debug
    async with async_session_factory() as session:
        await crud_1_user_basic(session)

    async with async_session_factory() as session:
        await crud_2_campaign_under_user(session)

    async with async_session_factory() as session:
        await crud_3_session_lifecycle(session)


if __name__ == "__main__":
    asyncio.run(main())