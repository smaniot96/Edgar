"""Turn diagnostics through the real persistence path (EDG-053). Needs Postgres (testcontainers).

Hosted CI runs these; they are not executed on a machine without Docker. Assertions do not depend
on dice or on model wording: they check row presence, atomicity, retry idempotence, the developer
read gate and the whitelisted reload shape.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agent.turn_diagnostics import TURN_DIAGNOSTICS_EVENT
from db.postgres.models import EventLog
from tests.api.test_turn_pipeline import _count_events, _seed, _sse_events


async def _diagnostic_rows(session_id: int) -> list[dict]:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(EventLog)
                    .where(
                        EventLog.session_id == session_id,
                        EventLog.event_type == TURN_DIAGNOSTICS_EVENT,
                    )
                    .order_by(EventLog.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [dict(row.payload) for row in rows]


@pytest.mark.asyncio
async def test_sync_turn_stores_one_row_and_reload_returns_whitelisted_view(
    fake_llm_turn, fake_retrieval, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]

    turn = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I look around."})
    assert turn.status_code == 200

    (row,) = await _diagnostic_rows(sid)
    assert row["delivery"] == "sync"
    assert row["path"] == "normal"
    assert row["total_ms"] >= 0

    listed = await api_client.get(f"/api/sessions/{sid}/diagnostics/turns")
    assert listed.status_code == 200
    body = listed.json()
    assert body["scope"] == "dm_turns"
    assert body["summary"]["turns"] == 1
    (turn_view,) = body["turns"]
    assert turn_view["scope"] == "dm_turn"
    for forbidden in ("prompt", "headers", "source_reference", "creature_id", "narration"):
        assert forbidden not in str(turn_view)


@pytest.mark.asyncio
async def test_stream_turn_debug_frame_carries_the_same_row(
    fake_llm_turn, fake_retrieval, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]

    r = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I look around.", "debug": True}
    )
    events = _sse_events(r.text)
    debug = next(data for name, data in events if name == "debug")
    assert debug["turn_diagnostics"]["delivery"] == "stream"
    assert debug["turn_diagnostics"]["path"] == "normal"
    assert len(await _diagnostic_rows(sid)) == 1


@pytest.mark.asyncio
async def test_failed_persist_leaves_no_diagnostics_row(
    fake_llm_turn, fake_retrieval, api_client, monkeypatch
) -> None:
    from apps.api.services import turn_runner

    async def boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    # Fails after the diagnostics row has been staged in the same transaction as the turn.
    monkeypatch.setattr(turn_runner, "maybe_complete_campaign", boom)
    sid = (await _seed(api_client))["session_id"]

    r = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I look around."}
    )
    assert _sse_events(r.text)[-1][0] == "error"
    assert await _diagnostic_rows(sid) == []
    assert await _count_events(sid, "narration") == 0


@pytest.mark.asyncio
async def test_retried_turn_writes_one_diagnostics_row(fake_llm_turn, fake_retrieval, api_client) -> None:
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services.turn_runner import persist_turn, run_turn

    sid = (await _seed(api_client))["session_id"]
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I look around.", db)
    initial["turn_id"] = "edg-053-retry"
    result = await run_turn(initial)
    first = await persist_turn(sid, initial["player_input"], result)
    assert await persist_turn(sid, initial["player_input"], result) == first

    assert len(await _diagnostic_rows(sid)) == 1
    async with sm.async_session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(EventLog.session_id == sid, EventLog.event_type == TURN_DIAGNOSTICS_EVENT)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_turn_diagnostics_read_is_developer_gated(api_client, monkeypatch) -> None:
    sid = (await _seed(api_client))["session_id"]
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    hidden = await api_client.get(f"/api/sessions/{sid}/diagnostics/turns")
    assert hidden.status_code == 404

    monkeypatch.setenv("EDGAR_DEBUG", "1")
    empty = await api_client.get(f"/api/sessions/{sid}/diagnostics/turns")
    assert empty.status_code == 200
    assert empty.json()["turns"] == []
    assert empty.json()["summary"]["turns"] == 0
    assert empty.json()["summary"]["total_ms"]["median"] is None
