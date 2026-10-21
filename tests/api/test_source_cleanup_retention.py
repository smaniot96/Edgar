"""Campaign cleanup retains durable source work independently of metadata health."""

import pytest
from fastapi import HTTPException

from apps.api.routers import adventures
from apps.api.services import adventures_meta


@pytest.fixture
def source_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventures_meta, "PDF_DIR", tmp_path / "pdfs")
    return tmp_path


@pytest.mark.parametrize("metadata", [None, "{broken", '{"status":"failed"}'])
def test_paid_artifacts_survive_missing_or_incomplete_metadata(
    source_storage, monkeypatch, metadata
):
    artifact = adventures_meta.adventure_path("held", "preparation-v2", "graph.sqlite3")
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"paid response and accepted artifacts")
    if metadata is not None:
        adventures_meta.meta_path("held").write_text(metadata)

    def unexpected_inventory():
        pytest.fail("Retained artifacts must be protected before vector-store cleanup")

    monkeypatch.setattr(adventures, "_all_collection_names", unexpected_inventory)

    assert adventures.release_unused_source("held") == "retained"
    assert artifact.read_bytes() == b"paid response and accepted artifacts"
    if metadata is not None:
        assert adventures_meta.meta_path("held").read_text() == metadata


def test_destructive_helper_cannot_bypass_receipt_retention(source_storage, monkeypatch):
    adventures_meta.save_meta("held", {"status": "failed", "preparation_run_id": "prep-receipt"})
    pdf = adventures_meta.canonical_pdf_path("held")
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"original PDF")

    def unexpected_client():
        pytest.fail("Source collection must not be deleted before checking retained work")

    monkeypatch.setattr(adventures, "get_qdrant_client", unexpected_client)

    with pytest.raises(HTTPException) as error:
        adventures.remove_adventure_source("held", has_collection=True)

    assert error.value.status_code == 409
    assert adventures_meta.load_meta("held")["preparation_run_id"] == "prep-receipt"
    assert pdf.read_bytes() == b"original PDF"


@pytest.mark.parametrize(
    "metadata",
    [
        {"status": "processing"},
        {"job": {"state": "queued"}},
        {"job": {"state": "running"}},
        {"job": {"state": "cancel_requested"}},
    ],
)
def test_destructive_helper_cannot_remove_active_source(source_storage, monkeypatch, metadata):
    adventures_meta.save_meta("active", metadata)
    pdf = adventures_meta.canonical_pdf_path("active")
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"source still being processed")

    def unexpected_client():
        pytest.fail("Active source collections must be protected before deletion")

    monkeypatch.setattr(adventures, "get_qdrant_client", unexpected_client)

    with pytest.raises(HTTPException) as error:
        adventures.remove_adventure_source("active", has_collection=True)

    assert error.value.status_code == 409
    assert adventures_meta.load_meta("active") == metadata
    assert pdf.read_bytes() == b"source still being processed"
