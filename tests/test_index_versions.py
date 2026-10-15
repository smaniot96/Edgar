"""Atomic replacement, lost responses, resumable versions and stale worker fencing.

All vectors are synthetic. The HTTP case starts a disposable Qdrant v1.16.3 container;
it never connects to an installed corpus or an embedding provider.
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct
from testcontainers.qdrant import QdrantContainer

from db.vector.collections import ensure_collection
from db.vector.embeddings import VECTOR_SIZE
from db.vector.retrieval import search_collections
from db.vector.source_status import inspect_source
from db.vector.versions import (
    INDEX_STATE_ID,
    SupersededIngestion,
    active_index_read,
    begin_index,
    promote_index,
    read_index_state,
)
from ingestion import embed

VECTOR = [1.0] + [0.0] * (VECTOR_SIZE - 1)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    with closing(QdrantClient(":memory:", force_disable_check_same_thread=True)) as client:
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
        monkeypatch.setattr(embed, "BATCH_SIZE", 1)
        monkeypatch.setattr(embed, "BATCH_DELAY_SEC", 0)
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        monkeypatch.setattr("db.vector.retrieval.embed_query", lambda _: VECTOR)
        yield client


def chunks(*texts, source="book", edition="2014", manifest=None):
    return [
        {
            "text": text,
            "source": source,
            "page": i + 1,
            "index": i,
            "source_pages": [i + 1],
            "source_sha256": f"source-{texts}",
            "extraction_sha256": f"extraction-{texts}",
            "rules_edition": edition,
            **({"manifest_sha256": manifest, "passage_id": f"passage-{i}"} if manifest else {}),
        }
        for i, text in enumerate(texts)
    ]


def active_points(client, collection="books", query_filter=None):
    with active_index_read(client, collection, query_filter) as routed:
        return client.scroll(collection, scroll_filter=routed, limit=1000, with_payload=True)[0]


def test_interrupted_replacement_is_hidden_and_exact_retry_resumes_without_duplicates(
    corpus, monkeypatch
):
    old = chunks("Old opening", "Old conclusion", manifest="review-1")
    embed._embed_and_upsert(old, "books")
    embed._embed_and_upsert(chunks("Other book stays", source="other"), "books")
    old_payloads = {p.id: p.payload for p in active_points(corpus)}
    new = chunks("New opening", "New middle", "New conclusion", manifest="review-2")
    calls = []

    def fail_second_batch(texts):
        calls.extend(texts)
        if len(calls) == 2:
            raise ConnectionError("interrupted embedding")
        return [VECTOR for _ in texts]

    monkeypatch.setattr(embed, "get_embeddings", fail_second_batch)
    with pytest.raises(ConnectionError):
        embed._embed_and_upsert(new, "books")
    assert {p.id: p.payload for p in active_points(corpus)} == old_payloads
    hits = search_collections(
        "anything", ["books"], client=corpus, min_score=None, limit=None, dedupe=False
    )
    assert {c["text"] for c in hits} == {"Old opening", "Old conclusion", "Other book stays"}
    assert inspect_source("books", expected_chunks=3, client=corpus)["status"] == "ready"
    calls.clear()
    monkeypatch.setattr(
        embed, "get_embeddings", lambda texts: calls.extend(texts) or [VECTOR for _ in texts]
    )
    assert embed._embed_and_upsert(new, "books") == 3
    assert calls == ["New middle", "New conclusion"]
    assert {p.payload["text"] for p in active_points(corpus)} == {
        "New opening",
        "New middle",
        "New conclusion",
        "Other book stays",
    }
    version = read_index_state(corpus, "books")["sources"]["book"]["active"]["index_version"]
    assert all(
        p.payload["manifest_sha256"] == "review-2" and p.payload["rules_edition"] == "2014"
        for p in active_points(corpus)
        if p.payload["source"] == "book"
    )
    monkeypatch.setattr(
        embed,
        "get_embeddings",
        lambda _: pytest.fail("Identical retry re-embedded verified points"),
    )
    assert embed._embed_and_upsert(new, "books") == 3
    assert (
        read_index_state(corpus, "books")["sources"]["book"]["active"]["index_version"] == version
    )
    assert corpus.count("books", exact=True).count == 5  # four active chunks plus one control point


def test_reauthored_retry_drops_partial_points_but_preserves_unrelated_legacy_source(
    corpus, monkeypatch
):
    ensure_collection(corpus, "books")
    corpus.upsert(
        "books",
        [
            PointStruct(
                id=42,
                vector=VECTOR,
                payload={"source": "legacy", "text": "Untouched legacy source"},
            )
        ],
    )
    real_upsert = corpus.upsert
    failed = False

    def response_lost(**kwargs):
        nonlocal failed
        result = real_upsert(**kwargs)
        if kwargs["points"][0].id != INDEX_STATE_ID and not failed:
            failed = True
            raise ConnectionError("response lost after accepted write")
        return result

    monkeypatch.setattr(corpus, "upsert", response_lost)
    with pytest.raises(ConnectionError):
        embed._embed_and_upsert(chunks("Partial first draft", "Never written"), "books")
    assert [p.id for p in active_points(corpus)] == [42]
    monkeypatch.setattr(corpus, "upsert", real_upsert)
    embed._embed_and_upsert(chunks("Reauthored manuscript"), "books")
    assert {p.payload["text"] for p in active_points(corpus)} == {
        "Untouched legacy source",
        "Reauthored manuscript",
    }
    assert corpus.count("books", exact=True).count == 3
    assert corpus.retrieve("books", [42], with_vectors=True)[0].vector == VECTOR


def test_stored_coverage_is_validated_before_activation(corpus, monkeypatch):
    embed._embed_and_upsert(chunks("Usable previous version"), "books")
    previous = read_index_state(corpus, "books")["sources"]["book"]["active"]
    real_upsert = corpus.upsert

    def silently_drop(**kwargs):
        if kwargs["points"][0].id == INDEX_STATE_ID:
            return real_upsert(**kwargs)

    monkeypatch.setattr(corpus, "upsert", silently_drop)
    with pytest.raises(ValueError, match="complete source version"):
        embed._embed_and_upsert(chunks("Missing staged point"), "books")
    assert read_index_state(corpus, "books")["sources"]["book"]["active"] == previous
    assert [p.payload["text"] for p in active_points(corpus)] == ["Usable previous version"]


@pytest.mark.parametrize("vector", [{}, [0.0] * VECTOR_SIZE])
def test_retry_repairs_missing_or_zero_staged_dense_vectors(corpus, monkeypatch, vector):
    embed._embed_and_upsert(chunks("Previous ready"), "books")
    upsert = corpus.upsert

    def response_lost(**kwargs):
        result = upsert(**kwargs)
        if kwargs["points"][0].id != INDEX_STATE_ID:
            raise ConnectionError("accepted staging response lost")
        return result

    monkeypatch.setattr(corpus, "upsert", response_lost)
    replacement = chunks("Staged replacement")
    with pytest.raises(ConnectionError):
        embed._embed_and_upsert(replacement, "books")
    monkeypatch.setattr(corpus, "upsert", upsert)
    staged = next(
        p
        for p in corpus.scroll("books", with_payload=True)[0]
        if (p.payload or {}).get("text") == "Staged replacement"
    )
    upsert("books", [PointStruct(id=staged.id, vector=vector, payload=staged.payload)])
    calls = []
    monkeypatch.setattr(
        embed, "get_embeddings", lambda texts: calls.extend(texts) or [VECTOR for _ in texts]
    )
    embed._embed_and_upsert(replacement, "books")
    assert calls == ["Staged replacement"]
    assert [p.payload["text"] for p in active_points(corpus)] == ["Staged replacement"]


@pytest.mark.parametrize(
    "vectors", [[], [[0.0] * VECTOR_SIZE], [[1.0]], [[float("nan")] * VECTOR_SIZE]]
)
def test_invalid_embedding_response_never_replaces_ready_version(corpus, monkeypatch, vectors):
    embed._embed_and_upsert(chunks("Usable version"), "books")
    monkeypatch.setattr(embed, "get_embeddings", lambda _: vectors)
    with pytest.raises(ValueError, match="usable vector"):
        embed._embed_and_upsert(chunks("Rejected replacement"), "books")
    assert [p.payload["text"] for p in active_points(corpus)] == ["Usable version"]


def test_stale_writer_cannot_overwrite_a_newer_completed_attempt(corpus, monkeypatch):
    embed._embed_and_upsert(chunks("Original"), "books")
    new = chunks("Newest accepted version")

    def newer_finishes_during_old_embedding(_texts):
        monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
        embed._embed_and_upsert(new, "books")
        return [VECTOR]

    monkeypatch.setattr(embed, "get_embeddings", newer_finishes_during_old_embedding)
    with pytest.raises(SupersededIngestion):
        embed._embed_and_upsert(chunks("Stale delayed version"), "books")
    assert [p.payload["text"] for p in active_points(corpus)] == ["Newest accepted version"]


def test_failed_cleanup_is_invisible_and_an_identical_retry_removes_it(corpus, monkeypatch):
    embed._embed_and_upsert(chunks("Old"), "books")
    cleanup = embed._prune_stale
    monkeypatch.setattr(
        embed,
        "_prune_stale",
        lambda *_: (_ for _ in ()).throw(ConnectionError("cleanup unavailable")),
    )
    embed._embed_and_upsert(chunks("New"), "books")
    assert [p.payload["text"] for p in active_points(corpus)] == ["New"]
    assert inspect_source("books", expected_chunks=1, client=corpus)["status"] == "ready"
    assert corpus.count("books", exact=True).count == 3
    monkeypatch.setattr(embed, "_prune_stale", cleanup)
    embed._embed_and_upsert(chunks("New"), "books")
    assert corpus.count("books", exact=True).count == 2


def test_read_lock_holds_the_route_until_the_complete_query_finishes(corpus):
    embed._embed_and_upsert(chunks("Old"), "books")
    token = begin_index(corpus, "books", "book", "new-version")
    started = threading.Event()
    finished = threading.Event()

    def promote():
        started.set()
        promote_index(corpus, "books", "book", "new-version", token, {}, validate=lambda: None)
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with active_index_read(corpus, "books") as route:
            future = pool.submit(promote)
            assert started.wait(2)
            assert not finished.wait(0.1)
            assert corpus.scroll("books", scroll_filter=route)[0][0].payload["text"] == "Old"
        future.result(timeout=2)
    assert finished.is_set()


def test_disposable_qdrant_http_lost_response_and_active_nested_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    # Uses a separate randomly port-mapped container, never localhost:6333 from .env.
    with QdrantContainer("qdrant/qdrant:v1.16.3") as container:
        with closing(container.get_client()) as client:
            name = f"test_{uuid.uuid4().hex}"
            monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
            monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
            embed._embed_and_upsert(chunks("Previous ready", manifest="old"), name)
            upsert = client.upsert

            def response_lost(**kwargs):
                result = upsert(**kwargs)
                if kwargs["points"][0].id != INDEX_STATE_ID:
                    raise ConnectionError("accepted write response lost")
                return result

            monkeypatch.setattr(client, "upsert", response_lost)
            new = chunks("Replacement ready", manifest="new")
            with pytest.raises(ConnectionError):
                embed._embed_and_upsert(new, name)
            assert [p.payload["text"] for p in active_points(client, name)] == ["Previous ready"]
            monkeypatch.setattr(client, "upsert", upsert)
            monkeypatch.setattr(
                embed,
                "get_embeddings",
                lambda _: pytest.fail("Accepted staged HTTP point was not resumed"),
            )
            embed._embed_and_upsert(new, name)
            exact = Filter(
                must=[FieldCondition(key="manifest_sha256", match=MatchValue(value="new"))]
            )
            with active_index_read(client, name, exact) as route:
                points = client.query_points(
                    name, query=VECTOR, query_filter=route, with_payload=True
                ).points
                assert [p.payload["text"] for p in points] == ["Replacement ready"]
                assert client.count(name, count_filter=route, exact=True).count == 1
            assert client.count(name, exact=True).count == 2
            monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
            started, finished = threading.Event(), threading.Event()

            def replace_while_reading():
                started.set()
                embed._embed_and_upsert(chunks("Accepted after reader", manifest="latest"), name)
                finished.set()

            with ThreadPoolExecutor(max_workers=1) as pool:
                with active_index_read(client, name) as route:
                    future = pool.submit(replace_while_reading)
                    assert started.wait(2)
                    assert not finished.wait(0.1)
                    assert (
                        client.query_points(
                            name, query=VECTOR, query_filter=route, with_payload=True
                        )
                        .points[0]
                        .payload["text"]
                        == "Replacement ready"
                    )
                future.result(timeout=5)
            assert [p.payload["text"] for p in active_points(client, name)] == [
                "Accepted after reader"
            ]
            assert client.count(name, exact=True).count == 2
