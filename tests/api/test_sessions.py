import pytest


@pytest.mark.asyncio
async def test_create_session(api_client) -> None:
    seed = await api_client.post("/api/seed")
    camp_id = seed.json()["campaign_id"]

    r = await api_client.post("/api/sessions", json={"campaign_id": camp_id})
    assert r.status_code == 200
    sid = r.json()["id"]

    got = await api_client.get(f"/api/sessions/{sid}")
    assert got.status_code == 200
    assert got.json()["campaign_id"] == camp_id


@pytest.mark.asyncio
async def test_delete_session(api_client) -> None:
    seed = await api_client.post("/api/seed")
    camp_id = seed.json()["campaign_id"]
    r = await api_client.post("/api/sessions", json={"campaign_id": camp_id})
    sid = r.json()["id"]

    d = await api_client.delete(f"/api/sessions/{sid}")
    assert d.status_code == 204
    assert (await api_client.get(f"/api/sessions/{sid}")).status_code == 404
