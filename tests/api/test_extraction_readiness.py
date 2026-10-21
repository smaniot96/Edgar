"""Worker diagnostics and local preview endpoints use isolated files and stubbed indexes."""

import hashlib

import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

from apps.api.routers import adventures
from apps.api.schemas.adventure import AdventureRetryRequest
from apps.api.services import adventures_meta, campaign_sources
from ingestion.extract import ExtractedPage, Extraction, save_extraction
from ingestion.readiness import prepare_report, report_summary


@pytest.fixture
def local_sources(tmp_path, monkeypatch):
    root = tmp_path / "adventures"
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    for module, field in [(adventures_meta, "ADVENTURES_DIR"), (adventures, "_ADVENTURES_DIR")]:
        monkeypatch.setattr(module, field, root)
    for module, field in [
        (adventures_meta, "PDF_DIR"),
        (adventures, "_PDF_DIR"),
        (campaign_sources, "PDF_DIR"),
    ]:
        monkeypatch.setattr(module, field, pdfs)
    monkeypatch.setattr(
        campaign_sources,
        "inspect_source",
        lambda slug, **kw: {
            "slug": slug,
            "status": "ready",
            "usable_chunks": 1,
            "collection_exists": True,
            "expected_chunks": kw.get("expected_chunks"),
        },
    )
    return root, pdfs


def extraction(*, source_hash="source", failed=False):
    pages = [(1, "The party arrives at the bridge.")]
    extracted = Extraction(
        "bridge",
        source_hash,
        1,
        [
            ExtractedPage(
                1,
                pages[0][1],
                "native_geometry",
                {"errors": ["duplicate_word_layer"] if failed else []},
                ["numbers_need_local_review"],
            )
        ],
    )
    result = prepare_report(
        extracted,
        source_kind="pdf",
        chunks=[{"page": 1, "source_pages": [1], "text": pages[0][1], "section": "Bridge"}],
    )
    return result, extracted


def test_changed_retained_pdf_and_failed_quality_cannot_read_as_ready(local_sources):
    _, pdfs = local_sources
    pdf = pdfs / "bridge.pdf"
    pdf.write_bytes(b"Original supplied source")
    result, _ = extraction(source_hash=hashlib.sha256(pdf.read_bytes()).hexdigest())
    adventures_meta.save_meta(
        "bridge", {"status": "ready", "chunks": 1, "ingestion": report_summary(result)}
    )
    assert campaign_sources.source_read("bridge").status == "ready"
    pdf.write_bytes(b"Different PDF under the same slug")
    assert campaign_sources.source_read("bridge").status == "partial"
    result["quality_passed"] = False
    adventures_meta.save_meta(
        "bridge", {"status": "ready", "chunks": 1, "ingestion": report_summary(result)}
    )
    assert campaign_sources.source_read("bridge").status == "failed"


def test_local_preview_endpoints_expose_report_and_literal_markdown(local_sources):
    root, _ = local_sources
    result, extracted = extraction(failed=True)
    extracted.pages[0].text = "<script>not executable</script>\n" + extracted.pages[0].text
    save_extraction(extracted, root / "bridge", readiness=result)
    app = FastAPI()
    app.include_router(adventures.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get("/api/adventures/bridge/extraction")
        assert response.status_code == 200
        assert response.json()["quality_passed"] is False
        assert "text" not in response.json()["pages"][0]
        assert response.json()["source_current"] is False
        full = client.get("/api/adventures/bridge/extraction/markdown")
        assert full.status_code == 200 and full.headers["content-type"].startswith("text/plain")
        assert "<script>" in full.text
        assert full.headers["X-Extraction-Source-Current"] == "false"
        assert client.get("/api/adventures/unknown/extraction").status_code == 404
        assert client.get("/api/adventures/rules_monster_manual/extraction").status_code == 422
        save_extraction(extracted, root / "bridge")
        raw = client.get("/api/adventures/bridge/extraction").json()
        assert raw["chunk_count"] == 0 and raw["quality_passed"] is False
        assert raw["previews"]


def test_archived_reviewed_retry_is_held_without_metadata_changes(local_sources):
    root, pdfs = local_sources
    pdf = pdfs / "bridge.pdf"
    pdf.write_bytes(b"Retained test source")
    result, extracted = extraction(source_hash=hashlib.sha256(pdf.read_bytes()).hexdigest())
    save_extraction(extracted, root / "bridge", readiness=result)
    original = {"status": "failed", "chunks": 1, "ingestion": report_summary(result)}
    adventures_meta.save_meta("bridge", original)
    current = AdventureRetryRequest(
        reviewed_source_sha256=extracted.source_sha256,
        reviewed_extraction_sha256=extracted.text_sha256(),
    )
    background = BackgroundTasks()
    before = adventures_meta.meta_path("bridge").read_bytes()
    with pytest.raises(HTTPException) as caught:
        adventures._retry_adventure("bridge", background, current)
    assert caught.value.status_code == 409
    assert not background.tasks
    assert adventures_meta.meta_path("bridge").read_bytes() == before


def test_changed_pdf_makes_retained_preview_explicitly_previous(local_sources):
    root, pdfs = local_sources
    pdf = pdfs / "bridge.pdf"
    pdf.write_bytes(b"Original source")
    result, extracted = extraction(source_hash=hashlib.sha256(pdf.read_bytes()).hexdigest())
    save_extraction(extracted, root / "bridge", readiness=result)
    adventures_meta.save_meta(
        "bridge", {"status": "ready", "chunks": 1, "ingestion": report_summary(result)}
    )
    assert campaign_sources.source_read("bridge").extraction_preview_current
    pdf.write_bytes(b"New source")
    source = campaign_sources.source_read("bridge")
    assert source.status == "partial" and source.extraction_preview_available
    assert not source.extraction_preview_current
    assert not adventures.get_extraction_preview("bridge")["source_current"]
