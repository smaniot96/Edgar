"""Coverage uses a disposable Qdrant index, without embeddings or external services."""

from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from db.vector.embeddings import VECTOR_SIZE
from db.vector.source_status import inspect_source


@pytest.fixture
def corpus():
    client = QdrantClient(":memory:")
    yield client
    client.close()


def add_source(client, slug, texts, *, size=VECTOR_SIZE):
    client.create_collection(slug, vectors_config=VectorParams(size=size, distance=Distance.COSINE))
    if texts:
        client.upsert(
            slug,
            [
                PointStruct(id=i, vector=[1.0] + [0.0] * (size - 1), payload={"text": text})
                for i, text in enumerate(texts)
            ],
        )


@pytest.mark.parametrize(
    "texts,expected,status,usable",
    [
        ([], None, "empty", 0),
        (["", "  ", None], 3, "empty", 0),
        (["usable text"], None, "ready", 1),
        (["usable text"], 2, "partial", 1),
        (["usable text", ""], None, "partial", 1),
        (["usable text", "second chunk"], 2, "ready", 2),
    ],
)
def test_live_coverage(corpus, texts, expected, status, usable):
    add_source(corpus, "module", texts)
    result = inspect_source("module", expected_chunks=expected, client=corpus)
    assert result["status"] == status
    assert result["usable_chunks"] == usable
    assert result["collection_exists"]


def test_missing_and_unavailable_are_distinct(corpus):
    assert inspect_source("lost", client=corpus)["status"] == "missing"
    assert inspect_source("lost", client=SimpleNamespace())["status"] == "unavailable"


def test_wrong_vector_schema_is_not_ready(corpus):
    add_source(corpus, "wrong", ["A valid passage"], size=8)
    assert inspect_source("wrong", client=corpus)["status"] == "empty"


def test_text_without_vectors_is_not_usable(corpus):
    add_source(corpus, "module", [])
    corpus.upsert(
        "module", [PointStruct(id=1, vector={}, payload={"text": "Text without embedding"})]
    )
    assert inspect_source("module", client=corpus)["status"] == "empty"


def test_coverage_paginates_and_never_downloads_embeddings(corpus, monkeypatch):
    add_source(corpus, "module", [f"chunk {n}" for n in range(300)])
    scroll = corpus.scroll
    calls = []

    def checked_scroll(**kwargs):
        calls.append(kwargs)
        assert kwargs["with_vectors"] is False
        assert kwargs["with_payload"] == ["text"]
        return scroll(**kwargs)

    monkeypatch.setattr(corpus, "scroll", checked_scroll)
    assert inspect_source("module", expected_chunks=300, client=corpus)["status"] == "ready"
    assert len(calls) == 2
    corpus.delete_collection("module")
    assert inspect_source("module", client=corpus)["status"] == "missing"
