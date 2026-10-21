"""Focused restart, cancellation and no-regeneration recovery boundaries."""

from __future__ import annotations

import pytest

from apps.api.routers import adventures
from apps.api.services import adventures_meta as store, campaign_sources


@pytest.fixture
def recovery_store(tmp_path, monkeypatch):
    adventures_dir = tmp_path / "adventures"
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    monkeypatch.setattr(store, "ADVENTURES_DIR", adventures_dir)
    monkeypatch.setattr(store, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(adventures, "_ADVENTURES_DIR", adventures_dir)
    monkeypatch.setattr(adventures, "_PDF_DIR", pdf_dir)
    monkeypatch.setattr(campaign_sources, "PDF_DIR", pdf_dir)
    monkeypatch.setattr(campaign_sources, "active_index_version", lambda _slug: None)
    monkeypatch.setattr(
        campaign_sources,
        "inspect_source",
        lambda slug, **_kwargs: {
            "slug": slug,
            "status": "missing",
            "usable_chunks": 0,
            "expected_chunks": None,
            "collection_exists": False,
        },
    )
    return pdf_dir


def test_failed_job_projection_uses_safe_copy_and_stable_support_reference():
    private_detail = "private generation prompt and sk-secret-123"
    metadata = {
        "status": "failed",
        "failure_code": "turn_save_failed",
        "error": private_detail,
        "support_ref": "err-0123456789abcdef",
        "job": {
            "job_id": "a" * 32,
            "kind": "ingestion",
            "state": "failed",
            "stage": "failed",
        },
    }

    first = campaign_sources.safe_job_read(metadata, "failed_job")
    second = campaign_sources.safe_job_read(metadata, "failed_job")

    assert first is not None and second is not None
    assert first.failure is not None and second.failure is not None
    assert first.failure.code == "turn_save_failed"
    assert first.failure.support_ref == second.failure.support_ref == "err-0123456789abcdef"
    assert first.failure.message != private_detail
    assert private_detail not in first.model_dump_json()


@pytest.mark.parametrize("title_hint", ["", "An earlier working title"])
def test_failed_generated_replacement_preserves_the_published_title(recovery_store, title_hint):
    store.save_meta(
        "published_keep",
        {
            "status": "ready",
            "title": "The Accepted Keep",
            "chunks": 3,
            "generation": {"kind": "ai", "size": "small", "title_hint": title_hint},
        },
    )
    token = store.begin_attempt("published_keep", job_attempt=True, job_kind="generation")
    store.ensure_job("published_keep", token, kind="generation", stage="outline")
    assert store.finish_attempt(
        "published_keep", token, {"status": "failed", "error": "Synthetic provider failure"}
    )

    failed_replacement = store.load_meta("published_keep")
    assert failed_replacement["status"] == "ready"
    assert failed_replacement["title"] == "The Accepted Keep"
    assert failed_replacement["job"]["state"] == "failed"
