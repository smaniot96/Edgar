"""EDG-026 over the API: rest turns store their evidence, reload shows it, and manual edits keep the
Hit Dice ledger.

Needs Postgres (testcontainers), so these run in hosted CI. They compare stored values with what
the live turn returned, and never assume particular dice beyond the engine's own bounds.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult
from db.postgres.models import CharacterAssignment, Session as SessionModel
from tests.api.test_turn_pipeline import _fake_chat, _seed


def _dm_messages(history: list[dict]) -> list[dict]:
    return [m for m in history if m["role"] == "dm"]


async def _history(api_client, session_id: int) -> list[dict]:
    response = await api_client.get(f"/api/sessions/{session_id}/messages")
    assert response.status_code == 200, response.text
    return response.json()


async def _turn(api_client, session_id: int, message: str) -> dict:
    # A fresh ruling per turn: the fake returns the same object, which enforcement mutates.
    _fake_chat()._adj = AdjudicationResult(success=True, mechanical_summary="You rest.")
    response = await api_client.post(f"/api/sessions/{session_id}/turn", json={"message": message})
    assert response.status_code == 200, response.text
    return response.json()


async def _set_assignment(session_id: int, *, hp_current: int, stats: dict | None = None) -> None:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()
        ass.hp_current = hp_current
        if stats is not None:
            ass.stats = {**(ass.stats or {}), **stats}
        await s.commit()


async def _stored_stats(session_id: int) -> dict:
    import db.postgres.session as sm

    async with sm.async_session_factory() as s:
        sess = (
            await s.execute(select(SessionModel).where(SessionModel.id == session_id))
        ).scalar_one()
        ass = (
            await s.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == sess.active_character_id,
                    CharacterAssignment.campaign_id == sess.campaign_id,
                )
            )
        ).scalar_one()
        return dict(ass.stats or {})


@pytest.mark.asyncio
async def test_short_rest_evidence_reloads_and_the_last_die_is_refused(
    fake_llm_turn, fake_retrieval, api_client
) -> None:
    sid = (await _seed(api_client))["session_id"]  # a level-1 Fighter: one Hit Die (d10)
    await _set_assignment(sid, hp_current=5)

    body = await _turn(api_client, sid, "I take a short rest")
    assert body["roll_status"] == "captured"
    rest = body["roll"]["rest"]
    assert rest["kind"] == "short" and rest["refused"] is False
    assert rest["die"] == "d10" and rest["expression"] == "1d10+2"
    assert rest["hit_dice_remaining"] == 0 and rest["hit_dice_total"] == 1
    # The HP gained is the roll capped at the 7 HP missing; the character payload agrees.
    assert rest["healed"] == min(rest["rolled"], 7)
    assert body["character"]["hp_current"] == 5 + rest["healed"]
    assert body["character"]["hit_dice"] == {"remaining": 0, "total": 1, "die": "d10"}
    assert body["roll"]["healing"] == rest["healed"]

    (dm,) = _dm_messages(await _history(api_client, sid))
    assert dm["roll"] == body["roll"]  # reload reads the stored evidence, not a new roll

    # With no die left the next rest is refused: no roll, no healing, the same HP.
    again = await _turn(api_client, sid, "I take a short rest")
    assert again["roll"]["rest"]["refused"] is True and again["roll"]["rest"]["healed"] == 0
    assert again["roll"]["dice_log"] == []
    assert again["character"]["hp_current"] == body["character"]["hp_current"]
    assert again["character"]["hit_dice"]["remaining"] == 0

    # A long rest restores HP to the maximum. Half of level 1 rounds down to zero (PHB p.187), so the
    # spent die is not recovered and the ledger is unchanged.
    long_rest = await _turn(api_client, sid, "I sleep for the night")
    assert long_rest["character"]["hp_current"] == 12
    assert long_rest["character"]["hit_dice"] == {"remaining": 0, "total": 1, "die": "d10"}
    assert long_rest["roll"]["rest"]["hit_dice_recovered"] == 0
    assert (await _stored_stats(sid))["hit_dice"] == {"spent": 1}


@pytest.mark.asyncio
async def test_manual_sheet_edit_keeps_the_hit_dice_ledger(api_client) -> None:
    seeded = await _seed(api_client)
    cid, sid = seeded["campaign_id"], seeded["session_id"]
    char_id = (await api_client.get("/api/characters")).json()[0]["id"]

    # A legacy sheet with no key shows its die as available.
    assignments = (await api_client.get(f"/api/characters/{char_id}/assignments")).json()
    active = next(a for a in assignments if a["status"] == "active")
    assert active["hit_dice"] == {"remaining": 1, "total": 1, "die": "d10"}

    await _set_assignment(sid, hp_current=12, stats={"hit_dice": {"spent": 1}})
    patched = await api_client.patch(
        f"/api/campaigns/{cid}/characters/{char_id}", json={"stats": {"STR": 18}}
    )
    assert patched.status_code == 200, patched.text

    assert (await _stored_stats(sid))["hit_dice"] == {"spent": 1}
    assignments = (await api_client.get(f"/api/characters/{char_id}/assignments")).json()
    active = next(a for a in assignments if a["status"] == "active")
    assert active["hit_dice"] == {"remaining": 0, "total": 1, "die": "d10"}
