"""Recovery / fallback guards exercise real disposable coverage and relational data."""

from unittest.mock import AsyncMock

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sqlalchemy import func, select

from apps.api.routers import adventures
from apps.api.services import adventures_meta, campaign_sources
from db.postgres.models import Campaign, EventLog
from db.vector.embeddings import VECTOR_SIZE
from db.vector.source_status import inspect_source


@pytest.fixture
def source_corpus(api_client, monkeypatch):
    q = QdrantClient(":memory:")
    monkeypatch.setattr(
        campaign_sources, "inspect_source", lambda slug, **kw: inspect_source(slug, client=q, **kw)
    )
    monkeypatch.setattr(adventures, "get_qdrant_client", lambda: q)
    yield q
    q.close()


def source(q, slug, texts, *, expected=None, status="ready"):
    q.create_collection(
        slug, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
    )
    if texts:
        q.upsert(
            slug,
            [
                PointStruct(id=i, vector=[1.0] + [0.0] * (VECTOR_SIZE - 1), payload={"text": t})
                for i, t in enumerate(texts)
            ],
        )
    adventures_meta.save_meta(slug, {"status": status, "chunks": expected or len(texts)})


async def test_orphan_selection_and_all_play_entrypoints_are_guarded(
    api_client, source_corpus, db_session, monkeypatch
):
    seed = (await api_client.post("/api/seed")).json()
    cid, sid = seed["campaign_id"], seed["session_id"]
    campaign = (await api_client.get(f"/api/campaigns/{cid}")).json()
    assert campaign["retrieval_degraded"] and not campaign["play_allowed"]
    assert campaign["sources"][0]["status"] == "missing"
    assert not campaign["sources"][0]["source_pdf_available"]
    slug = campaign["adventure_collections"][0]
    selected = await api_client.post(f"/api/adventures/{slug}/campaign")
    assert selected.status_code == 200 and selected.json()["id"] == cid
    assert selected.json()["retrieval_degraded"]
    before = await db_session.scalar(select(func.count(EventLog.id)))
    runner = AsyncMock()
    monkeypatch.setattr("apps.api.routers.session.run_turn", runner)
    for path, body in [
        ("/sessions", {"campaign_id": cid}),
        (f"/sessions/{sid}/intro", None),
        (f"/sessions/{sid}/turn", {"message": "I look around"}),
        (f"/sessions/{sid}/turn/stream", {"message": "I look around"}),
    ]:
        response = await api_client.post("/api" + path, json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "campaign_sources_unavailable"
        assert response.json()["detail"]["retrieval_degraded"]
    assert await db_session.scalar(select(func.count(EventLog.id))) == before
    runner.assert_not_called()


async def test_creation_and_binding_changes_require_ready_coverage(api_client, source_corpus):
    await api_client.post("/api/seed")
    source(source_corpus, "usable", ["The adventure begins"], expected=2)
    source(source_corpus, "complete", ["The complete adventure"])
    body = {
        "title": "New",
        "system": "5e",
        "adventure_collections": ["usable", "absent", "complete"],
    }
    rejected = await api_client.post("/api/campaigns", json=body)
    assert rejected.status_code == 409
    assert [s["status"] for s in rejected.json()["detail"]["sources"]] == [
        "partial",
        "missing",
        "ready",
    ]
    assert (await api_client.post("/api/adventures/absent/campaign")).status_code == 409
    body["adventure_collections"] = ["complete"]
    created = (await api_client.post("/api/campaigns", json=body)).json()
    assert created["play_allowed"] and not created["retrieval_degraded"]
    rejected = await api_client.patch(
        f"/api/campaigns/{created['id']}", json={"adventure_collections": ["absent"]}
    )
    assert rejected.status_code == 409
    assert (await api_client.get(f"/api/campaigns/{created['id']}")).json()[
        "adventure_collections"
    ] == ["complete"]
    adventure = (await api_client.get("/api/adventures/usable")).json()
    assert adventure["status"] == "partial" and adventure["usable_chunks"] == 1


async def test_explicit_homebrew_preserves_bindings_and_empty_means_no_module(
    api_client, source_corpus, monkeypatch
):
    seed = (await api_client.post("/api/seed")).json()
    cid = seed["campaign_id"]
    before = (await api_client.get(f"/api/campaigns/{cid}")).json()["adventure_collections"]
    chosen = (
        await api_client.patch(f"/api/campaigns/{cid}", json={"source_mode": "homebrew"})
    ).json()
    assert chosen["play_allowed"] and chosen["retrieval_degraded"]
    assert chosen["adventure_collections"] == before
    # Use the endpoint's DB fixture through the app dependency via a captured runner.
    runner = AsyncMock(return_value={"narration": "A homebrew scene"})
    monkeypatch.setattr("apps.api.routers.session.run_turn", runner)
    response = await api_client.post(
        f"/api/sessions/{seed['session_id']}/turn", json={"message": "Explore"}
    )
    assert response.status_code == 200
    assert runner.call_args.args[0]["adventure_collections"] == []
    assert runner.call_args.args[0]["source_mode"] == "homebrew"
    assert (
        await api_client.patch(f"/api/campaigns/{cid}", json={"source_mode": "module"})
    ).status_code == 409
    body = {"title": "Homebrew", "system": "5e", "adventure_collections": []}
    assert (await api_client.post("/api/campaigns", json=body)).status_code == 409
    body["source_mode"] = "homebrew"
    fresh = (await api_client.post("/api/campaigns", json=body)).json()
    assert fresh["adventure_collections"] == [] and fresh["play_allowed"]
    assert (
        await api_client.post("/api/sessions", json={"campaign_id": fresh["id"]})
    ).status_code == 200
    body["adventure_collections"] = ["../unsafe"]
    assert (await api_client.post("/api/campaigns", json=body)).status_code == 422


async def test_reupload_restores_exact_bound_slug_and_preserves_history(
    api_client, source_corpus, db_session, monkeypatch
):
    seed = (await api_client.post("/api/seed")).json()
    cid = seed["campaign_id"]
    old = (await api_client.get(f"/api/campaigns/{cid}")).json()
    slug = old["adventure_collections"][0]
    source(source_corpus, "unrelated", ["Do not change this"])
    unrelated = source_corpus.scroll("unrelated", with_vectors=True)[0]
    before = await db_session.scalar(select(func.count(EventLog.id)))
    ingests = []

    def ingest(target, path, *, attempt_id, source_filename):
        pending = adventures_meta.load_meta(target)
        assert pending["status"] == "processing"
        candidate = adventures_meta.candidate_pdf_path(target, attempt_id)
        assert str(candidate) == path
        assert candidate.read_bytes() == b"%PDF supplied source"
        assert not adventures._PDF_DIR.joinpath(f"{target}.pdf").exists()
        assert source_filename == "Original adventure.pdf"
        ingests.append((target, path))
        source(source_corpus, target, ["Restored opening"], expected=1)
        # This route/history test substitutes indexing, then completes the real source
        # publication lifecycle. Real extraction/index activation has separate coverage.
        adventures_meta.save_meta(target, pending)
        assert adventures_meta.finish_attempt(
            target, attempt_id, {"status": "ready", "chunks": 1}, success=True
        )

    monkeypatch.setattr(adventures, "_ingest_adventure", ingest)
    uploaded = await api_client.post(
        f"/api/campaigns/{cid}/sources/{slug}/upload",
        files={"file": ("Original adventure.pdf", b"%PDF supplied source", "application/pdf")},
    )
    assert uploaded.status_code == 202 and uploaded.json()["slug"] == slug
    assert len(ingests) == 1
    restored = (await api_client.get(f"/api/campaigns/{cid}")).json()
    assert restored["adventure_collections"] == old["adventure_collections"]
    assert restored["source_mode"] == "module" and restored["play_allowed"]
    assert restored["sources"][0]["source_pdf_available"]
    assert adventures._PDF_DIR.joinpath(f"{slug}.pdf").read_bytes() == b"%PDF supplied source"
    assert adventures_meta.load_meta(slug)["source_filename"] == "Original adventure.pdf"
    assert await db_session.scalar(select(func.count(EventLog.id))) == before
    assert source_corpus.scroll("unrelated", with_vectors=True)[0] == unrelated
    assert (
        await api_client.post(
            f"/api/campaigns/{cid}/sources/unbound/upload", files={"file": ("x.pdf", b"%PDF x")}
        )
    ).status_code == 422
    assert (
        await api_client.post(
            f"/api/campaigns/{cid}/sources/{slug}/upload", files={"file": ("x.pdf", b"%PDF x")}
        )
    ).status_code == 409


async def test_rejected_reupload_keeps_retained_pdf_and_retry_never_invents_missing_source(
    api_client, source_corpus, monkeypatch
):
    seed = (await api_client.post("/api/seed")).json()
    cid = seed["campaign_id"]
    slug = (await api_client.get(f"/api/campaigns/{cid}")).json()["adventure_collections"][0]
    adventures_meta.save_meta(slug, {"status": "ready", "chunks": 5})
    assert (await api_client.post(f"/api/adventures/{slug}/retry")).status_code == 410
    adventures._PDF_DIR.mkdir(parents=True)
    retained = adventures._PDF_DIR / f"{slug}.pdf"
    retained.write_bytes(b"%PDF retained")
    rejected = await api_client.post(
        f"/api/campaigns/{cid}/sources/{slug}/upload", files={"file": ("x.pdf", b"")}
    )
    assert rejected.status_code == 400
    assert retained.read_bytes() == b"%PDF retained"
    assert adventures_meta.load_meta(slug)["status"] == "ready"
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *_, **_kw: None)
    assert (await api_client.post(f"/api/adventures/{slug}/retry")).status_code == 202
    assert adventures_meta.load_meta(slug)["status"] == "processing"
    assert (
        await api_client.post(
            f"/api/campaigns/{cid}/sources/{slug}/upload", files={"file": ("x.pdf", b"%PDF x")}
        )
    ).status_code == 409


async def test_legacy_invalid_binding_remains_visible(api_client, source_corpus, db_session):
    seed = (await api_client.post("/api/seed")).json()
    campaign = await db_session.get(Campaign, seed["campaign_id"])
    campaign.adventure_collections = ["../legacy"]
    await db_session.commit()
    result = await api_client.get("/api/campaigns")
    assert result.status_code == 200
    assert result.json()[0]["sources"][0]["status"] == "unavailable"
    assert not result.json()[0]["play_allowed"]


async def test_intro_requires_review_before_module_play_and_allows_explicit_homebrew(
    api_client, source_corpus, monkeypatch
):
    from langchain_core.messages import AIMessage

    from apps.api.services import session_intro

    seed = (await api_client.post("/api/seed")).json()
    cid, sid = seed["campaign_id"], seed["session_id"]
    slug = (await api_client.get(f"/api/campaigns/{cid}")).json()["adventure_collections"][0]
    source(source_corpus, slug, ["The actual module text"])
    model = AsyncMock()
    model.ainvoke.return_value = AIMessage(content="A chosen homebrew opening")
    monkeypatch.setattr(session_intro, "make_chat_model", lambda **kw: model)
    response = await api_client.post(f"/api/sessions/{sid}/intro")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adventure_manifest_review_required"
    assert response.json()["detail"]["source"] == slug
    model.ainvoke.assert_not_called()
    await api_client.patch(f"/api/campaigns/{cid}", json={"source_mode": "homebrew"})
    response = await api_client.post(f"/api/sessions/{sid}/intro")
    assert response.status_code == 200 and response.json()["started"]
    assert "explicitly chose homebrew" in model.ainvoke.call_args.args[0][-1].content


@pytest.mark.parametrize("status", ["processing", "failed"])
async def test_ingestion_lifecycle_never_counts_as_ready_despite_usable_points(
    api_client, source_corpus, status
):
    await api_client.post("/api/seed")
    source(source_corpus, "unfinished", ["An indexed chunk"], status=status)
    response = await api_client.post(
        "/api/campaigns",
        json={"title": "Wait", "system": "5e", "adventure_collections": ["unfinished"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["sources"][0]["status"] == status


async def test_source_disappears_after_preflight_sync_and_stream_report_degraded_without_saving(
    api_client, source_corpus, db_session, monkeypatch, fake_llm_turn, fake_retrieval
):
    from agent.nodes import world_retriever

    seed = (await api_client.post("/api/seed")).json()
    cid, sid = seed["campaign_id"], seed["session_id"]
    slug = (await api_client.get(f"/api/campaigns/{cid}")).json()["adventure_collections"][0]
    before = await db_session.scalar(select(func.count(EventLog.id)))

    def removed_after_preflight(name, **kw):
        source_corpus.delete_collection(name)
        return inspect_source(name, client=source_corpus)

    monkeypatch.setattr(world_retriever, "inspect_source", removed_after_preflight)
    for suffix in ("turn", "turn/stream"):
        source(source_corpus, slug, ["Ready before the turn starts"])
        response = await api_client.post(
            f"/api/sessions/{sid}/{suffix}", json={"message": "Explore"}
        )
        if suffix == "turn":
            assert response.status_code == 409
            assert response.json()["code"] == "campaign_sources_unavailable"
            assert response.json()["retrieval_degraded"]
        else:
            assert response.status_code == 200
            assert '"code": "campaign_sources_unavailable"' in response.text
            assert '"retrieval_degraded": true' in response.text
            assert "event: token" not in response.text and "event: done" not in response.text
        assert await db_session.scalar(select(func.count(EventLog.id))) == before
