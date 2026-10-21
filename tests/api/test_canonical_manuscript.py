"""Private, exact-byte manuscript retention and export contract."""

from __future__ import annotations

import hashlib

import pytest

from apps.api.services import adventures_meta as store, campaign_sources, manuscript as manuscripts


@pytest.fixture
def manuscript_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(campaign_sources, "active_index_version", lambda _slug: None)
    return tmp_path


def _published_record(slug: str, attempt_id: str, content: bytes) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    path = store.attempt_dir(slug, attempt_id) / f"{slug}.manuscript.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {
        "attempt_id": attempt_id,
        "filename": f"{slug}.manuscript.md",
        "version": digest,
        "sha256": digest,
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "index_version": "index-1",
    }


def test_export_preserves_exact_utf8_bytes_and_does_not_need_live_index(manuscript_store):
    content = "# Œil-de-brume\r\n\r\nL’aventure — déjà prête.\n".encode()
    attempt_id = "1" * 32
    record = _published_record("authored", attempt_id, content)
    store.save_meta(
        "authored",
        {
            "status": "ready",
            "manuscript": record,
            # No active index receipt: the retained original remains independently usable.
        },
    )

    destination = manuscript_store / "author-export.md"
    resolved = manuscripts.export_canonical_manuscript("authored", destination)

    assert destination.read_bytes() == content
    assert resolved.sha256 == hashlib.sha256(content).hexdigest()
    assert resolved.version == resolved.sha256
    assert resolved.index_version == "index-1"


@pytest.mark.parametrize(
    ("meta", "message"),
    [
        ({"status": "ready"}, "predates retained"),
        (
            {
                "status": "ready",
                "manuscript": {
                    "attempt_id": "2" * 32,
                    "filename": "authored.manuscript.md",
                    "version": "a" * 64,
                    "sha256": "a" * 64,
                    "source_sha256": "b" * 64,
                    "extraction_sha256": "c" * 64,
                    "index_version": "index-1",
                },
            },
            "file is unavailable",
        ),
    ],
)
def test_legacy_or_missing_original_is_reported_honestly(manuscript_store, meta, message):
    store.save_meta("authored", meta)
    with pytest.raises(manuscripts.ManuscriptUnavailable, match=message):
        manuscripts.resolve_canonical_manuscript("authored")


def test_tampering_is_detected_before_export(manuscript_store):
    attempt_id = "3" * 32
    record = _published_record("authored", attempt_id, b"trusted manuscript")
    store.save_meta("authored", {"status": "ready", "manuscript": record})
    source = store.attempt_dir("authored", attempt_id) / "authored.manuscript.md"
    source.write_bytes(b"altered manuscript")

    with pytest.raises(manuscripts.ManuscriptUnavailable, match="integrity check"):
        manuscripts.export_canonical_manuscript("authored", manuscript_store / "bad.md")
    assert not (manuscript_store / "bad.md").exists()


def test_export_rechecks_hash_while_copying_before_replacing_destination(
    manuscript_store, monkeypatch
):
    attempt_id = "4" * 32
    record = _published_record("authored", attempt_id, b"original")
    store.save_meta("authored", {"status": "ready", "manuscript": record})
    source = store.attempt_dir("authored", attempt_id) / "authored.manuscript.md"
    destination = manuscript_store / "existing.md"
    destination.write_bytes(b"keep this destination")

    original_open = type(source).open

    def changing_open(path, *args, **kwargs):
        if path == source and args and args[0] == "rb":
            return _ChangedReader(b"changed after resolve")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(type(source), "open", changing_open)
    with pytest.raises(manuscripts.ManuscriptUnavailable, match="during export"):
        manuscripts.export_canonical_manuscript("authored", destination)
    assert destination.read_bytes() == b"keep this destination"


class _ChangedReader:
    def __init__(self, content: bytes):
        self.content = content
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, size: int) -> bytes:
        result = self.content[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def test_manifest_only_reindex_keeps_manuscript_and_updates_index_version(manuscript_store):
    attempt_id = "5" * 32
    content = b"same generated source"
    record = _published_record("authored", attempt_id, content)
    record.update(source_sha256="a" * 64, extraction_sha256="b" * 64)
    store.save_meta(
        "authored",
        {
            "status": "ready",
            "ingestion": {
                "source_sha256": "a" * 64,
                "extraction_sha256": "b" * 64,
                "index_version": "index-1",
            },
            "manuscript": record,
            "manifest": {"source_sha256": "a" * 64, "extraction_sha256": "b" * 64},
        },
    )
    token = store.begin_attempt("authored")
    report = {
        "report_version": 1,
        "source_kind": "generated",
        "rules_edition": "2014",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "page_count": 1,
        "required_pages": 1,
        "chunk_count": 1,
        "quality_passed": True,
        "issues": [],
        "warning_count": 0,
        "review_note": "test",
        "index_version": "index-2",
    }

    store.prepare_attempt("authored", token, report)
    store.finish_attempt("authored", token, {"status": "ready"}, success=True)
    published = store.load_meta("authored")

    assert published["manuscript"]["attempt_id"] == attempt_id
    assert published["manuscript"]["index_version"] == "index-2"
    assert published["manifest"] == {
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
    }
    assert manuscripts.resolve_canonical_manuscript("authored").path.read_bytes() == content


def test_replacement_extraction_drops_stale_manifest_and_manuscript_pointer(manuscript_store):
    attempt_id = "6" * 32
    record = _published_record("authored", attempt_id, b"old original")
    store.save_meta(
        "authored",
        {
            "status": "ready",
            "ingestion": {"extraction_sha256": "b" * 64},
            "manuscript": record,
            "manifest": {"extraction_sha256": "b" * 64},
            "manifest_draft": {"extraction_sha256": "b" * 64},
        },
    )
    token = store.begin_attempt("authored")
    report = {
        "report_version": 1,
        "source_kind": "pdf",
        "rules_edition": "2014",
        "source_sha256": "c" * 64,
        "extraction_sha256": "d" * 64,
        "page_count": 1,
        "required_pages": 1,
        "chunk_count": 1,
        "quality_passed": True,
        "issues": [],
        "warning_count": 0,
        "review_note": "test",
        "index_version": "index-new",
    }

    store.prepare_attempt("authored", token, report)
    receipt = store.load_meta("authored")["index_receipts"]["index-new"]
    assert "manuscript" not in receipt
    assert "manifest" not in receipt and "manifest_draft" not in receipt
    assert store.load_meta("authored")["attempt"]["invalidate_manifest"] is True

    store.finish_attempt("authored", token, {"status": "ready"}, success=True)
    with pytest.raises(manuscripts.ManuscriptUnavailable, match="predates retained"):
        manuscripts.resolve_canonical_manuscript("authored")


def test_invalid_manuscript_binding_is_rejected(manuscript_store):
    with pytest.raises(manuscripts.ManuscriptUnavailable, match="binding is invalid"):
        manuscripts.resolve_canonical_manuscript("../outside")


def test_player_source_projection_never_includes_manuscript_pointer_or_content(
    manuscript_store, monkeypatch
):
    record = _published_record("authored", "7" * 32, b"PRIVATE FULL MANUSCRIPT CONTENT")
    store.save_meta(
        "authored",
        {
            "status": "ready",
            "title": "Public title",
            "manuscript": record,
            "ingestion": {
                "index_version": "index-1",
                "source_sha256": "a" * 64,
                "extraction_sha256": "b" * 64,
                "quality_passed": True,
            },
        },
    )
    monkeypatch.setattr(
        campaign_sources,
        "inspect_ingestion_source",
        lambda slug, _meta: {
            "slug": slug,
            "status": "ready",
            "chunk_count": 1,
            "source_file_matches": True,
        },
    )

    projection = campaign_sources.source_read("authored").model_dump_json()

    assert "PRIVATE FULL MANUSCRIPT CONTENT" not in projection
    assert "manuscript" not in projection
    assert "attempt_id" not in projection
    assert "authored.manuscript.md" not in projection
