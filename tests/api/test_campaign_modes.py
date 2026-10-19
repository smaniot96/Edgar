"""EDG-045: campaign progression and death modes are explicit, defaulted and validated over the API.

Existing campaigns (and any campaign created without the fields) keep the behaviour they had before
the modes existed: progression 'fixed' and death 'subdued'. The modes are stored and returned; the
engine reads them per turn (see the session loader and agent.progression).
"""

from __future__ import annotations

import pytest


async def _uid(api_client) -> int:
    return (await api_client.post("/api/seed")).json()["user_id"]


@pytest.mark.asyncio
async def test_campaign_modes_default_to_the_existing_behaviour(api_client) -> None:
    uid = await _uid(api_client)
    created = await api_client.post(
        "/api/campaigns", json={"title": "Modes", "system": "5e", "created_by": uid}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # The defaults are the behaviour every campaign had before the modes (explicit in the response).
    assert body["progression_mode"] == "fixed"
    assert body["death_mode"] == "subdued"
    cid = body["id"]

    one = await api_client.get(f"/api/campaigns/{cid}")
    assert one.json()["progression_mode"] == "fixed"
    assert one.json()["death_mode"] == "subdued"

    # An explicit choice at creation is stored as chosen.
    chosen = await api_client.post(
        "/api/campaigns",
        json={
            "title": "Chosen",
            "system": "5e",
            "progression_mode": "milestone",
            "death_mode": "death_saves",
        },
    )
    assert chosen.status_code == 201, chosen.text
    assert chosen.json()["progression_mode"] == "milestone"
    assert chosen.json()["death_mode"] == "death_saves"


@pytest.mark.asyncio
async def test_mode_patch_is_explicit_and_validated(api_client) -> None:
    uid = await _uid(api_client)
    cid = (
        await api_client.post(
            "/api/campaigns", json={"title": "Switch", "system": "5e", "created_by": uid}
        )
    ).json()["id"]

    patched = await api_client.patch(
        f"/api/campaigns/{cid}",
        json={"progression_mode": "milestone", "death_mode": "death_saves"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["progression_mode"] == "milestone"
    assert patched.json()["death_mode"] == "death_saves"

    # Unknown values are refused, not coerced, and nulls are not a way to reset a mode.
    bad = await api_client.patch(f"/api/campaigns/{cid}", json={"death_mode": "instant"})
    assert bad.status_code == 422
    nulled = await api_client.patch(f"/api/campaigns/{cid}", json={"progression_mode": None})
    assert nulled.status_code == 422

    # A patch that names neither mode leaves both as they are.
    renamed = await api_client.patch(f"/api/campaigns/{cid}", json={"title": "Renamed"})
    assert renamed.json()["progression_mode"] == "milestone"
    assert renamed.json()["death_mode"] == "death_saves"


@pytest.mark.asyncio
async def test_ended_campaign_cannot_change_modes(api_client) -> None:
    uid = await _uid(api_client)
    cid = (
        await api_client.post(
            "/api/campaigns", json={"title": "Ended", "system": "5e", "created_by": uid}
        )
    ).json()["id"]
    ended = await api_client.post(f"/api/campaigns/{cid}/end")
    assert ended.status_code == 200, ended.text

    refused = await api_client.patch(f"/api/campaigns/{cid}", json={"death_mode": "death_saves"})
    assert refused.status_code == 409
    # Other fields still patch on an ended campaign, as before.
    renamed = await api_client.patch(f"/api/campaigns/{cid}", json={"title": "Still ended"})
    assert renamed.status_code == 200
    assert renamed.json()["death_mode"] == "subdued"
