"""Fake-provider sync/SSE story continuity, atomic writes, replay and privacy."""

from __future__ import annotations

import json

import pytest

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryRef, story_target_id

NOTE = "The lens tracks reflections, not the sky."
PUBLIC = f"Mara greets you. A Lens note reads “{NOTE}” Spare cord lies beside the Brace."
SECRET = "PRIVATE FUTURE VILLAIN TRUE NAME"


@pytest.fixture
def story_source(monkeypatch, api_client):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS
    from db.vector.embeddings import VECTOR_SIZE
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.manifest import manifest_chunks
    from ingestion.quality import check_text

    q = QdrantClient(":memory:")
    manifests, evidence = {}, {}
    for slug in DEFAULT_ADVENTURE_COLLECTIONS:
        text = PUBLIC + "\n" + SECRET
        manifest = AdventureManifest(
            source=slug,
            source_sha256="a" * 64,
            extraction_sha256=Extraction(
                slug, "a" * 64, 1, [ExtractedPage(1, text, "generated", check_text(text))]
            ).text_sha256(),
            opening_scene_id="room",
            scenes=[{"scene_id": "room", "title": "Room"}],
            entities=[{"entity_id": "mara", "kind": "actor", "title": "Mara"}],
            objects=[
                {
                    "canonical_id": "note",
                    "title": "Lens note",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                },
                {
                    "canonical_id": "cord",
                    "title": "Spare cord",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                    "operations": ["tether"],
                    "consumable": True,
                },
                {
                    "canonical_id": "brace",
                    "title": "Brace",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                    "portable": False,
                    "operations": ["secure"],
                },
            ],
            objectives=[
                {
                    "canonical_id": "rescue",
                    "title": "Rescue",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                }
            ],
            passages=[
                {
                    "passage_id": "public",
                    "page": 1,
                    "start": 0,
                    "end": len(PUBLIC),
                    "text_sha256": text_digest(PUBLIC),
                    "scene_ids": ["room"],
                    "entity_ids": ["mara"],
                    "audience": "player",
                },
                {
                    "passage_id": "private",
                    "page": 1,
                    "start": len(PUBLIC) + 1,
                    "end": len(text),
                    "text_sha256": text_digest(SECRET),
                    "scene_ids": ["room"],
                    "audience": "planner",
                },
            ],
            reviewed_by="synthetic acceptance fixture",
        )
        manifest.reviewed_sha256 = manifest_digest(manifest)
        extraction = Extraction(
            slug, "a" * 64, 1, [ExtractedPage(1, text, "generated", check_text(text))]
        )
        chunks = manifest_chunks(extraction, manifest)
        manifests[slug] = manifest.model_dump()
        evidence[slug] = chunks
        q.create_collection(
            slug, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        q.upsert(
            slug,
            [
                PointStruct(id=i + 1, vector=[1.0] + [0.0] * (VECTOR_SIZE - 1), payload=c)
                for i, c in enumerate(chunks)
            ],
        )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda slugs: {s: manifests[s] for s in slugs},
    )
    monkeypatch.setattr("db.vector.retrieval.get_qdrant_client", lambda: q)
    monkeypatch.setattr(
        "agent.nodes.world_retriever.inspect_source",
        lambda slug, **kw: {"slug": slug, "status": "ready", "usable_chunks": 2},
    )
    monkeypatch.setattr("agent.nodes.world_retriever.search_rules_context", lambda *a, **kw: [])
    slug = next(iter(manifests))
    manifest = manifests[slug]
    chunk = next(c for c in evidence[slug] if c["audience"] == "player")
    refid = source_citation(chunk, "2014")["reference_id"]

    def effect(kind, identity, *, target_kind="object", status="discovered", **kw):
        return StoryEffect(
            kind=kind,
            status=status,
            target=StoryRef(
                collection=slug,
                source_sha256=manifest["source_sha256"],
                manifest_sha256=manifest["reviewed_sha256"],
                kind=target_kind,
                canonical_id=identity,
            ),
            source_reference_ids=[refid],
            **kw,
        )

    yield effect
    q.close()


def events(text):
    return [
        (frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:]))
        for frame in text.strip().split("\n\n")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_story_pickup_environment_reload_and_rejection(
    story_source, fake_llm_schema, api_client, monkeypatch, stream
):
    import db.postgres.session as sm
    from apps.api.services.story_state import load_story_state

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    suffix = "/stream" if stream else ""

    async def turn(proposals, narration="You proceed carefully."):
        fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
            success=True,
            story_effects=proposals,
            mechanical_summary="REJECTED CLAIM OF COMPLETED FACT",
        )
        fake_llm_schema.narration = narration
        response = await api_client.post(
            f"/api/sessions/{sid}/turn{suffix}",
            json={"message": "I inspect and use what I find.", "debug": True},
        )
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
        assert "REJECTED CLAIM" not in response.text
        if stream:
            frames = events(response.text)
            assert frames[-1][0] == "done", response.text
            return frames[-1][1]
        return response.json()

    effect = story_source
    await turn([effect("encounter", "mara", target_kind="entity", status="encountered")])
    pickup = effect("acquire", "note", status="acquired")
    first = await turn([pickup, pickup], f"You recover the Lens note: “{NOTE}”")
    stable_id = story_target_id(pickup.target)
    assert (
        sum(
            isinstance(i, dict) and i.get("story_object_id") == stable_id
            for i in first["character"]["inventory"]["items"]
        )
        == 1
    )
    await turn([effect("acquire", "cord", status="acquired")])
    await turn([effect("use", "cord", status="used", operation="tether", destination="brace")])
    async with sm.async_session_factory() as db:
        ledger = await load_story_state(db, sid)
    use = next(f for f in ledger["facts"] if f["kind"] == "use")
    await turn(
        [
            effect(
                "environment",
                "brace",
                status="changed",
                operation="secure",
                knowledge_fact_ids=[use["fact_id"]],
            )
        ]
    )
    await turn(
        [
            effect(
                "objective",
                "rescue",
                target_kind="objective",
                status="completed",
                knowledge_fact_ids=[use["fact_id"]],
            )
        ]
    )
    await turn([pickup, effect("environment", "brace", status="changed", operation="unsupported")])
    async with sm.async_session_factory() as db:
        ledger = await load_story_state(db, sid)
    assert (
        len(
            [
                f
                for f in ledger["facts"]
                if f["kind"] == "acquire" and f["target"]["canonical_id"] == "note"
            ]
        )
        == 1
    )
    assert any(f["kind"] == "environment" for f in ledger["facts"])
    assert any(f["kind"] == "objective" and f["status"] == "completed" for f in ledger["facts"])
    assert all(f["player_text"] == PUBLIC for f in ledger["facts"])
    restored = await api_client.get(f"/api/sessions/{sid}/messages")
    assert NOTE in restored.text
    assert SECRET not in restored.text
    # Future planning receives the exact committed object identity and quote after reload.
    prompts = "\n".join(
        str(messages) for schema, messages in fake_llm_schema.calls if schema is AdjudicationResult
    )
    assert stable_id in prompts and NOTE in prompts
