"""Private preparation must not be inspectable through legacy source review APIs."""

import pytest
from fastapi import HTTPException

from apps.api.routers import adventure_manifests, adventures
from apps.api.services import adventures_meta as store
from apps.api.services.preparation_privacy import require_mutable_source


@pytest.fixture
def private_source(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    store.save_meta("private", {
        "private_preparation": True,
        "status": "ready",
        "manifest": {"ending": "SECRET_ENDING"},
        "prepared_bundle": {"digest": "a" * 64},
    })


@pytest.mark.parametrize("read", [
    adventure_manifests.inspect_manifest,
    adventure_manifests.compile_manifest,
    adventures.get_extraction_preview,
    adventures.get_extraction_markdown,
])
def test_private_definition_and_source_reads_are_absent(private_source, read):
    with pytest.raises(HTTPException) as error:
        read("private")
    assert error.value.status_code == 404
    assert error.value.detail == "Not found"


def test_player_cannot_publish_a_private_draft_by_supplying_reviewer(private_source):
    with pytest.raises(HTTPException) as error:
        adventure_manifests.publish_manifest("private", adventure_manifests.ManifestReview(
            reviewed_digest="b" * 64, reviewer="arbitrary player string",
        ))
    assert error.value.status_code == 404
    assert store.load_meta("private")["manifest"] == {"ending": "SECRET_ENDING"}


def test_prepared_versions_stay_immutable_for_running_saves(private_source):
    with pytest.raises(HTTPException) as error:
        require_mutable_source(store.load_meta("private"))
    assert error.value.status_code == 409
    require_mutable_source({"private_preparation": True, "status": "preparing"})
    require_mutable_source({"status": "ready", "manifest": {"legacy": True}})
