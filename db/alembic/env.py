from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from edgar_core.config import DATABASE_URL

alembic_cfg = context.config

if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)

# Use URL from config (single source of truth)
# ConfigParser treats '%' as interpolation; get_main_option restores the encoded URL.
alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

import db.postgres.models  # noqa: F401 - register all models on Base.metadata
from db.postgres.base import Base

target_metadata = Base.metadata


# Shared by every migrator on a database (upgrade, downgrade, concurrent deploys). Taken inside the
# migration transaction, so a second migrator waits here and then reads the revision the first one
# committed, instead of racing it on the same DDL.
MIGRATION_LOCK_KEY = 0x45444741  # "EDGA"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generate SQL only, no DB connection)."""
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async engine, real DB)."""
    connectable = create_async_engine(
        alembic_cfg.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
    )

    async def run_async_migrations() -> None:
        async with connectable.connect() as connection:
            # The lock must come before do_run_migrations reads the current revision.
            await connection.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
            await connection.run_sync(do_run_migrations)
            # Alembic joins this open transaction rather than committing its own, so commit here.
            # Commit also releases the lock; a failure rolls back and releases it too.
            await connection.commit()

    import asyncio

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
