"""Reviewed campaign records publish without being parsed as extracted PDF stat columns."""

import json
from copy import deepcopy

import pytest
from test_campaign_graph_v2 import sample

from apps.api.services.campaign_prep.compile import assemble, make_extraction
from edgar_core.manifests import AdventureManifest, ManifestError, manifest_digest
from ingestion.embed import _prepare_extraction
from ingestion.extract import ExtractedPage, Extraction, load_reviewed_extraction
from ingestion.quality import check_text
from ingestion.readiness import IngestionRejected
from ingestion.semantic import (
    SemanticRecordPage,
    load_reviewed_manifest_records,
    prepare_reviewed_manifest_records,
)

QUOTED_SOURCE = (
    "Guz\nMedium humanoid, chaotic neutral\nArmor Class 14\nHit Points 67\nSpeed 30 ft.\n\n"
    "Ahmed Noke\nMedium humanoid, neutral evil\nArmor Class 12\nHit Points 55\nSpeed 30 ft."
)


def reviewed_records():
    spine, bundles = deepcopy(sample())
    bundles["chapters"][0]["source_notes"] = QUOTED_SOURCE
    compiled = assemble("semantic_publication", "2014", spine, bundles, [])
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "independent-semantic-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return make_extraction(compiled), manifest


def test_reviewed_records_retain_mixed_private_source_and_exact_index_receipts(tmp_path):
    extraction, manifest = reviewed_records()
    before = extraction.text_sha256()
    assert any(check_text(page.text)["errors"] for page in extraction.pages)
    report = {}
    chunks = prepare_reviewed_manifest_records(
        extraction, manifest, preview_dir=tmp_path, on_prepared=report.update
    )
    assert extraction.text_sha256() == before
    assert report["quality_passed"] and report["issues"] == []
    assert report["required_pages"] == report["covered_pages"]
    assert report["chunk_count"] == len(manifest.passages) == len(chunks)
    assert report["source_sha256"] == report["extraction_sha256"] == before
    assert report["index_version"]
    quoted = next(chunk for chunk in chunks if QUOTED_SOURCE in chunk["text"])
    assert quoted["audience"] == "planner"
    assert quoted["manifest_sha256"] == manifest.reviewed_sha256
    assert quoted["source_sha256"] == before
    assert quoted["text_sha256"] == next(
        passage.text_sha256
        for passage in manifest.passages
        if passage.passage_id == quoted["passage_id"]
    )
    persisted = json.loads((tmp_path / "semantic_publication.extraction.json").read_text())
    assert persisted["readiness"] == report
    assert "not a PDF page" in (tmp_path / "semantic_publication.md").read_text()


def test_ordinary_pdf_stat_integrity_checks_remain_active():
    extraction = Extraction(
        "original_pdf",
        "a" * 64,
        1,
        [ExtractedPage(1, QUOTED_SOURCE, "layout_ocr", check_text(QUOTED_SOURCE))],
    )
    with pytest.raises(IngestionRejected):
        _prepare_extraction(extraction, "pdf", 1000, 0, None, None)
    with pytest.raises(ValueError, match="explicit semantic record"):
        prepare_reviewed_manifest_records(extraction, reviewed_records()[1])


def test_changed_semantic_text_cannot_reuse_review():
    extraction, manifest = reviewed_records()
    extraction.pages[-1].text += "\nA changed hidden truth."
    with pytest.raises(ManifestError, match="source checksum"):
        prepare_reviewed_manifest_records(extraction, manifest)
    extraction.source_sha256 = extraction.text_sha256()
    with pytest.raises(ManifestError, match="exact source/extraction"):
        prepare_reviewed_manifest_records(extraction, manifest)


@pytest.mark.parametrize("change", ["review", "span", "digest", "audience"])
def test_missing_review_or_modified_classification_is_rejected(change):
    extraction, manifest = reviewed_records()
    if change == "review":
        manifest.reviewed_by = None
    elif change == "span":
        manifest.passages[-1].end -= 1
    elif change == "digest":
        manifest.passages[-1].text_sha256 = "b" * 64
    else:
        manifest.passages[-1].audience = "unreviewed"
    with pytest.raises(ManifestError):
        prepare_reviewed_manifest_records(extraction, manifest)


def test_even_reacknowledged_bad_passage_checksum_is_rejected():
    extraction, manifest = reviewed_records()
    manifest.passages[-1].text_sha256 = "b" * 64
    manifest.reviewed_sha256 = manifest_digest(manifest)
    with pytest.raises(ManifestError, match="Passage checksum"):
        prepare_reviewed_manifest_records(extraction, manifest)


def test_semantic_record_coverage_is_required_after_valid_spans():
    extraction, manifest = reviewed_records()
    extraction.page_count += 1
    with pytest.raises(IngestionRejected) as caught:
        prepare_reviewed_manifest_records(extraction, manifest)
    assert caught.value.report["issues"][0]["code"] == "source_page_coverage"


def test_semantic_records_cannot_exclude_private_content():
    extraction, manifest = reviewed_records()
    extraction.excluded_pages = [{"page": 2, "reason": "art"}]
    with pytest.raises(ValueError, match="complete unmodified"):
        prepare_reviewed_manifest_records(extraction, manifest)


def test_saved_semantic_artifacts_reopen_with_exact_receipt_and_private_source(tmp_path):
    extraction, manifest = reviewed_records()
    expected = prepare_reviewed_manifest_records(extraction, manifest, preview_dir=tmp_path)
    path = tmp_path / "semantic_publication.extraction.json"
    # The PDF loader fails explicitly instead of losing the document discriminator.
    with pytest.raises(ValueError, match="exact reviewed manifest"):
        load_reviewed_extraction(None, path)
    reopened = load_reviewed_manifest_records(path, manifest)
    assert all(isinstance(page, SemanticRecordPage) for page in reopened.pages)
    assert reopened.text_sha256() == extraction.text_sha256()
    assert prepare_reviewed_manifest_records(reopened, manifest) == expected
    assert any(QUOTED_SOURCE in page.text for page in reopened.pages)


@pytest.mark.parametrize("tamper", ["text", "markdown", "review", "format"])
def test_saved_semantic_artifact_tampering_is_rejected(tmp_path, tamper):
    extraction, manifest = reviewed_records()
    prepare_reviewed_manifest_records(extraction, manifest, preview_dir=tmp_path)
    path = tmp_path / "semantic_publication.extraction.json"
    data = json.loads(path.read_text())
    if tamper == "text":
        data["pages"][-1]["text"] += "changed hidden truth"
    elif tamper == "markdown":
        markdown = tmp_path / "semantic_publication.md"
        markdown.write_text(markdown.read_text() + "changed text")
    elif tamper == "review":
        data["reviewed_manifest"]["passages"][-1]["audience"] = "player"
    else:
        data["document_kind"] = "pdf"
    path.write_text(json.dumps(data))
    with pytest.raises(ManifestError):
        load_reviewed_manifest_records(path, manifest)
