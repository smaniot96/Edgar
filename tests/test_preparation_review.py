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

    monkeypatch.setattr(manuscript, "resolve_canonical_manuscript", replace_between_resolve_and_read)
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
