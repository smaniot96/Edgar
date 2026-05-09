"""Async engine and session factory.

`expire_on_commit=False` keeps ORM objects usable after `commit()` so the turn endpoint can
read back fields (HP, scene id) without re-querying. `get_session` is a FastAPI-compatible
async generator dependency; tests override it with a transactional session per test.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from edgar_core.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session():
    async with async_session_factory() as session:
        yield session
