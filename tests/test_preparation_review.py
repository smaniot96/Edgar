"""Independent retained-source integrity regressions; no provider or installed storage."""

import hashlib

import pytest

from apps.api.services import adventures_meta as store


@pytest.fixture
def retained_source(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    token = "a" * 32
    content = "# The Keep\r\n\r\nExact café prose.\n\n## Ending\nPeace.\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    path = store.attempt_dir("keep", token) / "keep.manuscript.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    meta = {
        "status": "ready",
        "ingestion": {
            "source_sha256": "b" * 64,
            "extraction_sha256": "c" * 64,
            "index_version": "index-1",
        },
        "manuscript": {
            "attempt_id": token,
            "filename": path.name,
            "version": digest,
            "sha256": digest,
            "source_sha256": "b" * 64,
            "extraction_sha256": "c" * 64,
            "index_version": "index-1",
        },
    }
    store.save_meta("keep", meta)
    return path, content, meta


def test_retained_original_export_survives_index_failure(retained_source, tmp_path):
    from apps.api.services.manuscript import export_canonical_manuscript

    _, content, meta = retained_source
    meta.update(status="failed", failure_code="index_coverage")
    store.save_meta("keep", meta)
    target = tmp_path / "author-export.md"
    result = export_canonical_manuscript("keep", target)
    assert target.read_bytes() == content
    assert result.sha256 == hashlib.sha256(content).hexdigest()


def test_retained_original_fails_closed_for_changed_source_provenance(retained_source):
    from apps.api.services.manuscript import ManuscriptUnavailable, resolve_canonical_manuscript

    _, _, meta = retained_source
    meta["ingestion"]["source_sha256"] = "d" * 64
    store.save_meta("keep", meta)
    with pytest.raises(ManuscriptUnavailable):
        resolve_canonical_manuscript("keep")


def test_export_checks_bytes_copied_after_resolve_before_replacing_author_file(
    retained_source, tmp_path, monkeypatch
):
    from apps.api.services import manuscript

    path, _, _ = retained_source
    target = tmp_path / "existing-author-export.md"
    target.write_bytes(b"Previous exact export")
    resolve = manuscript.resolve_canonical_manuscript

    def replace_between_resolve_and_read(slug):
        result = resolve(slug)
        path.write_bytes(b"Tampered private source after validation")
        return result

    monkeypatch.setattr(
        manuscript, "resolve_canonical_manuscript", replace_between_resolve_and_read
    )
    with pytest.raises(manuscript.ManuscriptUnavailable):
        manuscript.export_canonical_manuscript("keep", target)
    assert target.read_bytes() == b"Previous exact export"
    assert not list(target.parent.glob("*.tmp"))


@pytest.mark.parametrize("token", ["../outside", "/tmp/outside", "f" * 31, "g" * 32])
def test_corrupt_private_pointer_never_reads_outside_source_attempt(retained_source, token):
    from apps.api.services.manuscript import ManuscriptUnavailable, resolve_canonical_manuscript

    _, _, meta = retained_source
    meta["manuscript"]["attempt_id"] = token
    store.save_meta("keep", meta)
    with pytest.raises((ManuscriptUnavailable, ValueError)):
        resolve_canonical_manuscript("keep")


def test_legacy_preview_is_never_certified_as_original(tmp_path, monkeypatch):
    from apps.api.services.manuscript import ManuscriptUnavailable, resolve_canonical_manuscript

    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    store.save_meta("legacy", {"status": "ready", "generation": {"kind": "ai"}})
    preview = store.ADVENTURES_DIR / "legacy" / "legacy.md"
    preview.parent.mkdir(parents=True)
    preview.write_text("Duplicated overlapping chunks cannot recover the original.")
    with pytest.raises(ManuscriptUnavailable):
        resolve_canonical_manuscript("legacy")


@pytest.mark.parametrize("fresh_review", [False, True])
def test_new_source_bytes_with_identical_extraction_invalidate_only_previous_review(
    retained_source, fresh_review
):
    _, _, meta = retained_source
    meta["manifest"] = {"source_sha256": "b" * 64, "reviewed_sha256": "old-review"}
    store.save_meta("keep", meta)
    token = store.begin_attempt("keep")
    report = {
        "report_version": 1,
        "source_kind": "pdf",
        "rules_edition": "2014",
        "source_sha256": "d" * 64,
        "extraction_sha256": "c" * 64,
        "page_count": 1,
        "required_pages": [1],
        "chunk_count": 1,
        "quality_passed": True,
        "issues": [],
        "warning_count": 0,
        "review_note": "Synthetic reviewed replacement",
        "index_version": "index-2",
    }
    replacement = {"source_sha256": "d" * 64, "reviewed_sha256": "fresh-review"}
    updates = {"manifest": replacement} if fresh_review else {}
    store.prepare_attempt("keep", token, report, updates)
    store.finish_attempt("keep", token, {"status": "ready", **updates}, success=True)
    published = store.load_meta("keep")
    assert "manuscript" not in published
    assert published.get("manifest") == (replacement if fresh_review else None)


def test_restart_recovers_checkpoint_when_summary_write_fails(
    tmp_path, monkeypatch
):
    """A chapter saved before a failed metadata write must still be useful after restart."""
    from apps.api.services import campaign_sources
    from db.vector.versions import SupersededIngestion

    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(store, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(campaign_sources, "active_index_version", lambda _: None)
    monkeypatch.setattr(
        campaign_sources,
        "inspect_source",
        lambda slug, **_: {"slug": slug, "status": "missing", "usable_chunks": 0},
    )
    job = store.new_job("generation", "queued", total_chapters=3)
    store.save_meta(
        "keep",
        {
            "status": "processing",
            "title": "AI campaign (generating…)",
            "rules_edition": "2014",
            "generation": {"kind": "ai", "size": "small"},
            "job": job,
        },
    )
    token = store.begin_attempt("keep", job_attempt=True, job_kind="generation")
    store.ensure_job("keep", token, kind="generation", stage="outline")
    checkpoint = {
        "version": 1,
        "job_id": job["job_id"],
        "outline": {
            "title": "Private title",
            "premise": "PRIVATE FUTURE PLOT",
            "setting": "A quiet village",
            "hook": "A guide waits by the well",
            "chapters": [{"name": f"Private chapter {i}", "summary": "Hidden"} for i in range(3)],
        },
        "chapters": {
            "0": "PRIVATE COMPLETED CHAPTER. "
            + "A guide follows the old path into the village. " * 4
        },
    }
    save = store.save_meta

    def fail_summary(*_args, **_kwargs):
        raise OSError("simulated metadata publication failure after chapter fsync")

    monkeypatch.setattr(store, "save_meta", fail_summary)
    with pytest.raises(OSError, match="summary|metadata publication"):
        store.save_generation_checkpoint(
            "keep", token, checkpoint, stage="generating_chapters", total_chapters=3
        )
    monkeypatch.setattr(store, "save_meta", save)
    assert store.load_meta("keep")["job"]["completed_chapters"] == 0
    assert (
        store.load_generation_checkpoint("keep", job["job_id"])["chapters"]
        == checkpoint["chapters"]
    )
    assert store.fail_stale_processing() == ["keep"]
    state = campaign_sources.safe_job_read(store.load_meta("keep"), "keep")
    assert state.state == "interrupted" and not state.retryable
    assert state.completed_chapters == 1 and state.total_chapters == 3
    assert "PRIVATE" not in state.model_dump_json()
    assert "generating" not in store.load_meta("keep")["title"].lower()
    with pytest.raises(SupersededIngestion):
        store.save_generation_checkpoint(
            "keep", token, checkpoint, stage="generating_chapters", total_chapters=3
        )


@pytest.mark.parametrize(
    "corruption", ["optional_outline_type", "noncanonical_chapter", "short_chapter"]
)
def test_unreadable_progress_is_terminal_and_offers_no_paid_retry(
    tmp_path, monkeypatch, corruption
):
    import json

    from apps.api.routers import adventures
    from apps.api.services import campaign_sources

    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(store, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(campaign_sources, "active_index_version", lambda _: None)
    monkeypatch.setattr(
        campaign_sources,
        "inspect_source",
        lambda slug, **_: {
            "slug": slug,
            "status": "missing",
            "usable_chunks": 0,
        },
    )
    job = store.new_job("generation", "queued", total_chapters=3)
    store.save_meta(
        "keep",
        {
            "status": "failed",
            "title": "AI campaign (generating…)",
            "generation": {"kind": "ai", "size": "small"},
            "job": job,
        },
    )
    outline = {
        "title": "Private future",
        "premise": "SECRET",
        "setting": "Hidden keep",
        "hook": "Hidden guide",
        "chapters": [{"name": str(index), "summary": "SECRET"} for index in range(3)],
    }
    chapters = {"0": "Enough retained prose with the guide. " * 6}
    if corruption == "optional_outline_type":
        outline["factions"] = 42
    elif corruption == "noncanonical_chapter":
        chapters = {"01": chapters["0"]}
    else:
        chapters = {"0": "unfinished"}
    directory = store.job_dir("keep", job["job_id"])
    directory.mkdir(parents=True)
    (directory / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "job_id": job["job_id"],
                "outline": outline,
                "chapters": chapters,
            }
        )
    )
    with pytest.raises(ValueError):
        store.load_generation_checkpoint("keep", job["job_id"], expected_chapters=3)
    read = adventures._read_for_slug("keep", False)
    assert not read.can_retry and not read.job.retryable
    assert "files and any previously published source are preserved" in read.job.explanation
    assert "SECRET" not in read.model_dump_json()
