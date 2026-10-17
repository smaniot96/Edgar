"""Developer turn traces across reload (EDG-138) through the real turn paths.

A captured trace commits with its turn, is written only for a debug request while EDGAR_DEBUG is on,
is not duplicated by a retry, and reloads with its companions matched by turn_id. Needs Postgres
(testcontainers), so these run in hosted CI; they assert shape and linkage, not particular dice.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from agent.models.parsed_input import ParsedInput
from agent.turn_trace import TURN_TRACE_EVENT
from db.postgres.models import EventLog
from tests.api.test_turn_pipeline import _fake_chat, _seed, _sse_events

# Secret-looking prose only (not token-shaped): it must never reach a stored or returned trace.
PROVIDER_ERROR = "provider said: connection refused at 10.0.0.7"
POLICY_SELECTOR = "adventure-reviewed-policy-7f3a"
RECALL_SELECTOR = "recall-fact-review-91c2"


@pytest.fixture
def traced_fake(fake_llm_turn, fake_retrieval):
    fake = _fake_chat()
    fake._parsed = ParsedInput(
        intent="exploration",
        check="perception",
        difficulty="easy",
        retrieval_query="notice a hidden door",
        resolution_policy_id=POLICY_SELECTOR,
        recall_fact_id=RECALL_SELECTOR,
        entities={"note": {"raw": PROVIDER_ERROR}, "npc": "Mara"},
    )
    return fake


async def _rows(session_id: int, event_type: str) -> list[dict]:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        rows = (
            (
                await s.execute(
                    select(EventLog)
                    .where(EventLog.session_id == session_id, EventLog.event_type == event_type)
                    .order_by(EventLog.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [dict(row.payload) for row in rows]


async def _insert_legacy_narration(session_id: int, turn_id: str) -> None:
    """A narration row written before traces existed: no trace, no companions."""
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        s.add(
            EventLog(
                session_id=session_id,
                event_type="narration",
                payload={
                    "player_input": "I wait.",
                    "narration": "Time passes.",
                    "citations": [],
                    "ruling_citation_ids": [],
                    "rules_edition": "2014",
                    "turn_id": turn_id,
                    "roll_status": "no_roll",
                    "roll": None,
                },
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_sync_debug_turn_is_captured_and_reloads_with_companions(
    traced_fake, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]

    turn = await api_client.post(
        f"/api/sessions/{sid}/turn",
        json={"message": "I search for a hidden door.", "debug": True, "request_id": "trace-sync-1"},
    )
    assert turn.status_code == 200, turn.text

    (row,) = await _rows(sid, TURN_TRACE_EVENT)
    assert row["turn_id"] == "trace-sync-1"

    reloaded = await api_client.get(f"/api/sessions/{sid}/diagnostics/turn-traces")
    assert reloaded.status_code == 200
    body = reloaded.json()
    assert body["scope"] == "dm_turns"
    (entry,) = body["turns"]
    assert entry["turn_id"] == "trace-sync-1"
    trace = entry["trace"]
    assert trace["parsed_input"]["intent"] == "exploration"
    assert trace["parsed_input"]["check"] == "perception"
    assert trace["settings"]["narrator_profile"] == "play.narrator"
    # The companion EDG-053 row is composed by turn_id, not copied into the trace.
    assert entry["turn_diagnostics"]["turn_id"] == "trace-sync-1"
    assert entry["combat_round"] is None
    assert "timings_ms" not in trace

    # The stored row and the reload never carry selectors or the secret-looking provider text.
    for text in (json.dumps(row), json.dumps(body)):
        assert POLICY_SELECTOR not in text
        assert RECALL_SELECTOR not in text
        assert PROVIDER_ERROR not in text
        assert "resolution_policy_id" not in text
        assert "recall_fact_id" not in text

    # The reloaded narration row carries the same turn_id the trace links to.
    messages = (await api_client.get(f"/api/sessions/{sid}/messages")).json()
    dm = [m for m in messages if m["role"] == "dm"]
    assert [m["turn_id"] for m in dm] == ["trace-sync-1"]


@pytest.mark.asyncio
async def test_sse_debug_turn_counts_match_the_live_debug_frame(
    traced_fake, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]
    r = await api_client.post(
        f"/api/sessions/{sid}/turn/stream",
        json={"message": "I search for a hidden door.", "debug": True, "request_id": "trace-sse-1"},
    )
    events = _sse_events(r.text)
    debug = next(data for name, data in events if name == "debug")
    assert events[-1][0] == "done", r.text
    assert "turn_trace" not in r.text

    (entry,) = (await api_client.get(f"/api/sessions/{sid}/diagnostics/turn-traces")).json()["turns"]
    # Same state, same evidence: the stored counts equal what the live frame reported.
    assert entry["trace"]["rag"]["rules"]["count"] == debug["rules_context_count"]
    assert entry["trace"]["rag"]["adventure"]["count"] == debug["adventure_context_count"]
    assert entry["turn_diagnostics"]["path"] == "normal"


@pytest.mark.asyncio
async def test_turn_without_debug_stores_no_trace(traced_fake, api_client, monkeypatch) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]
    turn = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I wait.", "request_id": "trace-player-1"}
    )
    assert turn.status_code == 200
    assert await _rows(sid, TURN_TRACE_EVENT) == []
    # The turn itself is still reloadable; its absence from the trace list is the 'not captured' state.
    body = (await api_client.get(f"/api/sessions/{sid}/diagnostics/turn-traces")).json()
    assert body["turns"] == []


@pytest.mark.asyncio
async def test_debug_request_is_ignored_and_endpoint_hidden_when_developer_debug_is_off(
    traced_fake, api_client, monkeypatch
) -> None:
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    sid = (await _seed(api_client))["session_id"]
    turn = await api_client.post(
        f"/api/sessions/{sid}/turn",
        json={"message": "I search.", "debug": True, "request_id": "trace-off-1"},
    )
    assert turn.status_code == 200
    assert await _rows(sid, TURN_TRACE_EVENT) == []
    assert (await api_client.get(f"/api/sessions/{sid}/diagnostics/turn-traces")).status_code == 404


@pytest.mark.asyncio
async def test_failed_commit_leaves_no_trace_and_retry_writes_exactly_one(
    monkeypatch, traced_fake, api_client
) -> None:
    from apps.api.services import turn_runner

    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]

    async def boom(*_a, **_kw):
        raise RuntimeError(PROVIDER_ERROR)

    original = turn_runner.maybe_complete_campaign
    # The trace is already staged when the commit fails: nothing from the attempt may persist.
    monkeypatch.setattr(turn_runner, "maybe_complete_campaign", boom)
    failed = await api_client.post(
        f"/api/sessions/{sid}/turn/stream",
        json={"message": "I search.", "debug": True, "request_id": "trace-retry-1"},
    )
    assert "done" not in failed.text
    assert PROVIDER_ERROR not in failed.text
    assert await _rows(sid, TURN_TRACE_EVENT) == []
    assert await _rows(sid, "narration") == []

    monkeypatch.setattr(turn_runner, "maybe_complete_campaign", original)
    ok = await api_client.post(
        f"/api/sessions/{sid}/turn",
        json={"message": "I search.", "debug": True, "request_id": "trace-retry-1"},
    )
    assert ok.status_code == 200, ok.text
    # A second delivery of the same committed turn replays the receipt and writes nothing new.
    replay = await api_client.post(
        f"/api/sessions/{sid}/turn",
        json={"message": "I search.", "debug": True, "request_id": "trace-retry-1"},
    )
    assert replay.status_code == 200
    assert len(await _rows(sid, TURN_TRACE_EVENT)) == 1


@pytest.mark.asyncio
async def test_legacy_turn_without_trace_reloads_as_not_captured(
    traced_fake, api_client, monkeypatch
) -> None:
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    sid = (await _seed(api_client))["session_id"]
    await _insert_legacy_narration(sid, "legacy-turn-1")

    messages = (await api_client.get(f"/api/sessions/{sid}/messages")).json()
    assert [m["turn_id"] for m in messages if m["role"] == "dm"] == ["legacy-turn-1"]
    body = (await api_client.get(f"/api/sessions/{sid}/diagnostics/turn-traces")).json()
    assert all(turn["turn_id"] != "legacy-turn-1" for turn in body["turns"])
