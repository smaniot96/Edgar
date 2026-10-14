"""Character bounds apply to every writer, beyond HTTP validators."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.postgres.models import Character, CharacterAssignment


@pytest.mark.parametrize(
    ("model", "field", "invalid"),
    [
        (Character, "level", 0),
        (Character, "level", 21),
        (Character, "hp_max", 0),
        (CharacterAssignment, "hp_current", -1),
        (CharacterAssignment, "hp_max", 0),
    ],
)
async def test_database_rejects_invalid_character_bounds(
    api_client, db_session, model, field, invalid
):
    await api_client.post("/api/seed")
    row = (await db_session.execute(select(model))).scalar_one()
    setattr(row, field, invalid)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_session_dependency_rolls_back_uncommitted_work(api_client, db_session) -> None:
    from db.postgres import session as session_module

    await api_client.post("/api/seed")
    original = (await db_session.execute(select(Character.name))).scalar_one()
    dependency = session_module.get_session()
    isolated_session = await anext(dependency)
    row = (await isolated_session.execute(select(Character))).scalar_one()
    row.name = "must not persist"
    await isolated_session.flush()
    await dependency.aclose()
    db_session.expire_all()
    assert (await db_session.execute(select(Character.name))).scalar_one() == original


async def test_postgres_error_logging_hides_bound_parameters_and_server_row_details(
    api_client,
    db_session,
) -> None:
    import json
    import sys

    import structlog
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from apps.api.logging_config import format_safe_exception, redact_sensitive_data
    from db.postgres import session as session_module

    await api_client.post("/api/seed")
    engine = create_async_engine(
        session_module.engine.url, hide_parameters=True, poolclass=NullPool
    )
    statement = text(
        "UPDATE characters SET level = 0, name = :name, base_stats = CAST(:stats AS jsonb)"
    )
    params = {
        "name": "private-character-value\n\nprivate-name-tail",
        "stats": json.dumps({"story": "private-story-value\n\nsecond-private-line"}),
    }
    try:
        async with engine.begin() as connection:
            try:
                await connection.execute(statement, params)
            except IntegrityError:
                exc_info = sys.exc_info()
                event = structlog.processors.format_exc_info(
                    None, "exception", {"event": "database_failed", "exc_info": exc_info}
                )
                # Prove this is the server DETAIL leak that hide_parameters alone misses.
                assert "private-character-value" in event["exception"]
                assert "private-story-value" in event["exception"]
                event = format_safe_exception(
                    None, "exception", {"event": "database_failed", "exc_info": exc_info}
                )
                event = redact_sensitive_data(None, "exception", event)
                output = json.dumps(event)
                assert "characters_level_valid" in output
                assert "CheckViolationError" in output
                assert "IntegrityError" in output
                for value in params.values():
                    assert value not in output
                assert "private-story-value" not in output
                assert "second-private-line" not in output
                assert "private-character-value" not in output
                assert "private-name-tail" not in output
            else:
                pytest.fail("expected the database character bounds to reject the update")
    finally:
        await engine.dispose()
