"""Deleting a campaign from the campaign list (EDG-055 F16).

The list card deletes the campaign and its saves, then releases each bound adventure source that
no other campaign uses and that may still change. Uses disposable in-memory Qdrant data.
"""

from datetime import UTC, datetime

import httpx
import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, VectorParams
from sqlalchemy import func, select

from apps.api.auth import generate_token, hash_token
from apps.api.routers import adventures
from apps.api.services import adventures_meta
from db.postgres.models import ApiToken, Campaign, Session, User
from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS
from db.vector.embeddings import VECTOR_SIZE

CONFIRM = {"X-Confirm-Delete": "yes"}
RELEASE = "?remove_unused_sources=true"


@pytest.fixture
def qdrant(api_client, monkeypatch):
    q = QdrantClient(":memory:")
    monkeypatch.setattr(adventures, "get_qdrant_client", lambda: q)
    yield q
    q.close()


def add_source(q: QdrantClient, slug: str, **meta) -> None:
    q.create_collection(
        slug, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
    )
    adventures_meta.save_meta(slug, {"status": "ready", "chunks": 1, **meta})


async def add_campaign(db, slugs: list[str], *, owner_id: int, saves: int = 0) -> int:
    campaign = Campaign(
        title="Velmoor", system="D&D 5e", created_by=owner_id, adventure_collections=slugs
    )
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    for _ in range(saves):
        db.add(Session(campaign_id=campaign.id, started_at=datetime.now(UTC)))
    await db.commit()
    return campaign.id


async def owner(api_client) -> int:
    return (await api_client.post("/api/seed")).json()["user_id"]


def has_source(q: QdrantClient, slug: str) -> bool:
    return q.collection_exists(slug) or adventures_meta.meta_path(slug).is_file()


async def test_deletes_campaign_saves_and_its_unused_source(api_client, qdrant, db_session):
    add_source(qdrant, "velmoor")
    cid = await add_campaign(db_session, ["velmoor"], owner_id=await owner(api_client), saves=2)

    resp = await api_client.delete(f"/api/campaigns/{cid}{RELEASE}", headers=CONFIRM)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "campaign_id": cid,
        "sources": [{"slug": "velmoor", "outcome": "removed"}],
    }
    assert (await api_client.get(f"/api/campaigns/{cid}")).status_code == 404
    assert (
        await db_session.scalar(select(func.count(Session.id)).where(Session.campaign_id == cid))
        == 0
    )
    assert not has_source(qdrant, "velmoor")


async def test_shared_source_stays_until_its_last_campaign_is_deleted(
    api_client, qdrant, db_session
):
    add_source(qdrant, "velmoor")
    uid = await owner(api_client)
    first = await add_campaign(db_session, ["velmoor"], owner_id=uid)
    second = await add_campaign(db_session, ["velmoor"], owner_id=uid)

    kept = await api_client.delete(f"/api/campaigns/{first}{RELEASE}", headers=CONFIRM)
    assert kept.json()["sources"] == [{"slug": "velmoor", "outcome": "in_use"}]
    assert has_source(qdrant, "velmoor")

    removed = await api_client.delete(f"/api/campaigns/{second}{RELEASE}", headers=CONFIRM)
    assert removed.json()["sources"] == [{"slug": "velmoor", "outcome": "removed"}]
    assert not has_source(qdrant, "velmoor")


@pytest.mark.parametrize(
    ("meta", "outcome"),
    [
        ({"private_preparation": True, "prepared_bundle": {"digest": "a" * 64}}, "retained"),
        ({"status": "processing"}, "processing"),
        ({"job": {"state": "running"}}, "processing"),
    ],
)
async def test_immutable_or_busy_sources_are_kept(api_client, qdrant, db_session, meta, outcome):
    add_source(qdrant, "velmoor", **meta)
    cid = await add_campaign(db_session, ["velmoor"], owner_id=await owner(api_client), saves=1)

    resp = await api_client.delete(f"/api/campaigns/{cid}{RELEASE}", headers=CONFIRM)

    assert resp.status_code == 200
    assert resp.json()["sources"] == [{"slug": "velmoor", "outcome": outcome}]
    assert (await api_client.get(f"/api/campaigns/{cid}")).status_code == 404
    assert qdrant.collection_exists("velmoor")


async def test_campaigns_without_a_source_record_can_be_deleted(api_client, qdrant, db_session):
    """The F16 dead end: a homebrew campaign, or one whose source is already gone, used to 404."""
    uid = await owner(api_client)
    orphan = await add_campaign(db_session, ["already_deleted"], owner_id=uid, saves=1)
    homebrew = await add_campaign(db_session, [], owner_id=uid)

    orphan_resp = await api_client.delete(f"/api/campaigns/{orphan}{RELEASE}", headers=CONFIRM)
    homebrew_resp = await api_client.delete(f"/api/campaigns/{homebrew}{RELEASE}", headers=CONFIRM)

    assert orphan_resp.json()["sources"] == [{"slug": "already_deleted", "outcome": "missing"}]
    assert homebrew_resp.json() == {"campaign_id": homebrew, "sources": []}
    assert (
        await db_session.scalar(
            select(func.count(Campaign.id)).where(Campaign.id.in_([orphan, homebrew]))
        )
        == 0
    )


async def test_bundled_module_is_never_removed_with_a_campaign(api_client, qdrant, db_session):
    slug = DEFAULT_ADVENTURE_COLLECTIONS[0]
    add_source(qdrant, slug)
    cid = await add_campaign(db_session, [slug], owner_id=await owner(api_client))

    resp = await api_client.delete(f"/api/campaigns/{cid}{RELEASE}", headers=CONFIRM)

    assert resp.json()["sources"] == [{"slug": slug, "outcome": "built_in"}]
    assert has_source(qdrant, slug)


async def test_source_release_requires_the_confirmation_header(api_client, qdrant, db_session):
    add_source(qdrant, "velmoor")
    cid = await add_campaign(db_session, ["velmoor"], owner_id=await owner(api_client))

    resp = await api_client.delete(f"/api/campaigns/{cid}{RELEASE}")

    assert resp.status_code == 400
    assert "X-Confirm-Delete" in resp.json()["detail"]
    assert (await api_client.get(f"/api/campaigns/{cid}")).status_code == 200
    assert has_source(qdrant, "velmoor")


async def test_plain_campaign_delete_keeps_its_204_contract(api_client, qdrant, db_session):
    add_source(qdrant, "velmoor")
    cid = await add_campaign(db_session, ["velmoor"], owner_id=await owner(api_client))

    resp = await api_client.delete(f"/api/campaigns/{cid}")

    assert resp.status_code == 204 and resp.content == b""
    assert has_source(qdrant, "velmoor")


async def test_vector_store_failure_keeps_the_source_but_not_the_campaign(
    api_client, qdrant, db_session, monkeypatch
):
    add_source(qdrant, "velmoor")
    cid = await add_campaign(db_session, ["velmoor"], owner_id=await owner(api_client))

    def fail(_slug):
        raise UnexpectedResponse(500, "error", b"", httpx.Headers())

    monkeypatch.setattr(qdrant, "delete_collection", fail)
    resp = await api_client.delete(f"/api/campaigns/{cid}{RELEASE}", headers=CONFIRM)

    assert resp.json()["sources"] == [{"slug": "velmoor", "outcome": "unavailable"}]
    assert (await api_client.get(f"/api/campaigns/{cid}")).status_code == 404
    assert has_source(qdrant, "velmoor")


async def test_non_admin_owner_deletes_the_campaign_but_not_the_shared_source(
    api_client, qdrant, db_session, monkeypatch
):
    admin, player = User(email="operator@example.test"), User(email="player@example.test")
    db_session.add_all([admin, player])
    await db_session.commit()
    token = generate_token()
    db_session.add(ApiToken(user_id=player.id, token_hash=hash_token(token), label="test"))
    await db_session.commit()
    monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", str(admin.id))
    add_source(qdrant, "velmoor")
    cid = await add_campaign(db_session, ["velmoor"], owner_id=player.id)

    resp = await api_client.delete(
        f"/api/campaigns/{cid}{RELEASE}", headers={**CONFIRM, "Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    assert resp.json()["sources"] == [{"slug": "velmoor", "outcome": "not_permitted"}]
    assert has_source(qdrant, "velmoor")


async def test_adventure_delete_still_requires_its_confirmation_header(api_client, qdrant):
    add_source(qdrant, "velmoor")

    assert (await api_client.delete("/api/adventures/velmoor")).status_code == 400
    assert (await api_client.delete("/api/adventures/velmoor", headers=CONFIRM)).status_code == 204
    assert not has_source(qdrant, "velmoor")
