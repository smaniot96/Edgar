import pytest


@pytest.mark.asyncio
async def test_character_delete_requires_awaited_delete(api_client) -> None:
    """Regression guard: AsyncSession.delete is awaitable (plan 08 verification)."""
    await api_client.post("/api/seed")
    listed = await api_client.get("/api/characters")
    assert listed.status_code == 200
    rows = listed.json()
    assert rows
    cid = rows[0]["id"]

    d = await api_client.delete(f"/api/characters/{cid}")
    assert d.status_code == 204

    missing = await api_client.get(f"/api/characters/{cid}")
    assert missing.status_code == 404
