"""Collection management uses in-memory Qdrant; embeddings are stubbed locally."""

import sys
from collections.abc import Iterator

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct

from db.vector import VECTOR_SIZE, ensure_collection, existing_collection_names
from db.vector.scripts import create_collections_for_pdfs, new_collection
from db.vector.versions import active_index_read


@pytest.fixture
def qdrant() -> Iterator[QdrantClient]:
    client = QdrantClient(":memory:")
    yield client
    client.close()


def test_collection_creation_refreshes_cache_and_preserves_existing_points(qdrant) -> None:
    assert existing_collection_names(qdrant) == frozenset()
    assert ensure_collection(qdrant, "rules_test")
    assert existing_collection_names(qdrant) == frozenset({"rules_test"})
    vectors = qdrant.get_collection("rules_test").config.params.vectors
    assert vectors.size == VECTOR_SIZE
    assert vectors.distance == Distance.COSINE

    qdrant.upsert(
        "rules_test",
        points=[PointStruct(id=1, vector=[1.0] * VECTOR_SIZE, payload={"text": "rule"})],
    )
    assert not ensure_collection(qdrant, "rules_test")
    assert qdrant.get_collection("rules_test").points_count == 1
    assert qdrant.retrieve("rules_test", ids=[1])[0].payload == {"text": "rule"}


@pytest.mark.parametrize("failing_method", ["collection_exists", "create_collection"])
def test_collection_management_propagates_errors(qdrant, monkeypatch, failing_method: str) -> None:
    def fail(*_args, **_kwargs):
        raise ConnectionError("test Qdrant failure")

    monkeypatch.setattr(qdrant, failing_method, fail)
    with pytest.raises(ConnectionError, match="test Qdrant failure"):
        ensure_collection(qdrant, "rules_test")
    assert qdrant.get_collections().collections == []


@pytest.mark.parametrize("index_available", [False, True])
def test_ingestion_reuses_collection_and_tolerates_index_failure(
    qdrant, monkeypatch, index_available
):
    from ingestion import embed

    def create_index(*_args, **_kwargs):
        if not index_available:
            raise RuntimeError("test index failure")

    monkeypatch.setattr(embed, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(embed, "get_embeddings", lambda texts: [[1.0] * VECTOR_SIZE for _ in texts])
    monkeypatch.setattr(qdrant, "create_payload_index", create_index)

    pages = [(1, "# Rules\n\nA character rolls a d20 for an ability check.")]
    assert embed.ingest_pages(pages, "test-book", "rules_test") == 1
    assert embed.ingest_pages(pages, "test-book", "rules_test") == 1
    assert qdrant.get_collection("rules_test").points_count == 2  # content plus routing point
    with active_index_read(qdrant, "rules_test") as route:
        points = qdrant.scroll("rules_test", scroll_filter=route)[0]
    assert len(points) == 1
    point = points[0]
    assert point.payload["source"] == "test-book"
    assert point.payload["page"] == 1


def test_collection_scripts_share_settings_and_skip_existing(qdrant, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(new_collection, "get_qdrant_client", lambda: qdrant)
    monkeypatch.setattr(sys, "argv", ["new_collection.py", "rules_test"])
    new_collection.main()
    assert "Created collection 'rules_test'" in capsys.readouterr().out
    new_collection.main()
    assert "Collection 'rules_test' already exists. Skipping." in capsys.readouterr().out

    (tmp_path / "rules_test.pdf").touch()
    (tmp_path / "adventure_test.pdf").touch()
    monkeypatch.setattr(create_collections_for_pdfs, "PDFS_DIR", tmp_path)
    monkeypatch.setattr("db.vector.get_qdrant_client", lambda: qdrant)
    create_collections_for_pdfs.main()
    output = capsys.readouterr().out
    assert "Collection 'rules_test' already exists. Skipping." in output
    assert "Created collection 'adventure_test'" in output
    assert existing_collection_names(qdrant) == frozenset({"rules_test", "adventure_test"})
    assert qdrant.get_collection("adventure_test").config.params.vectors.size == VECTOR_SIZE
