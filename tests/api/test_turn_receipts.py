"""Lost acknowledgments resolve through the original atomic turn receipt."""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api.dependencies import acquire_turn_lock
from apps.api.schemas.turn import TurnRequest
from db.postgres.models import EventLog


def test_request_identity_is_optional_bounded_and_not_silently_discarded():
    assert TurnRequest(message="Wait.").request_id is None
    assert TurnRequest(message="Wait.", request_id="request-123").request_id == "request-123"
    for invalid in ("", "../another-session", "a" * 101, "contains spaces"):
        with pytest.raises(ValidationError):
            TurnRequest(message="Wait.", request_id=invalid)


@pytest.mark.parametrize("first_stream", [False, True])
async def test_lost_ack_replay_never_runs_provider_or_repeats_state(
    api_client, fake_llm_schema, fake_retrieval, monkeypatch, first_stream
):
    import db.postgres.session as sm

    sid = (await api_client.post("/api/seed")).json()["session_id"]
    body = {"message": "I inspect the trail.", "request_id": "stable-action-123"}
    suffix = "/stream" if first_stream else ""
    first = await api_client.post(f"/api/sessions/{sid}/turn{suffix}", json=body)
    assert first.status_code == 200, first.text
    if first_stream:
        done = json.loads(first.text.strip().split("\n\n")[-1].split("data: ", 1)[1])
    else:
        done = first.json()

    # Simulate the client losing the response after the authoritative commit. Both
    # provider entry points are forbidden from now on, including receipt reads/replays.
    async def forbidden(*args, **kwargs):
        raise AssertionError("Receipt replay attempted generation")

    monkeypatch.setattr("apps.api.routers.session.run_turn", forbidden)
    monkeypatch.setattr("apps.api.routers.session._load_turn_initial_state", forbidden)
    receipt = await api_client.get(f"/api/sessions/{sid}/turns/{body['request_id']}")
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["status"] == "committed"
    assert receipt.json()["result"]["narration"] == done["narration"]
    for replay_suffix in ("", "/stream"):
        replay = await api_client.post(f"/api/sessions/{sid}/turn{replay_suffix}", json=body)
        assert replay.status_code == 200, replay.text
        assert done["narration"] in replay.text
        if replay_suffix:
            assert replay.text.count("event: done") == 1
            assert "event: token" not in replay.text
    conflict = await api_client.post(
        f"/api/sessions/{sid}/turn", json={**body, "message": "A different action."}
    )
    assert conflict.status_code == 409
    assert "turn_identity_conflict" in conflict.text
    async with sm.async_session_factory() as db:
        assert await db.scalar(
            select(func.count()).select_from(EventLog).where(
                EventLog.session_id == sid, EventLog.event_type == "narration"
            )
        ) == 1


async def test_receipt_absence_is_not_reported_while_turn_is_active(
    api_client, redis_client
):
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    lock = await acquire_turn_lock(sid, redis_client)
    try:
        busy = await api_client.get(f"/api/sessions/{sid}/turns/pending-action")
        assert busy.status_code == 409
    finally:
        await lock.release()
    absent = await api_client.get(f"/api/sessions/{sid}/turns/pending-action")
    assert absent.status_code == 404
    other_session = await api_client.get(f"/api/sessions/{sid + 1}/turns/pending-action")
    assert other_session.status_code == 404


async def test_receipts_do_not_cross_playthroughs(api_client, fake_llm_schema, fake_retrieval):
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    session = (await api_client.get(f"/api/sessions/{sid}")).json()
    second = (await api_client.post("/api/sessions", json={"campaign_id": session["campaign_id"]})).json()["id"]
    response = await api_client.post(
        f"/api/sessions/{sid}/turn",
        json={"message": "I inspect the trail.", "request_id": "isolated-request"},
    )
    assert response.status_code == 200, response.text
    assert (await api_client.get(f"/api/sessions/{second}/turns/isolated-request")).status_code == 404
