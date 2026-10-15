"""Focused restart, cancellation and no-regeneration recovery boundaries."""

from __future__ import annotations

import json

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


def _outline() -> dict:
    return {
        "title": "The Quiet Keep",
        "premise": "A missing bell has unsettled the valley.",
        "setting": "A village at the edge of a pine forest.",
        "hook": "A keeper asks the characters to find the bell.",
        "factions": ["The valley wardens"],
        "key_npcs": ["Mara, the watchful keeper"],
        "chapters": [
            {"name": f"Chapter {index + 1}", "summary": f"The party follows clue {index + 1}."}
            for index in range(3)
        ],
    }


def _checkpoint_chapters() -> dict[str, str]:
    return {
        str(index): f"## Chapter {index + 1}\n\n"
        + f"Chapter {index + 1} preserves its own saved story and clue. " * 8
        for index in range(3)
    }


def test_restart_at_outline_stage_reports_paid_restart_without_calling_provider(
    recovery_store, monkeypatch
):
    from apps.api.services import campaign_generator as generator

    _generation_metadata("outline_only", stage="outline", state="running")
    provider_calls = []
    monkeypatch.setattr(generator, "make_chat_model", lambda *_a, **_kw: provider_calls.append(True))

    assert store.fail_stale_processing() == ["outline_only"]
    job = campaign_sources.safe_job_read(store.load_meta("outline_only"), "outline_only")

    assert job is not None
    assert job.state == "interrupted"
    assert job.stage == "outline"
    assert not job.checkpoint_available
    assert job.retryable
    assert job.action_label == "Restart generation"
    assert "repeats paid model generation calls" in (job.explanation or "")
    assert provider_calls == []


@pytest.mark.asyncio
async def test_queued_cancellation_before_worker_start_skips_generation_provider(
    recovery_store, monkeypatch
):
    from apps.api.services import campaign_generator as generator

    token, _ = _generation_metadata("cancel_before_start", stage="queued", state="queued")
    store.request_job_cancel("cancel_before_start")
    provider_calls = []
    monkeypatch.setattr(generator, "make_chat_model", lambda *_a, **_kw: provider_calls.append(True))

    await generator.build_generated_campaign(
        "cancel_before_start", "", "", "small", attempt_id=token
    )

    metadata = store.load_meta("cancel_before_start")
    assert metadata["job"]["state"] == "cancelled"
    assert not metadata["job"].get("attempt_id")
    assert provider_calls == []


@pytest.mark.asyncio
async def test_retry_with_complete_checkpoint_rebuilds_without_generation_calls(
    recovery_store, monkeypatch
):
    from apps.api.services import campaign_generator as generator

    job = store.new_job("generation", "failed", total_chapters=3, retryable=True)
    job.update(state="failed", completed_chapters=3, checkpoint_available=True)
    store.save_meta(
        "complete_checkpoint",
        {
            "title": "The Quiet Keep",
            "rules_edition": "2014",
            "status": "failed",
            "generation": {
                "kind": "ai",
                "size": "small",
                "theme": "a mystery",
                "title_hint": "The Quiet Keep",
            },
            "job": job,
        },
    )
    checkpoint_dir = store.job_dir("complete_checkpoint", job["job_id"])
    checkpoint_dir.mkdir(parents=True)
    (checkpoint_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": job["job_id"],
                "outline": _outline(),
                "chapters": _checkpoint_chapters(),
            }
        ),
        encoding="utf-8",
    )

    generation_provider_calls = []

    def unexpected_provider_call(*_args, **_kwargs):
        generation_provider_calls.append(True)
        raise AssertionError("A complete saved manuscript must not invoke generation.")

    monkeypatch.setattr(generator, "make_chat_model", unexpected_provider_call)

    def ingest_pages(_pages, _source, _collection, **kwargs):
        kwargs["on_prepared"](
            {
                "report_version": 1,
                "source_kind": "generated",
                "rules_edition": "2014",
                "source_sha256": "1" * 64,
                "extraction_sha256": "2" * 64,
                "page_count": 3,
                "required_pages": [1, 2, 3],
                "covered_pages": [1, 2, 3],
                "chunk_count": 3,
                "quality_passed": True,
                "warning_count": 0,
                "review_note": "Synthetic generated source.",
                "issues": [],
                "previews": [],
                "scene_preview_reviewed": False,
                "index_version": "synthetic-index-version",
            }
        )
        with kwargs["publication_guard"]():
            pass
        return 3

    from ingestion import embed

    monkeypatch.setattr(embed, "ingest_pages", ingest_pages)
    monkeypatch.setattr(
        campaign_sources,
        "inspect_ingestion_source",
        lambda slug, _meta: {
            "slug": slug,
            "status": "ready",
            "usable_chunks": 3,
            "expected_chunks": 3,
            "collection_exists": True,
            "source_file_matches": True,
        },
    )

    tasks = BackgroundTasks()
    accepted = adventures.retry_adventure("complete_checkpoint", tasks)
    assert accepted.status == "processing"
    await tasks()

    completed = store.load_meta("complete_checkpoint")
    assert completed["status"] == "ready"
    assert completed["job"]["state"] == "completed"
    assert completed["job"]["retained_work"] == "published"
    assert generation_provider_calls == []


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
