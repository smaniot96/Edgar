from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.main import mount_frontend_routes


@pytest.mark.asyncio
async def test_asgi_serves_cover_from_ui_base_and_nested_routes(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    shell = b"<!doctype html><html><body>spa-shell</body></html>"
    cover = b'<svg xmlns="http://www.w3.org/2000/svg"><text>EDG129 fixture</text></svg>'
    (dist / "index.html").write_bytes(shell)
    (assets / "edg129-fixture.svg").write_bytes(cover)

    api = FastAPI()

    @api.get("/api/cover-fixture")
    async def api_cover_fixture():
        return {"path": "/api/cover-fixture"}

    mount_frontend_routes(api, dist)

    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        root = await client.get("/ui/")
        nested = await client.get("/ui/campaigns/17")
        served_cover = await client.get("/ui/assets/edg129-fixture.svg")
        missing_cover = await client.get("/ui/assets/missing.svg")
        api_root = await client.get("/api/cover-fixture")
        unbased_asset = await client.get("/assets/edg129-fixture.svg")

    assert root.status_code == 200 and root.content == shell
    assert nested.status_code == 200 and nested.content == shell
    assert served_cover.status_code == 200 and served_cover.content == cover
    assert served_cover.headers["content-type"].startswith("image/svg+xml")
    assert missing_cover.status_code == 404
    assert api_root.status_code == 200 and api_root.json() == {"path": "/api/cover-fixture"}
    assert unbased_asset.status_code == 404
