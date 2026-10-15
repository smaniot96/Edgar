"""Quality readiness uses disposable previews, synthetic source text and no providers."""

import json
from contextlib import closing

import pytest
from qdrant_client import QdrantClient

from db.vector.embeddings import VECTOR_SIZE
from db.vector.versions import active_index_read
from ingestion import embed
from ingestion.extract import ExtractedPage, Extraction, save_extraction
from ingestion.readiness import (
    IngestionRejected,
    prepare_report,
    require_ready,
    stamp_chunks,
)


def report(pages, *, count=None, errors=None, chunks=None, blank=None):
    chunks = (
        chunks
        if chunks is not None
        else [
            {
                "page": n,
                "source_pages": [n],
                "text": text,
                "section": "Starting area",
                "stat_block": False,
            }
            for n, text in pages
            if text.strip()
        ]
    )
    extracted = Extraction(
        "source",
        "source-hash",
        count if count is not None else len(pages),
        [
            ExtractedPage(
                n,
                text,
                "blank_page" if n in (blank or []) else "native_geometry",
                {"errors": (errors or {}).get(n, [])},
            )
            for n, text in pages
        ],
    )
    return prepare_report(extracted, source_kind="pdf", chunks=chunks)


def persist(result, pages, tmp_path):
    extracted = Extraction(
        "source",
        "source-hash",
        len(pages),
        [ExtractedPage(n, text, "native_geometry", {"errors": []}) for n, text in pages],
    )
    save_extraction(extracted, tmp_path, readiness=result)


@pytest.mark.parametrize(
    "pages,kwargs,issue",
    [
        ([(1, "duplicate duplicate " * 40)], {}, "extraction_quality"),
        (
            [(1, "Duplicated words are nonempty")],
            {"errors": {1: ["duplicate_word_layer"]}},
            "extraction_quality",
        ),
        ([(1, "Opening"), (3, "Ending")], {"count": 3}, "source_page_coverage"),
        ([(1, "Opening"), (2, "")], {}, "empty_content_page"),
        (
            [(1, "Opening"), (2, "Missing section")],
            {"chunks": [{"page": 1, "source_pages": [1], "text": "Opening"}]},
            "chunk_page_coverage",
        ),
        (
            [(1, "Opening"), (2, "Lost middle"), (3, "Ending")],
            {
                "chunks": [
                    {"page": 1, "page_end": 3, "source_pages": [1, 3], "text": "Opening Ending"}
                ]
            },
            "chunk_page_coverage",
        ),
        ([(1, " ")], {"blank": [1]}, "empty_extraction"),
    ],
)
def test_nonempty_corrupt_or_partial_extraction_is_not_ready(pages, kwargs, issue, tmp_path):
    result = report(pages, **kwargs)
    persist(result, pages, tmp_path)
    with pytest.raises(IngestionRejected) as caught:
        require_ready(result)
    assert caught.value.code == "extraction_quality"
    assert issue in [i["code"] for i in result["issues"]]
    assert "retry" in str(caught.value)
    assert json.loads((tmp_path / "source.extraction.json").read_text())["readiness"] == result
    assert (tmp_path / "source.md").is_file()


def test_blank_physical_pages_and_complete_cross_page_chunks(tmp_path):
    pages = [(1, "The opening"), (2, ""), (3, "The ending")]
    chunks = [
        {
            "page": 1,
            "page_end": 3,
            "source_pages": [1, 3],
            "text": "The opening\nThe ending",
            "section": "Start",
        }
    ]
    result = report(pages, chunks=chunks, blank=[2])
    require_ready(result)
    stamp_chunks(chunks, result)
    assert result["required_pages"] == [1, 3]
    assert chunks[0]["source_sha256"] == "source-hash"
    assert chunks[0]["extraction_sha256"] == result["extraction_sha256"]
    persist(result, pages, tmp_path)
    assert "PDF page 3" in (tmp_path / "source.md").read_text()


def test_representative_text_stat_and_unreviewed_scene_candidates():
    chunks = [
        {
            "page": 1,
            "source_pages": [1],
            "text": "The party approaches the bridge.",
            "section": "Bridge",
            "stat_block": False,
        },
        {
            "page": 2,
            "source_pages": [2],
            "text": "Guard\nArmor Class 12\nHit Points 9",
            "section": "Guard",
            "stat_block": True,
        },
    ]
    result = report([(1, chunks[0]["text"]), (2, chunks[1]["text"])], chunks=chunks)
    assert [p["kind"] for p in result["previews"]] == ["text", "stat", "scene_candidate"]
    assert result["scene_preview_reviewed"] is False


def test_pdf_gate_persists_rejected_output_before_any_vector_or_provider_call(
    tmp_path, monkeypatch
):
    text = "Broken broken text text with with duplicate duplicate words words " * 12
    extracted = Extraction(
        "broken",
        "f" * 64,
        2,
        [ExtractedPage(1, text, "native_geometry", {"errors": ["duplicate_word_layer"]})],
    )
    monkeypatch.setattr(embed, "extract_document", lambda _: extracted)
    monkeypatch.setattr(embed, "get_qdrant_client", lambda: pytest.fail("vector client called"))
    monkeypatch.setattr(embed, "get_embeddings", lambda *_: pytest.fail("provider called"))
    with pytest.raises(IngestionRejected):
        embed.run_pipeline(
            tmp_path / "broken.pdf", "broken", preview_dir=tmp_path, save_markdown=False
        )
    bundle = json.loads((tmp_path / "broken.extraction.json").read_text())
    assert bundle["pages"][0]["text"] == text
    assert bundle["readiness"]["quality_passed"] is False
    assert (tmp_path / "broken.md").is_file()


@pytest.mark.parametrize(
    "pages,expected",
    [
        (
            [
                (
                    1,
                    "This substantial generated chapter has enough words to explain the opening scene and its starting location.",
                ),
                (2, "## Chapter 2: Empty"),
            ],
            2,
        ),
        (
            [
                (
                    1,
                    "This substantial generated chapter has enough words to explain the opening scene and its starting location.",
                )
            ],
            3,
        ),
    ],
)
def test_generated_partial_or_empty_chapter_is_rejected_before_writes(
    tmp_path, monkeypatch, pages, expected
):
    monkeypatch.setattr(embed, "get_qdrant_client", lambda: pytest.fail("vector client called"))
    monkeypatch.setattr(embed, "get_embeddings", lambda *_: pytest.fail("provider called"))
    with pytest.raises(IngestionRejected):
        embed.ingest_pages(pages, "draft", "draft", expected_pages=expected, preview_dir=tmp_path)
    bundle = json.loads((tmp_path / "draft.extraction.json").read_text())
    assert bundle["readiness"]["source_kind"] == "generated"
    assert not (tmp_path / "draft.pdf").exists()


def test_generic_short_text_remains_valid_without_generated_chapter_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(
        embed,
        "_embed_and_upsert",
        lambda chunks, *args, **kwargs: calls.extend(chunks) or len(chunks),
    )
    assert (
        embed.ingest_pages([(1, "A character rolls a d20 for an ability check.")], "rules", "rules")
        == 1
    )
    assert calls[0]["text"] == "A character rolls a d20 for an ability check."


def test_generated_retry_prunes_old_source_only_after_complete_success(tmp_path, monkeypatch):
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        old = [
            (
                1,
                "The old manuscript describes a bridge with a stone guardian and a winding path through the forest.",
            )
        ]
        new = [
            (
                1,
                "The revised manuscript describes a river ferry with a human guide and a sheltered road through the valley.",
            )
        ]
        embed.ingest_pages(old, "draft", "draft", expected_pages=1, replace=True)
        old_ids = {point.id for point in client.scroll("draft")[0]}

        def failed_embeddings(_texts):
            raise ConnectionError("isolated provider failure")

        monkeypatch.setattr(embed, "get_embeddings", failed_embeddings)
        with pytest.raises(ConnectionError):
            embed.ingest_pages(new, "draft", "draft", expected_pages=1, replace=True)
        assert {point.id for point in client.scroll("draft")[0]} == old_ids
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        embed.ingest_pages(
            new, "draft", "draft", expected_pages=1, replace=True, preview_dir=tmp_path
        )
        with active_index_read(client, "draft") as active_filter:
            current = client.scroll("draft", scroll_filter=active_filter)[0]
        assert len(current) == 1 and current[0].id not in old_ids
        assert "revised manuscript" in current[0].payload["text"]
        assert current[0].payload["source_pages"] == [1]
