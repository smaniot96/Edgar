"""Exercise exact scene retrieval and the private planner prompt without paid calls."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from agent.nodes import world_retriever
from agent.nodes.narrator import narrator_context
from agent.nodes.rules_adjudicator import build_adjudicator_prompt
from apps.api.services.campaign_prep.compile import assemble, make_extraction
from edgar_core.manifests import AdventureManifest, manifest_digest
from ingestion.manifest import manifest_chunks
from tests.test_campaign_graph_v2 import sample


@pytest.mark.asyncio
async def test_all_source_pages_reach_private_planner_after_pagination_and_merge_limits(
    monkeypatch,
):
    spine, bundles = sample()
    numbers = list(range(1, 25))  # More private pages than the general merged top-k of 20.
    spine["chapters"][0]["pages"] = numbers
    source = {
        "kind": "pdf",
        "sha256": "a" * 64,
        "directory": "/data/test/source",
        "pages": [
            {
                "page": number,
                "text": f"PAGE_{number}_START\nOriginal source detail.\nPAGE_{number}_END",
                "image": f"page-{number:04d}.png",
                "regions": [],
                "quality": {},
            }
            for number in numbers
        ],
    }
    compiled = assemble("fresh", "2014", spine, bundles, [], source=source)
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "local-runtime-source-test"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = manifest_chunks(make_extraction(compiled), manifest)
    client = QdrantClient(":memory:")
    client.create_collection("fresh", vectors_config=VectorParams(size=1, distance=Distance.DOT))
    client.upsert(
        "fresh",
        points=[
            PointStruct(id=index, vector=[1.0], payload=chunk) for index, chunk in enumerate(chunks)
        ],
    )
    scroll_calls = []

    def paginated_scroll(**kwargs):
        scroll_calls.append(kwargs.get("offset"))
        return client.scroll(**{**kwargs, "limit": 3})

    @contextmanager
    def active(_client, _collection, query_filter):
        yield query_filter

    monkeypatch.setattr("db.vector.retrieval.active_index_read", active)
    monkeypatch.setattr(
        world_retriever, "inspect_source", lambda slug, **_: {"slug": slug, "status": "ready"}
    )
    monkeypatch.setattr(world_retriever, "_search_rule_queries", lambda *_, **__: ([], False))
    state = {
        "source_mode": "module",
        "player_input": "I examine the surroundings.",
        "adventure_collections": ["fresh"],
        "adventure_manifests": {"fresh": manifest.model_dump()},
        "current_scene_id": "event_square",
        "rules_edition": "2014",
        "qdrant_client": SimpleNamespace(scroll=paginated_scroll),
    }
    try:
        retrieved = await world_retriever._retrieve(state)
        assert retrieved["manifest_status"] == "reviewed"
        assert len(scroll_calls) > 1
        source_chunks = [
            c for c in retrieved["planner_context"] if c["passage_id"].startswith("source_page_")
        ]
        assert len(source_chunks) == 24
        state.update(retrieved)
        prompt = "\n".join(message["content"] for message in build_adjudicator_prompt(state, None))
        for page in source["pages"]:
            assert page["text"] in prompt
        assert all(c["audience"] == "player" for c in retrieved["retrieved_context"])
        _, public_adventure = narrator_context(state)
        assert all(c["audience"] == "player" for c in public_adventure)
        assert all("PAGE_24_END" not in c["text"] for c in public_adventure)
    finally:
        client.close()
