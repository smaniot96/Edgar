"""EDG-025: a Second Wind use or rest through the real turn endpoints, sync and SSE.

The seeded character is a level-1 Fighter. The use is declared outside combat, so the engine
resolves it in the linear path and persists the spent count with the turn transaction.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from db.postgres.models import CharacterAssignment


def _sse_events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for frame in text.strip().split("\n\n"):
        name, data = "message", None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        events.append((name, data))
    return events


async def _seed(api_client) -> dict[str, int]:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    return seed.json()


async def _set_remaining(campaign_id: int, remaining: int) -> None:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.campaign_id == campaign_id,
                    CharacterAssignment.ended_at.is_(None),
                )
            )
        ).scalar_one()
        ass.stats = {**(ass.stats or {}), "second_wind": {"uses_remaining": remaining}}
        await s.commit()


async def _stored_remaining(campaign_id: int) -> int:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.campaign_id == campaign_id,
                    CharacterAssignment.ended_at.is_(None),
                )
            )
        ).scalar_one()
        return (ass.stats or {})["second_wind"]["uses_remaining"]


@pytest.mark.asyncio
async def test_second_wind_use_is_persisted_and_refused_when_spent_sync(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seed = await _seed(api_client)
    sid, campaign_id = seed["session_id"], seed["campaign_id"]
    await _set_remaining(campaign_id, 1)

    first = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I use Second Wind"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["character"]["second_wind"] == {"uses_remaining": 0, "uses_max": 1}
    assert await _stored_remaining(campaign_id) == 0

    second = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I use Second Wind"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["character"]["second_wind"] == {"uses_remaining": 0, "uses_max": 1}
    assert await _stored_remaining(campaign_id) == 0

    rest = await api_client.post(
        f"/api/sessions/{sid}/turn", json={"message": "I take a short rest"}
    )
    assert rest.status_code == 200, rest.text
    assert rest.json()["character"]["second_wind"] == {"uses_remaining": 1, "uses_max": 1}
    assert await _stored_remaining(campaign_id) == 1


@pytest.mark.asyncio
async def test_second_wind_use_is_persisted_and_refused_when_spent_stream(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    seed = await _seed(api_client)
    sid, campaign_id = seed["session_id"], seed["campaign_id"]
    await _set_remaining(campaign_id, 1)

    stream = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I use Second Wind"}
    )
    events = _sse_events(stream.text)
    assert events[-1][0] == "done", stream.text
    assert events[-1][1]["character"]["second_wind"] == {"uses_remaining": 0, "uses_max": 1}
    assert await _stored_remaining(campaign_id) == 0

    again = await api_client.post(
        f"/api/sessions/{sid}/turn/stream", json={"message": "I use Second Wind"}
    )
    events = _sse_events(again.text)
    assert events[-1][0] == "done", again.text
    assert events[-1][1]["character"]["second_wind"] == {"uses_remaining": 0, "uses_max": 1}
    assert await _stored_remaining(campaign_id) == 0
