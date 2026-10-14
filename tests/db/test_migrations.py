"""Upgrade/downgrade/drift and invalid-row preflight on the disposable test database."""

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

_ROOT = Path(__file__).resolve().parents[2]


def _alembic(env: dict[str, str], *arguments: str, check: bool = True):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=_ROOT / "db",
        env={**os.environ, **env},
        check=check,
        capture_output=True,
        text=True,
    )


async def test_character_bounds_migration_preserves_rows_refuses_invalid_data_and_has_no_drift(
    postgres_env,
) -> None:
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=postgres_env["POSTGRES_USER"],
            password=postgres_env["POSTGRES_PASSWORD"],
            host=postgres_env["DB_HOST"],
            port=int(postgres_env["DB_PORT"]),
            database=postgres_env["POSTGRES_DB"],
        ),
        poolclass=NullPool,
    )
    try:
        async with engine.begin() as connection:
            json_params = {
                "stats": json.dumps({"str": 16}),
                "inventory": json.dumps({"items": ["sword"]}),
            }
            uid = (
                await connection.execute(
                    text("INSERT INTO users (email) VALUES ('migration@example.test') RETURNING id")
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns (created_by, title, system) VALUES (:uid, 'Preserved', '5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            char = (
                await connection.execute(
                    text(
                        "INSERT INTO characters (owner_user_id, name, class, level, hp_max, base_stats, base_inventory) "
                        "VALUES (:uid, 'Preserved Hero', 'Fighter', 1, 12, CAST(:stats AS jsonb), CAST(:inventory AS jsonb)) RETURNING id"
                    ),
                    {"uid": uid, **json_params},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO character_assignments (character_id, campaign_id, hp_current, hp_max, stats, inventory) "
                    "VALUES (:char, :cid, 9, 12, CAST(:stats AS jsonb), CAST(:inventory AS jsonb))"
                ),
                {"char": char, "cid": cid, **json_params},
            )
            sid = (
                await connection.execute(
                    text(
                        "INSERT INTO sessions (campaign_id, active_character_id) VALUES (:cid, :char) RETURNING id"
                    ),
                    {"cid": cid, "char": char},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO event_log (session_id, event_type, payload) "
                    "VALUES (:sid, 'narration', CAST(:payload AS jsonb))"
                ),
                {"sid": sid, "payload": json.dumps({"narration": "Keep this story"})},
            )
            before = await _snapshot(connection, char, sid)

        _alembic(postgres_env, "downgrade", "-1")
        async with engine.begin() as connection:
            assert "characters_level_valid" not in await _constraint_names(connection, "characters")
            await connection.execute(
                text("UPDATE characters SET level = 0 WHERE id = :id"), {"id": char}
            )
        refused = _alembic(postgres_env, "upgrade", "head", check=False)
        assert refused.returncode != 0
        assert "characters: 1 row(s) violate characters_level_valid" in refused.stderr
        assert "no data was changed" in refused.stderr
        async with engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT level FROM characters WHERE id = :id"), {"id": char}
                )
            ).scalar_one() == 0
            assert "characters_level_valid" not in await _constraint_names(connection, "characters")
            await connection.execute(
                text("UPDATE characters SET level = 1 WHERE id = :id"), {"id": char}
            )
        _alembic(postgres_env, "upgrade", "head")
        assert "No new upgrade operations detected" in _alembic(postgres_env, "check").stdout
        async with engine.connect() as connection:
            assert await _snapshot(connection, char, sid) == before
            indexes = await connection.run_sync(lambda conn: inspect(conn).get_indexes("event_log"))
            assert any(
                index["name"] == "ix_event_log_session_id_event_type_id"
                and index["column_names"] == ["session_id", "event_type", "id"]
                for index in indexes
            )
    finally:
        # Cleanup and restore head even if a regression interrupted the round trip. This
        # engine points exclusively at the fixture's throwaway Postgres container.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, "
                    "characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        _alembic(postgres_env, "upgrade", "head")
        await engine.dispose()


async def _constraint_names(connection, table):
    constraints = await connection.run_sync(lambda conn: inspect(conn).get_check_constraints(table))
    return {constraint["name"] for constraint in constraints}


async def _snapshot(connection, character_id, session_id):
    character = (
        (
            await connection.execute(
                text("SELECT * FROM characters WHERE id = :id"), {"id": character_id}
            )
        )
        .mappings()
        .one()
    )
    assignment = (
        (
            await connection.execute(
                text("SELECT * FROM character_assignments WHERE character_id = :id"),
                {"id": character_id},
            )
        )
        .mappings()
        .one()
    )
    event = (
        (
            await connection.execute(
                text("SELECT * FROM event_log WHERE session_id = :id"), {"id": session_id}
            )
        )
        .mappings()
        .one()
    )
    return dict(character), dict(assignment), dict(event)
