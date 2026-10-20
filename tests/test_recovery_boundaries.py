"""Focused restart, cancellation and no-regeneration recovery boundaries."""

from __future__ import annotations

import pytest
from fastapi import BackgroundTasks, HTTPException

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


def _generation_metadata(slug: str, *, stage: str, state: str, total_chapters: int = 3):
    job = store.new_job("generation", stage, total_chapters=total_chapters)
    token = "a" * 32
    job.update(state=state, attempt_id=token)
    store.save_meta(
        slug,
        {
            "title": "A saved campaign",
            "rules_edition": "2014",
            "status": "processing",
            "generation": {"kind": "ai", "size": "small", "theme": "a mystery"},
            "attempt": {"id": token},
            "job": job,
        },
    )
    return token, job


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


def test_archived_outline_reports_conversion_without_calling_provider(
    recovery_store,
):
    _generation_metadata("outline_only", stage="outline", state="running")

    assert store.fail_stale_processing() == ["outline_only"]
    job = campaign_sources.safe_job_read(store.load_meta("outline_only"), "outline_only")

    assert job is not None
    assert job.state == "interrupted"
    assert job.stage == "outline"
    assert not job.checkpoint_available
    assert not job.retryable
    assert job.action_label == "Preparation conversion required"
    assert "conversion to the current preparation workflow" in (job.explanation or "")


@pytest.mark.parametrize(
    ("source_available", "expected_retryable"), [(False, False), (True, True)]
)
def test_startup_finalizes_queued_cancellation_using_ingestion_source_availability(
    recovery_store, source_available, expected_retryable
):
    slug = f"cancelled_ingestion_{source_available}"
    token = "b" * 32
    job = store.new_job("ingestion", "extracting")
    job.update(state="cancel_requested", attempt_id=token)
    store.save_meta(slug, {"status": "processing", "attempt": {"id": token}, "job": job})
    if source_available:
        (recovery_store / f"{slug}.pdf").write_bytes(b"retained test source")

    assert store.fail_stale_processing() == [slug]

    cancelled = store.load_meta(slug)["job"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["retryable"] is expected_retryable


@pytest.mark.parametrize("stage", ["extracting", "embedding"])
def test_restart_during_ingestion_stage_retries_from_retained_pdf(
    recovery_store, tmp_path, stage
):
    slug = f"restart_{stage}"
    token = "c" * 32
    source_bytes = b"retained source PDF bytes across a process restart"
    source = tmp_path / "original-name.pdf"
    source.write_bytes(source_bytes)
    job = store.new_job("ingestion", stage)
    job.update(state="running", attempt_id=token)
    store.save_meta(
        slug,
        {"status": "processing", "attempt": {"id": token}, "job": job},
    )

    store.stage_attempt_pdf(slug, token, source, "original-name.pdf")
    job_id = job["job_id"]

    assert store.fail_stale_processing() == [slug]
    interrupted = campaign_sources.safe_job_read(store.load_meta(slug), slug)
    assert interrupted is not None
    assert interrupted.state == "interrupted"
    assert interrupted.stage == stage
    assert interrupted.job_id == job_id
    assert interrupted.retryable
    assert interrupted.retained_work == "source_pdf"

    tasks = BackgroundTasks()
    accepted = adventures.retry_adventure(slug, tasks)
    assert accepted.status == "processing"
    assert len(tasks.tasks) == 1

    retried = store.load_meta(slug)
    assert retried["job"]["job_id"] == job_id
    retry_token = retried["attempt"]["id"]
    retry_candidate = store.candidate_pdf_path(slug, retry_token)
    assert retry_candidate.read_bytes() == source_bytes

    with pytest.raises(HTTPException) as duplicate:
        adventures.retry_adventure(slug, BackgroundTasks())
    assert duplicate.value.status_code == 409


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
