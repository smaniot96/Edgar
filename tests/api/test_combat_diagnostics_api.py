"""Combat round diagnostics through the real turn path (EDG-031).

The round's `combat_round` row commits or rolls back with its turn, sync and SSE store the same
shape, debug frames and reload read it back, and a retried turn writes it once. Needs Postgres
(testcontainers), so these run in hosted CI; assertions avoid depending on particular dice.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from agent.combat_telemetry import COMBAT_ROUND_EVENT, COMBAT_STAGES
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from db.postgres.models import EventLog
from tests.agent.test_creature_encounters import verified_record
from tests.api.test_turn_pipeline import _count_events, _fake_chat, _seed, _sse_events


@pytest.fixture
def goblin_fight(monkeypatch, fake_llm_turn, fake_retrieval):
    """A one-goblin encounter with a verified identity and a neutral adjudicator."""
    record = verified_record()
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *_a, **_kw: record)
    fake = _fake_chat()
    fake._parsed = ParsedInput(intent="combat", target="Goblin")
    fake._adj = AdjudicationResult(success=True, mechanical_summary="")
    return record


async def _round_rows(session_id: int) -> list[dict]:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(EventLog)
                    .where(
                        EventLog.session_id == session_id, EventLog.event_type == COMBAT_ROUND_EVENT
                    )
                    .order_by(EventLog.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [dict(row.payload) for row in rows]


@pytest.mark.asyncio
async def test_sync_combat_turn_stores_round_and_reload_returns_it(
    goblin_fight, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]

    turn = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I attack the goblin!", "debug": True}
    )
    assert turn.status_code == 200, turn.text
    body = turn.json()
    assert body["combat_state"] is not None

    rows = await _round_rows(sid)
    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"]
    assert row["timings_unavailable"] == []
    assert set(row["timings_ms"]) == set(COMBAT_STAGES)
    # The developer view carries the same adjudicated dice the player's roll cards were built from.
    assert (
        body["combat_diagnostics"]["dice_log"]
        == body["adjudication"]["dice_log"]
        == row["dice_log"]
    )
    assert {a["id"] for a in row["actors"]} <= {"Goblin"}
    assert any(d["is_player"] for d in row["dispositions"])
    # Planner-only creature provenance never reaches the player payload or the stored row.
    # (The developer `debug` block legitimately lists retrieved chunks with their provenance, so
    # the player-facing fields and the combat diagnostics are checked individually.)
    player_visible = str(
        {k: body.get(k) for k in ("narration", "adjudication", "combat_state", "character")}
    )
    assert "e" * 64 not in player_visible and "source_sha256" not in player_visible
    assert "source_sha256" not in str(body["combat_diagnostics"])
    assert "e" * 64 not in str(body["combat_diagnostics"])
    assert "e" * 64 not in str(row)

    reloaded = await api_client.get(f"/api/sessions/{sid}/diagnostics/combat")
    assert reloaded.status_code == 200
    (entry,) = reloaded.json()
    assert entry["turn_id"] == row["turn_id"]
    assert entry["dice_log"] == row["dice_log"]
    assert entry["timings_ms"] == row["timings_ms"]


@pytest.mark.asyncio
async def test_sync_combat_turn_without_debug_returns_no_diagnostics(
    goblin_fight, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]
    turn = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I attack the goblin!"}
    )
    assert turn.status_code == 200
    assert turn.json()["combat_diagnostics"] is None
    # The round is still stored: only the response is gated, never the evidence.
    assert len(await _round_rows(sid)) == 1


@pytest.mark.asyncio
async def test_sse_combat_turn_stores_same_round_and_streams_debug(
    goblin_fight, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I attack the goblin!", "debug": True}
    )
    events = _sse_events(r.text)
    names = [name for name, _ in events]
    assert names[-1] == "done", r.text
    adjudication = next(data for name, data in events if name == "adjudication")
    debug = next(data for name, data in events if name == "debug")
    done = next(data for name, data in events if name == "done")
    # The player-facing done payload never carries developer diagnostics.
    assert "combat_diagnostics" not in done

    diag = debug["combat_diagnostics"]
    assert diag["dice_log"] == adjudication["dice_log"]
    # Measured combat stages are reported in the same timing object as the parse and round totals.
    for stage in COMBAT_STAGES:
        assert stage in debug["timings_ms"]
    assert "combat_ms" in debug["timings_ms"]

    rows = await _round_rows(sid)
    assert len(rows) == 1
    assert rows[0]["dice_log"] == diag["dice_log"]
    assert rows[0]["actors"] == diag["actors"]
    assert rows[0]["outcomes"] == diag["outcomes"]
    assert rows[0]["dispositions"] == diag["dispositions"]
    assert set(rows[0]) == set(diag)


@pytest.mark.asyncio
async def test_failed_turn_leaves_no_round_telemetry(monkeypatch, goblin_fight, api_client) -> None:
    from apps.api.services import turn_runner

    async def boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    # Fails after the combat round's telemetry has been staged in the turn transaction.
    monkeypatch.setattr(turn_runner, "maybe_complete_campaign", boom)
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I attack the goblin!"}
    )
    events = _sse_events(r.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "turn_save_failed"
    assert await _round_rows(sid) == []
    assert await _count_events(sid, "narration") == 0


@pytest.mark.asyncio
async def test_retried_combat_turn_writes_one_round(goblin_fight, api_client) -> None:
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services.turn_runner import persist_turn, run_turn

    sid = (await _seed(api_client))["session_id"]
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I attack the goblin!", db)
    result = await run_turn(initial)
    assert result.get("turn_id")
    first = await persist_turn(sid, initial["player_input"], result)
    assert await persist_turn(sid, initial["player_input"], result) == first
    assert len(await _round_rows(sid)) == 1
    async with sm.async_session_factory() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(EventLog)
            .where(EventLog.session_id == sid, EventLog.event_type == COMBAT_ROUND_EVENT)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_legacy_session_reports_no_captured_rounds(api_client, monkeypatch) -> None:
    sid = (await _seed(api_client))["session_id"]
    # Developer evidence is gated: without EDGAR_DEBUG the route does not exist for the client.
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    hidden = await api_client.get(f"/api/sessions/{sid}/diagnostics/combat")
    assert hidden.status_code == 404
    # A session with no stored combat rounds (every pre-EDG-031 combat turn) lists none.
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    listed = await api_client.get(f"/api/sessions/{sid}/diagnostics/combat")
    assert listed.status_code == 200
    assert listed.json() == []
