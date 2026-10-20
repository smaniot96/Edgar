"""Quality readiness uses disposable previews, synthetic source text and no providers."""

import json
from contextlib import closing

import pymupdf
import pytest
from qdrant_client import QdrantClient

from db.vector.embeddings import VECTOR_SIZE
from db.vector.versions import active_index_read, read_index_state
from ingestion import embed
from ingestion.chunk import chunk_page_runs
from ingestion.artifacts import corrections_digest, exclusions_digest, source_digest
from ingestion.extract import ExtractedPage, Extraction, apply_text_corrections, save_extraction
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


PICTURE_ONLY = (
    "**==> picture [578 x 515] intentionally omitted <==**\n\n"
    "**----- Start of picture text -----**<br>\nMZ Bs gt<br>Jt i, VP<br>"
    "**----- End of picture text -----**<br>\n"
)


def test_picture_only_page_is_not_required_but_is_logged_as_non_chunkable():
    pages = [(1, "The opening paragraph of the rules."), (2, PICTURE_ONLY)]
    chunk_list = chunk_page_runs(pages, "source")
    assert {n for c in chunk_list for n in c["source_pages"]} == {1}
    result = report(pages, chunks=chunk_list)
    require_ready(result)
    assert result["required_pages"] == [1]
    (skipped,) = result["non_chunkable_pages"]
    assert skipped["page"] == 2 and skipped["chunkable_chars"] == 0
    assert skipped["chars"] == len(PICTURE_ONLY)
    assert skipped["reason"] == "picture_placeholder_only"


def test_real_text_lost_by_the_chunker_still_fails_coverage():
    pages = [(1, "The opening paragraph of the rules."), (2, "Real rules text on page two.")]
    chunks = chunk_page_runs(pages[:1], "source")
    result = report(pages, chunks=chunks)
    assert "non_chunkable_pages" not in result
    assert "chunk_page_coverage" in [i["code"] for i in result["issues"]]


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


def test_reviewed_exclusion_is_recorded_in_the_promoted_receipt(tmp_path, monkeypatch):
    pdf = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 60), "BOOK COVER")
        doc.new_page().insert_text((40, 60), "Opening paragraph of the rules.")
        doc.save(pdf)
    excluded = [{"page": 1, "reason": "cover"}]
    extraction = Extraction(
        "book",
        source_digest(pdf),
        2,
        [
            ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": ["duplicate_word_layer"]}),
            ExtractedPage(2, "Opening paragraph of the rules.", "native_geometry", {"errors": []}),
        ],
        excluded_pages=excluded,
    )
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        count = embed.run_pipeline(
            pdf,
            "book_rules",
            reviewed_extraction=report,
            save_markdown=False,
            rules_edition="unspecified",
        )
        with active_index_read(client, "book_rules") as active_filter:
            points = client.scroll("book_rules", scroll_filter=active_filter)[0]
        assert count == len(points) == 1
        assert {point.payload["page"] for point in points} == {2}
        receipt = read_index_state(client, "book_rules")["sources"]["book"]["active"]
        assert receipt["excluded_pages"] == excluded
        assert receipt["exclusions_sha256"] == exclusions_digest(excluded)


def test_same_pdf_without_the_reviewed_exclusion_stays_blocked(tmp_path, monkeypatch):
    pdf = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 60), "BOOK COVER")
        doc.new_page().insert_text((40, 60), "Opening paragraph of the rules.")
        doc.save(pdf)
    extraction = Extraction(
        "book",
        source_digest(pdf),
        2,
        [
            ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": ["duplicate_word_layer"]}),
            ExtractedPage(2, "Opening paragraph of the rules.", "native_geometry", {"errors": []}),
        ],
    )
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    monkeypatch.setattr(embed, "get_qdrant_client", lambda: pytest.fail("vector client called"))
    monkeypatch.setattr(embed, "get_embeddings", lambda *_: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="quality checks failed"):
        embed.run_pipeline(pdf, "book_rules", reviewed_extraction=report, save_markdown=False)


def _correction_pdf(tmp_path):
    pdf = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 60), "BOOK COVER")
        doc.new_page().insert_text((40, 60), "Opening paragraph of the rules.")
        doc.save(pdf)
    return pdf


def _run_corrected_pipeline(monkeypatch, pdf, report, collection):
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        count = embed.run_pipeline(
            pdf,
            collection,
            reviewed_extraction=report,
            save_markdown=False,
            rules_edition="unspecified",
        )
        with active_index_read(client, collection) as active_filter:
            points = client.scroll(collection, scroll_filter=active_filter)[0]
        receipt = read_index_state(client, collection)["sources"]["book"]["active"]
    return count, points, receipt


def test_text_correction_is_recorded_in_the_promoted_receipt_and_payload(tmp_path, monkeypatch):
    pdf = _correction_pdf(tmp_path)
    extraction = Extraction(
        "book",
        source_digest(pdf),
        2,
        [
            ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": []}),
            ExtractedPage(2, "Opening paragraph of the rules.", "native_geometry", {"errors": []}),
        ],
    )
    apply_text_corrections(
        extraction, [{"page": 2, "old": "Opening paragraph", "new": "Opening passage"}]
    )
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    count, points, receipt = _run_corrected_pipeline(monkeypatch, pdf, report, "book_rules")

    expected = [{"page": 2, "old": "Opening paragraph", "new": "Opening passage"}]
    assert count == len(points) == 1
    assert receipt["text_corrections"] == expected
    assert receipt["corrections_sha256"] == corrections_digest(expected)
    payload = points[0].payload
    assert payload["text_corrections"] == expected
    assert payload["corrected_pages"] == [2]
    assert "Opening passage" in payload["text"]


def test_ordinary_receipt_and_payload_carry_no_correction_fields(tmp_path, monkeypatch):
    pdf = _correction_pdf(tmp_path)
    extraction = Extraction(
        "book",
        source_digest(pdf),
        2,
        [
            ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": []}),
            ExtractedPage(2, "Opening paragraph of the rules.", "native_geometry", {"errors": []}),
        ],
    )
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    _count, points, receipt = _run_corrected_pipeline(monkeypatch, pdf, report, "book_rules")
    assert "text_corrections" not in receipt and "corrections_sha256" not in receipt
    assert "text_corrections" not in points[0].payload
    assert "corrected_pages" not in points[0].payload
