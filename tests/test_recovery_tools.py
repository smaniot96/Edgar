"""Recovery bundle safety checks; this file never contacts a database or Qdrant service."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from tools import recovery


def test_restore_refuses_non_disposable_or_nonempty_targets(tmp_path):
    with pytest.raises(ValueError, match="explicit --disposable-target"):
        recovery.restore_bundle(
            tmp_path,
            "postgresql://operator@127.0.0.1/edgar_restore_target",
            "http://127.0.0.1:6333",
            tmp_path / "sources",
            disposable_target=False,
        )
    with pytest.raises(ValueError, match="loopback"):
        recovery._require_local_disposable_database(
            "postgresql://operator@db.example/edgar_restore_target"
        )
    with pytest.raises(ValueError, match="edgar_restore_"):
        recovery._require_local_disposable_database(
            "postgresql://operator@127.0.0.1/production"
        )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        recovery._empty_destination(occupied)


def test_bundle_checksum_detects_changed_database_artifact(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "postgres.dump").write_bytes(b"database")
    with tarfile.open(bundle / "sources.tar.gz", "w:gz"):
        pass
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": recovery.SCHEMA_VERSION,
                "artifacts": {
                    name: {"sha256": recovery._sha256(bundle / name), "bytes": (bundle / name).stat().st_size}
                    for name in ("postgres.dump", "sources.tar.gz")
                },
                "qdrant_collections": [],
            }
        ),
        encoding="utf-8",
    )
    assert recovery.verify_bundle(bundle)["schema_version"] == 1
    (bundle / "postgres.dump").write_bytes(b"modified")
    with pytest.raises(ValueError, match="checksum"):
        recovery.verify_bundle(bundle)


def test_source_archive_selects_campaign_files_and_excludes_secret_names(tmp_path):
    data = tmp_path / "data"
    attempt = data / "adventures" / "village" / "attempts" / ("a" * 32)
    attempt.mkdir(parents=True)
    (attempt / "village.manuscript.md").write_text("synthetic story", encoding="utf-8")
    (attempt / ".env").write_text("API_KEY=never archive", encoding="utf-8")
    (data / "pdfs").mkdir()
    (data / "pdfs" / "module.pdf").write_bytes(b"synthetic pdf")
    (data / ".env").write_text("POSTGRES_PASSWORD=never archive", encoding="utf-8")
    archive_path = tmp_path / "sources.tar.gz"
    counts = recovery._write_source_archive(data, archive_path)
    assert counts["adventures"] == 1
    with tarfile.open(archive_path, "r:gz") as archive:
        names = set(archive.getnames())
    assert "adventures/village/attempts/" + "a" * 32 + "/village.manuscript.md" in names
    assert "pdfs/module.pdf" in names
    assert all(".env" not in name for name in names)


def test_source_version_and_retained_manuscript_receipts_must_match(tmp_path):
    slug = "village"
    data = tmp_path / "data"
    attempt_id = "a" * 32
    manuscript = data / "adventures" / slug / ".attempts" / attempt_id / f"{slug}.manuscript.md"
    manuscript.parent.mkdir(parents=True)
    manuscript.write_bytes(b"exact retained text")
    digest = recovery._sha256(manuscript)
    (data / "adventures" / f"{slug}.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "ingestion": {
                    "index_version": "2" * 64,
                    "source_sha256": "b" * 64,
                    "extraction_sha256": "c" * 64,
                },
                "manuscript": {
                    "attempt_id": attempt_id,
                    "filename": f"{slug}.manuscript.md",
                    "version": digest,
                    "sha256": digest,
                    "source_sha256": "b" * 64,
                    "extraction_sha256": "c" * 64,
                    "index_version": "2" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="active vector index version"):
        recovery._validate_source_bindings(
            data,
            [{"collection": slug, "active_versions": {slug: "1" * 64}}],
        )
    recovery._validate_source_bindings(
        data,
        [{"collection": slug, "active_versions": {slug: "2" * 64}}],
    )
    manuscript.write_bytes(b"changed text")
    with pytest.raises(ValueError, match="manuscript checksum"):
        recovery._validate_source_bindings(
            data,
            [{"collection": slug, "active_versions": {slug: "2" * 64}}],
        )


def test_source_extraction_refuses_parent_traversal(tmp_path):
    archive_path = tmp_path / "bad.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("../outside.txt")
        payload = b"unsafe"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    destination = tmp_path / "restored"
    destination.mkdir()
    with pytest.raises(ValueError, match="unsafe path"):
        recovery._extract_sources(archive_path, destination)
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize("directory", ["secrets", "credentials", ".env.local", "private.key"])
def test_secret_directories_exclude_innocently_named_descendants(tmp_path, directory):
    # A secret-named host ancestor must not exclude the entire application tree.
    data = tmp_path / "secret-host-project" / "data"
    sources = data / "adventures" / "village"
    hidden = sources / directory / "nested"
    hidden.mkdir(parents=True)
    (hidden / "ordinary.txt").write_text("synthetic sensitive payload", encoding="utf-8")
    (sources / "story.md").write_text("retained story", encoding="utf-8")
    archive_path = tmp_path / "sources.tar.gz"
    counts = recovery._write_source_archive(data, archive_path)
    assert counts["adventures"] == 1
    with tarfile.open(archive_path, "r:gz") as archive:
        names = archive.getnames()
    assert "adventures/village/story.md" in names
    assert not any(directory in name for name in names)

    # Verification/restore must reject externally supplied archives with the same path.
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo(f"adventures/village/{directory}/nested/ordinary.txt")
        member.size = 7
        archive.addfile(member, io.BytesIO(b"blocked"))
    destination = tmp_path / "restored"
    destination.mkdir()
    with pytest.raises(ValueError, match="unsafe path"):
        recovery._extract_sources(archive_path, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("upload_succeeds", [True, False])
def test_restore_upload_uses_pinned_client_http_api_and_redacts_failures(
    tmp_path, monkeypatch, upload_succeeds
):
    import httpx
    from qdrant_client import QdrantClient

    artifact = tmp_path / "vectors" / "collection.snapshot"
    artifact.parent.mkdir()
    artifact.write_bytes(b"synthetic snapshot archive")
    requests = []

    def transport(request):
        requests.append(request)
        if request.method == "GET" and request.url.path == "/collections":
            return httpx.Response(200, json={"result": {"collections": []}, "status": "ok", "time": 0})
        assert request.method == "POST"
        assert request.url.path == "/collections/recovery_fixture/snapshots/upload"
        assert request.url.params["wait"] == "true"
        assert request.url.params["priority"] == "snapshot"
        assert request.url.params["checksum"] == recovery._sha256(artifact)
        assert request.headers["api-key"] == "synthetic-only-key"
        assert b"synthetic snapshot archive" in request.content
        if not upload_succeeds:
            return httpx.Response(500, json={"error": "SENSITIVE_PROVIDER_BODY_SENTINEL"})
        return httpx.Response(200, json={"result": True, "status": "ok", "time": 0})

    # Use the actual locked SDK with every request intercepted; no server or provider calls.
    def client(**kwargs):
        return QdrantClient(
            **kwargs, transport=httpx.MockTransport(transport), check_compatibility=False
        )

    monkeypatch.setattr(recovery, "QdrantClient", client)
    manifest = {"qdrant_collections": [{
        "collection": "recovery_fixture",
        "artifact": "vectors/collection.snapshot",
        "sha256": recovery._sha256(artifact),
    }]}
    if upload_succeeds:
        recovery._restore_vectors(tmp_path, manifest, "http://127.0.0.1:6333", "synthetic-only-key")
    else:
        with pytest.raises(RuntimeError, match="Could not restore") as error:
            recovery._restore_vectors(tmp_path, manifest, "http://127.0.0.1:6333", "synthetic-only-key")
        assert "SENSITIVE_PROVIDER_BODY_SENTINEL" not in str(error.value)
    assert len(requests) == 2
