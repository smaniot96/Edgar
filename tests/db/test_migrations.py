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

        _alembic(postgres_env, "downgrade", "10_integrity")
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


async def test_source_mode_migration_preserves_bindings_and_requires_explicit_choice(postgres_env):
    import pytest
    from sqlalchemy.exc import IntegrityError

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
        _alembic(postgres_env, "downgrade", "11_character_bounds")
        async with engine.begin() as connection:
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users (email) VALUES ('sources-migration@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns (created_by, title, system, adventure_collections) "
                        "VALUES (:uid, 'Lost source', '5e', ARRAY['lost_module']) RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            sid = (
                await connection.execute(
                    text("INSERT INTO sessions (campaign_id) VALUES (:cid) RETURNING id"),
                    {"cid": cid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO event_log (session_id, event_type, payload) VALUES (:sid, 'narration', '{\"narration\":\"Preserve history\"}')"
                ),
                {"sid": sid},
            )
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            "SELECT title, adventure_collections, source_mode, rules_edition FROM campaigns WHERE id=:cid"
                        ),
                        {"cid": cid},
                    )
                )
                .mappings()
                .one()
            )
            assert dict(row) == {
                "title": "Lost source",
                "adventure_collections": ["lost_module"],
                "source_mode": "module",
                "rules_edition": "2014",
            }
            assert (
                await connection.execute(
                    text("SELECT payload FROM event_log WHERE session_id=:sid"), {"sid": sid}
                )
            ).scalar_one() == {"narration": "Preserve history"}
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE campaigns SET source_mode='automatic' WHERE id=:cid"),
                        {"cid": cid},
                    )
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        text("UPDATE campaigns SET rules_edition='unknown' WHERE id=:cid"),
                        {"cid": cid},
                    )
            await connection.execute(
                text(
                    "UPDATE campaigns SET source_mode='homebrew', rules_edition='2024' WHERE id=:cid"
                ),
                {"cid": cid},
            )
        assert "No new upgrade operations detected" in _alembic(postgres_env, "check").stdout
        _alembic(postgres_env, "downgrade", "11_character_bounds")
        async with engine.connect() as connection:
            assert "source_mode" not in [
                column["name"]
                for column in await connection.run_sync(
                    lambda conn: inspect(conn).get_columns("campaigns")
                )
            ]
            assert (
                await connection.execute(
                    text("SELECT adventure_collections FROM campaigns WHERE id=:cid"), {"cid": cid}
                )
            ).scalar_one() == ["lost_module"]
        _alembic(postgres_env, "upgrade", "head")
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT source_mode FROM campaigns WHERE id=:cid"), {"cid": cid}
                )
            ).scalar_one() == "module"
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_combat_pointer_retirement_refuses_populated_history_and_keeps_roster(postgres_env):
    """A populated legacy pointer blocks removal; once cleared, the combat roster survives."""
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
    roster = [
        {"name": "player", "is_player": True, "hp_current": 7},
        {"name": "Goblin", "is_player": False, "hp_current": 0},
    ]
    try:
        _alembic(postgres_env, "downgrade", "14_session_story_ledger")
        async with engine.begin() as connection:
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email) VALUES ('combat-pointer@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns(created_by,title,system) VALUES (:uid,'Fight','5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            char = (
                await connection.execute(
                    text(
                        "INSERT INTO characters (owner_user_id, name, class, level, hp_max, base_stats, base_inventory) "
                        "VALUES (:uid, 'Pointer Hero', 'Fighter', 1, 12, CAST('{}' AS jsonb), CAST('{}' AS jsonb)) RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            sid = (
                await connection.execute(
                    text("INSERT INTO sessions(campaign_id) VALUES (:cid) RETURNING id"),
                    {"cid": cid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO combat_state (session_id, active_character, initiative_order, round, combatants, outcome) "
                    "VALUES (:sid, :char, CAST('[]' AS jsonb), 2, CAST(:roster AS jsonb), 'victory')"
                ),
                {"sid": sid, "char": char, "roster": json.dumps(roster)},
            )

        refused = _alembic(postgres_env, "upgrade", "head", check=False)
        assert refused.returncode != 0
        assert "combat_state: 1 row(s) hold a legacy active_character value" in refused.stderr
        assert "no data was changed" in refused.stderr
        async with engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT active_character FROM combat_state WHERE session_id=:sid"),
                    {"sid": sid},
                )
            ).scalar_one() == char
            await connection.execute(text("UPDATE combat_state SET active_character = NULL"))

        _alembic(postgres_env, "upgrade", "head")
        assert "No new upgrade operations detected" in _alembic(postgres_env, "check").stdout
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("combat_state")
            )
            assert "active_character" not in {column["name"] for column in columns}
            assert (
                (
                    await connection.execute(
                        text(
                            "SELECT round, outcome, combatants FROM combat_state WHERE session_id=:sid"
                        ),
                        {"sid": sid},
                    )
                )
                .mappings()
                .one()
            ) == {"round": 2, "outcome": "victory", "combatants": roster}

        _alembic(postgres_env, "downgrade", "14_session_story_ledger")
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("combat_state")
            )
            column = next(c for c in columns if c["name"] == "active_character")
            assert column["nullable"] is True
            foreign_keys = await connection.run_sync(
                lambda conn: inspect(conn).get_foreign_keys("combat_state")
            )
            assert any(
                fk["constrained_columns"] == ["active_character"]
                and fk["referred_table"] == "characters"
                and fk.get("options", {}).get("ondelete") == "SET NULL"
                for fk in foreign_keys
            )
            indexes = await connection.run_sync(
                lambda conn: inspect(conn).get_indexes("combat_state")
            )
            assert any(index["name"] == "ix_combat_state_active_character" for index in indexes)
            assert (
                await connection.execute(
                    text("SELECT active_character FROM combat_state WHERE session_id=:sid"),
                    {"sid": sid},
                )
            ).scalar_one() is None
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_session_story_ledger_migration_round_trip_preserves_legacy_rows(postgres_env):
    """Only additive schema on upgrade; existing NPC/history/inventory rows survive."""
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
        _alembic(postgres_env, "downgrade", "13_campaign_rules_edition")
        async with engine.begin() as connection:
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email) VALUES ('ledger-migration@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns(created_by,title,system) VALUES (:uid,'Keep','5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            sid = (
                await connection.execute(
                    text("INSERT INTO sessions(campaign_id) VALUES (:cid) RETURNING id"),
                    {"cid": cid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO npcs(campaign_id,name,disposition,stat_block) VALUES (:cid,'Legacy','friendly','{\"original\": true}')"
                ),
                {"cid": cid},
            )
            await connection.execute(
                text(
                    "INSERT INTO world_flags(campaign_id,key,value) VALUES (:cid,'legacy','kept')"
                ),
                {"cid": cid},
            )
            await connection.execute(
                text(
                    "INSERT INTO event_log(session_id,event_type,payload) VALUES (:sid,'narration','{\"text\":\"Keep\"}')"
                ),
                {"sid": sid},
            )
        for _ in range(2):
            _alembic(postgres_env, "upgrade", "head")
            assert "No new upgrade operations detected" in _alembic(postgres_env, "check").stdout
            async with engine.connect() as connection:
                npc = (
                    (
                        await connection.execute(
                            text(
                                "SELECT name,disposition,stat_block,session_id,story_identity FROM npcs WHERE campaign_id=:cid"
                            ),
                            {"cid": cid},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert dict(npc) == {
                    "name": "Legacy",
                    "disposition": "friendly",
                    "stat_block": {"original": True},
                    "session_id": None,
                    "story_identity": None,
                }
                assert (
                    await connection.execute(
                        text("SELECT value FROM world_flags WHERE campaign_id=:cid"), {"cid": cid}
                    )
                ).scalar_one() == "kept"
                assert (
                    await connection.execute(
                        text("SELECT payload FROM event_log WHERE session_id=:sid"), {"sid": sid}
                    )
                ).scalar_one() == {"text": "Keep"}
                unique = await connection.run_sync(
                    lambda conn: inspect(conn).get_unique_constraints("story_facts")
                )
                assert any(c["column_names"] == ["session_id", "effect_id"] for c in unique)
            _alembic(postgres_env, "downgrade", "13_campaign_rules_edition")
        _alembic(postgres_env, "upgrade", "head")
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_character_library_downgrade_refuses_populated_rows_and_keeps_history(postgres_env):
    """Revision 7 cannot hold library ownership or assignment history, so 8 refuses while rows exist."""
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
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email) VALUES ('library-downgrade@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns(created_by,title,system) VALUES (:uid,'Library','5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            active = (
                await connection.execute(
                    text(
                        "INSERT INTO characters (owner_user_id, name, class, level, hp_max, base_stats, base_inventory) "
                        "VALUES (:uid, 'Active Hero', 'Fighter', 1, 12, CAST('{}' AS jsonb), CAST('{}' AS jsonb)) RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            retired = (
                await connection.execute(
                    text(
                        "INSERT INTO characters (owner_user_id, name, class, level, hp_max, base_stats, base_inventory) "
                        "VALUES (:uid, 'Retired Hero', 'Wizard', 1, 8, CAST('{}' AS jsonb), CAST('{}' AS jsonb)) RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO character_assignments (character_id, campaign_id, hp_current, hp_max, stats, inventory) "
                    "VALUES (:char, :cid, 12, 12, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
                ),
                {"char": active, "cid": cid},
            )
            await connection.execute(
                text(
                    "INSERT INTO character_assignments (character_id, campaign_id, hp_current, hp_max, stats, inventory, ended_at) "
                    "VALUES (:char, :cid, 8, 8, CAST('{}' AS jsonb), CAST('{}' AS jsonb), now())"
                ),
                {"char": retired, "cid": cid},
            )

        refused = _alembic(postgres_env, "downgrade", "7_campaign_status", check=False)
        assert refused.returncode != 0
        assert "characters: 2 row(s) hold library ownership" in refused.stderr
        assert "no data was changed" in refused.stderr
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("characters")
            )
            assert "owner_user_id" in {column["name"] for column in columns}
            assert (
                await connection.execute(text("SELECT count(*) FROM character_assignments"))
            ).scalar_one() == 2
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_session_link_downgrade_refuses_populated_characters_and_runs_when_empty(
    postgres_env,
):
    """campaign_id replaced session_id, so 1ad2 refuses populated characters and restores empty ones."""
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
        _alembic(postgres_env, "downgrade", "1ad2ad6560f5")
        async with engine.begin() as connection:
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email) VALUES ('session-link@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns(created_by,title,system) VALUES (:uid,'Link','5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO characters (name, class, level, hp_max, campaign_id, hp_current, stats, inventory) "
                    "VALUES ('Linked Hero', 'Rogue', 1, 8, :cid, 8, CAST('{}' AS jsonb), CAST('{}' AS jsonb))"
                ),
                {"cid": cid},
            )

        refused = _alembic(postgres_env, "downgrade", "a2e2bb9f88cc", check=False)
        assert refused.returncode != 0
        assert "characters: 1 row(s) cannot recover the session_id" in refused.stderr
        assert "no data was changed" in refused.stderr
        async with engine.begin() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("characters")
            )
            assert "campaign_id" in {column["name"] for column in columns}
            await connection.execute(text("DELETE FROM characters"))

        _alembic(postgres_env, "downgrade", "a2e2bb9f88cc")
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("characters")
            )
            assert "session_id" in {column["name"] for column in columns}
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_combat_active_character_downgrade_refuses_rows_without_a_character(postgres_env):
    """Revision 4 requires combat_state.active_character, so 5 refuses while any row holds NULL."""
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
        _alembic(postgres_env, "downgrade", "5_combat_state_extensions")
        async with engine.begin() as connection:
            uid = (
                await connection.execute(
                    text(
                        "INSERT INTO users(email) VALUES ('combat-null@example.test') RETURNING id"
                    )
                )
            ).scalar_one()
            cid = (
                await connection.execute(
                    text(
                        "INSERT INTO campaigns(created_by,title,system) VALUES (:uid,'Encounter','5e') RETURNING id"
                    ),
                    {"uid": uid},
                )
            ).scalar_one()
            sid = (
                await connection.execute(
                    text(
                        "INSERT INTO sessions(campaign_id, started_at) VALUES (:cid, now()) RETURNING id"
                    ),
                    {"cid": cid},
                )
            ).scalar_one()
            await connection.execute(
                text(
                    "INSERT INTO combat_state (session_id, initiative_order, round) "
                    "VALUES (:sid, CAST('[]' AS jsonb), 3)"
                ),
                {"sid": sid},
            )

        refused = _alembic(postgres_env, "downgrade", "4_session_active_character", check=False)
        assert refused.returncode != 0
        assert "combat_state: 1 row(s) hold no active_character" in refused.stderr
        assert "no data was changed" in refused.stderr
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda conn: inspect(conn).get_columns("combat_state")
            )
            assert "ended" in {column["name"] for column in columns}
            assert (
                await connection.execute(text("SELECT round FROM combat_state"))
            ).scalar_one() == 3
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()


async def test_base_downgrade_refuses_populated_users_and_keeps_every_table(postgres_env):
    """Dropping the initial schema deletes every table, so the base downgrade refuses any user."""
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
            await connection.execute(
                text("INSERT INTO users(email) VALUES ('base-downgrade@example.test')")
            )

        refused = _alembic(postgres_env, "downgrade", "base", check=False)
        assert refused.returncode != 0
        assert "users: 1 row(s) would be deleted with every table" in refused.stderr
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert "character_assignments" in tables
            assert (await connection.execute(text("SELECT count(*) FROM users"))).scalar_one() == 1
    finally:
        _alembic(postgres_env, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE event_log, combat_state, sessions, character_assignments, characters, npcs, world_flags, campaigns, users RESTART IDENTITY CASCADE"
                )
            )
        await engine.dispose()
