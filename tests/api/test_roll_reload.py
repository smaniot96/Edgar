"""Original roll cards survive reload without re-rolling (EDG-030).

The stored roll is written with the turn (sync and SSE share `persist_turn`), and the history
route returns that stored record. Needs Postgres (testcontainers), so these run in hosted CI;
they compare stored values with what the live turn returned, never with particular dice.
"""

from __future__ import annotations

import pytest

from agent.models.parsed_input import ParsedInput
from db.postgres.models import EventLog
from tests.agent.test_creature_encounters import verified_record
from tests.api.test_turn_pipeline import _fake_chat, _seed, _sse_events


def _dm_messages(history: list[dict]) -> list[dict]:
    return [m for m in history if m["role"] == "dm"]


async def _history(api_client, session_id: int) -> list[dict]:
    response = await api_client.get(f"/api/sessions/{session_id}/messages")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def athletics_check(fake_llm_turn, fake_retrieval):
    """A non-combat check the engine resolves itself (so dice are really rolled)."""
    fake = _fake_chat()
    fake._parsed = ParsedInput(intent="exploration", check="athletics", difficulty="medium")
    return fake


@pytest.fixture
def goblin_fight(monkeypatch, fake_llm_turn, fake_retrieval):
    from agent.models.adjudication import AdjudicationResult

    record = verified_record()
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *_a, **_kw: record)
    fake = _fake_chat()
    fake._parsed = ParsedInput(intent="combat", target="Goblin")
    fake._adj = AdjudicationResult(success=True, mechanical_summary="")
    return record


@pytest.mark.asyncio
async def test_sync_check_roll_reloads_the_original_dice(athletics_check, api_client) -> None:
    sid = (await _seed(api_client))["session_id"]

    turn = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I force the gate"})
    assert turn.status_code == 200, turn.text
    body = turn.json()
    assert body["roll_status"] == "captured"
    live = body["roll"]
    assert live["turn_id"] and live["dice_log"]
    assert live["dice_result"] == body["adjudication"]["dice_result"]

    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["roll_status"] == "captured"
    # Equality with the live turn's record proves reload read the stored dice, not a new roll.
    assert dm["roll"] == live
    assert dm["turn_id"] == live["turn_id"]

    again = await _history(api_client, sid)
    assert _dm_messages(again)[0]["roll"] == live


@pytest.mark.asyncio
async def test_sse_check_roll_matches_reload(athletics_check, api_client) -> None:
    sid = (await _seed(api_client))["session_id"]

    stream = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I force the gate"}
    )
    events = _sse_events(stream.text)
    assert events[-1][0] == "done", stream.text
    done = events[-1][1]
    adjudication = next(data for name, data in events if name == "adjudication")
    assert done["roll_status"] == "captured"
    assert done["roll"]["dice_result"] == adjudication["dice_result"]
    assert done["roll"]["dice_log"][0]["rolls"] == adjudication["dice_log"][0]["rolls"]

    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["roll"] == done["roll"]


@pytest.mark.asyncio
async def test_combat_round_roll_reloads_with_its_turn(goblin_fight, api_client) -> None:
    sid = (await _seed(api_client))["session_id"]

    turn = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I attack the goblin!"}
    )
    assert turn.status_code == 200, turn.text
    body = turn.json()
    assert body["roll_status"] == "captured"
    assert body["roll"]["dice_result"] == body["adjudication"]["dice_result"]

    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["roll"] == body["roll"]


@pytest.mark.asyncio
async def test_legacy_narration_without_status_is_not_captured(api_client) -> None:
    import db.postgres.session as sm

    sid = (await _seed(api_client))["session_id"]
    async with sm.async_session_factory() as s:
        s.add(
            EventLog(
                session_id=sid,
                event_type="narration",
                payload={"player_input": "", "narration": "An old tale.", "turn_id": "legacy"},
            )
        )
        await s.commit()

    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["content"] == "An old tale."
    assert dm["roll_status"] == "not_captured"
    assert dm["roll"] is None


@pytest.mark.asyncio
async def test_history_never_returns_another_sessions_rolls(athletics_check, api_client) -> None:
    seeded = await _seed(api_client)
    sid = seeded["session_id"]
    other = await api_client.post("/api/sessions", json={"campaign_id": seeded["campaign_id"]})
    assert other.status_code == 200, other.text
    other_id = other.json()["id"]

    turn = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I force the gate"})
    assert turn.status_code == 200, turn.text

    assert _dm_messages(await _history(api_client, other_id)) == []
    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["roll"] is not None
