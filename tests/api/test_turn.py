import pytest
from sqlalchemy import select

from db.postgres.models import Character, EventLog, Session as SessionModel, WorldFlag


@pytest.mark.asyncio
async def test_session_turn_persists_narration_hp_flags(
    fake_llm_turn,
    fake_retrieval,
    api_client,
) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    session_id = seed.json()["session_id"]

    turn = await api_client.post(
        f"/api/sessions/{session_id}/turn",
        json={"message": "I look for tracks in the mud."},
    )
    assert turn.status_code == 200
    payload = turn.json()
    assert payload["narration"]
    assert payload["character"]["hp_current"] == 7
    assert payload["current_scene_id"] == "forest_trail"

    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        ev = (
            (
                await s.execute(
                    select(EventLog).where(
                        EventLog.session_id == session_id,
                        EventLog.event_type == "narration",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any("tracks" in (e.payload or {}).get("player_input", "") for e in ev)

        wf = (
            await s.execute(
                select(WorldFlag).where(WorldFlag.key == "goblin_ambush")
            )
        ).scalar_one_or_none()
        assert wf is not None
        assert wf.value == "resolved"

        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        assert sess.current_scene_id == "forest_trail"

        char_id = sess.active_character_id
        assert char_id is not None
        char = (await s.execute(select(Character).where(Character.id == char_id))).scalar_one()
        assert char.hp_current == 7


@pytest.mark.asyncio
async def test_turn_fails_when_hp_delta_zeroed_in_world_writes(
    monkeypatch,
    fake_llm_turn,
    fake_retrieval,
    api_client,
) -> None:
    """If apply_adjudication ignores HP delta, character HP stays at seed default (12)."""
    from apps.api.routers import session as session_mod
    from apps.api.services import world_writes

    real_apply = world_writes.apply_adjudication

    async def zero_hp_delta_apply(db, game_session, adj):  # noqa: ANN001
        if adj.character_update is None:
            await real_apply(db, game_session, adj)
            return
        adj2 = adj.model_copy(
            update={
                "character_update": adj.character_update.model_copy(update={"hp_delta": 0}),
            }
        )
        await real_apply(db, game_session, adj2)

    monkeypatch.setattr(session_mod, "apply_adjudication", zero_hp_delta_apply)

    seed = await api_client.post("/api/seed")
    session_id = seed.json()["session_id"]
    turn = await api_client.post(
        f"/api/sessions/{session_id}/turn",
        json={"message": "I scout ahead."},
    )
    assert turn.status_code == 200
    assert turn.json()["character"]["hp_current"] == 12
