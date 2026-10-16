"""Adversarial path checks for adventure metadata and retained source artifacts."""

import json

import pytest
from fastapi import HTTPException

from apps.api.routers import adventures
from apps.api.schemas.adventure import AdventureUpdate
from apps.api.services import adventures_meta as store
from apps.api.services.campaign_sources import validate_binding


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    adventures_dir = tmp_path / "data" / "adventures"
    pdf_dir = tmp_path / "data" / "pdfs"
    monkeypatch.setattr(store, "ADVENTURES_DIR", adventures_dir)
    monkeypatch.setattr(store, "PDF_DIR", pdf_dir)
    return adventures_dir, pdf_dir, tmp_path


def test_traversal_cannot_read_write_or_lock_outside_metadata_root(data_dirs):
    adventures_dir, _, root = data_dirs
    outside = root / "outside.json"
    sentinel = {"title": "outside sentinel", "status": "ready"}
    outside.write_text(json.dumps(sentinel), encoding="utf-8")
    slug = "../../outside"

    # This is the former path construction: the fallback metadata path escaped the store.
    assert (adventures_dir / f"{slug}.json").resolve() == outside.resolve()
    with pytest.raises(ValueError, match="Invalid adventure source binding"):
        store.meta_path(slug)
    with pytest.raises(ValueError, match="Invalid adventure source binding"):
        store.load_meta(slug)
    with pytest.raises(ValueError, match="Invalid adventure source binding"):
        store.save_meta(slug, {"title": "overwrite"})
    with pytest.raises(ValueError, match="Invalid adventure source binding"):
        with store.lifecycle_lock(slug):
            pass

    assert json.loads(outside.read_text(encoding="utf-8")) == sentinel
    assert not adventures_dir.exists()


@pytest.mark.asyncio
async def test_metadata_source_and_attempt_symlinks_cannot_escape(data_dirs, monkeypatch):
    adventures_dir, pdf_dir, root = data_dirs
    adventures_dir.mkdir(parents=True)
    pdf_dir.mkdir(parents=True)
    outside_meta = root / "private.json"
    outside_meta.write_text('{"title":"private"}', encoding="utf-8")
    (adventures_dir / "linked.json").symlink_to(outside_meta)
    with pytest.raises(ValueError, match="symlink"):
        store.load_meta("linked")
    with pytest.raises(ValueError, match="symlink"):
        store.save_meta("linked", {"title": "changed"})

    monkeypatch.setattr(adventures, "_all_collection_names", lambda: [])
    with pytest.raises(ValueError, match="symlink"):
        await adventures.get_adventure("linked")
    with pytest.raises(ValueError, match="symlink"):
        adventures.update_adventure("linked", AdventureUpdate(title="changed"))
    with pytest.raises(ValueError, match="symlink"):
        adventures.delete_adventure("linked", x_confirm_delete="yes")

    outside_dir = root / "outside"
    outside_dir.mkdir()
    (adventures_dir / "linked_dir").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.attempt_dir("linked_dir", "a" * 32)
    with pytest.raises(ValueError, match="symlink"):
        store.job_dir("linked_dir", "b" * 32)

    outside_pdf = root / "private.pdf"
    outside_pdf.write_bytes(b"private source")
    (pdf_dir / "linked.pdf").symlink_to(outside_pdf)
    with pytest.raises(ValueError, match="symlink"):
        store.source_pdf_path("linked")
    assert outside_meta.read_text(encoding="utf-8") == '{"title":"private"}'
    assert outside_pdf.read_bytes() == b"private source"


def test_generated_and_legacy_slug_forms_remain_valid(data_dirs):
    adventures_dir, pdf_dir, _ = data_dirs
    generated = adventures._slugify("The Dragon's Heist!")
    legacy = "legacy-adventure_2"

    assert generated == "the_dragon_s_heist"
    for slug in (generated, legacy):
        validate_binding(slug)
        store.save_meta(slug, {"title": slug, "status": "ready"})
        assert store.load_meta(slug)["title"] == slug
        assert store.meta_path(slug).parent == adventures_dir.resolve()
        assert store.canonical_pdf_path(slug).parent == pdf_dir.resolve()
        assert store.candidate_pdf_path(slug, "c" * 32).is_relative_to(adventures_dir.resolve())


@pytest.mark.asyncio
async def test_metadata_fallback_handlers_reject_traversal_before_filesystem_access(
    data_dirs, monkeypatch
):
    _, _, root = data_dirs
    outside = root / "outside.json"
    sentinel = {"title": "outside sentinel", "status": "ready"}
    outside.write_text(json.dumps(sentinel), encoding="utf-8")
    monkeypatch.setattr(adventures, "_all_collection_names", lambda: [])

    with pytest.raises(HTTPException) as get_error:
        await adventures.get_adventure("../../outside")
    assert get_error.value.status_code == 422

    with pytest.raises(HTTPException) as patch_error:
        adventures.update_adventure("../../outside", AdventureUpdate(title="changed"))
    assert patch_error.value.status_code == 422

    with pytest.raises(HTTPException) as delete_error:
        adventures.delete_adventure("../../outside", x_confirm_delete="yes")
    assert delete_error.value.status_code == 422
    assert json.loads(outside.read_text(encoding="utf-8")) == sentinel


def test_encoded_slash_traversal_does_not_match_adventure_route():
    from fastapi.testclient import TestClient

    from apps.api.main import app

    response = TestClient(app).get("/api/adventures/..%2F..%2Foutside")
    assert response.status_code == 404
