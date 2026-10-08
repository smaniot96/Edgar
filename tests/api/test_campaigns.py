import pytest


@pytest.mark.asyncio
async def test_create_list_get_campaign(api_client) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    uid = seed.json()["user_id"]

    created = await api_client.post(
        "/api/campaigns",
        json={"title": "West Marches", "system": "D&D 5e", "created_by": uid},
    )
    assert created.status_code == 201
    body = created.json()
    cid = body["id"]
    assert body["title"] == "West Marches"

    listed = await api_client.get("/api/campaigns")
    assert listed.status_code == 200
    ids = {c["id"] for c in listed.json()}
    assert cid in ids

    one = await api_client.get(f"/api/campaigns/{cid}")
    assert one.status_code == 200
    assert one.json()["id"] == cid


@pytest.mark.asyncio
async def test_patch_delete_campaign(api_client) -> None:
    seed = await api_client.post("/api/seed")
    uid = seed.json()["user_id"]
    created = await api_client.post(
        "/api/campaigns",
        json={"title": "Tmp", "system": "5e", "created_by": uid},
    )
    cid = created.json()["id"]

    patched = await api_client.patch(f"/api/campaigns/{cid}", json={"title": "Renamed"})
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"

    deleted = await api_client.delete(f"/api/campaigns/{cid}")
    assert deleted.status_code == 204
    missing = await api_client.get(f"/api/campaigns/{cid}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_create_campaign_ignores_client_created_by(api_client) -> None:
    """The owner is always the current user; a client-supplied created_by is ignored."""
    seed = await api_client.post("/api/seed")
    uid = seed.json()["user_id"]
    created = await api_client.post(
        "/api/campaigns",
        json={"title": "Spoof", "system": "5e", "created_by": uid + 999},
    )
    assert created.status_code == 201
    assert created.json()["created_by"] == uid


@pytest.mark.asyncio
async def test_list_campaigns_paginates(api_client) -> None:
    await api_client.post("/api/seed")
    for i in range(3):
        await api_client.post("/api/campaigns", json={"title": f"C{i}", "system": "5e"})
    everything = (await api_client.get("/api/campaigns")).json()
    assert len(everything) == 4
    page = await api_client.get("/api/campaigns", params={"limit": 2, "offset": 1})
    assert page.status_code == 200
    assert [c["id"] for c in page.json()] == [c["id"] for c in everything[1:3]]
    assert (await api_client.get("/api/campaigns", params={"limit": 0})).status_code == 422
