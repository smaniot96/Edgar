"""EDG-182: the campaign's "always confirm" combat setting is stored, defaulted, validated and passed on.

It defaults to off (the confirmation multi-part and conditional turns always get). It is returned on the
campaign, changed by PATCH, and rejects a null. The session loader passes it to the engine state, where
the round asks about every turn that declares an action (agent.graph_combat).
"""

from __future__ import annotations

import pytest


async def _uid(api_client) -> int:
    return (await api_client.post("/api/seed")).json()["user_id"]


@pytest.mark.asyncio
async def test_always_confirm_defaults_off_and_is_patched(api_client) -> None:
    uid = await _uid(api_client)
    created = await api_client.post(
        "/api/campaigns", json={"title": "Confirm", "system": "5e", "created_by": uid}
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert created.json()["combat_always_confirm"] is False

    on = await api_client.patch(f"/api/campaigns/{cid}", json={"combat_always_confirm": True})
    assert on.status_code == 200, on.text
    assert on.json()["combat_always_confirm"] is True
    assert (await api_client.get(f"/api/campaigns/{cid}")).json()["combat_always_confirm"] is True

    null = await api_client.patch(f"/api/campaigns/{cid}", json={"combat_always_confirm": None})
    assert null.status_code == 422, null.text
