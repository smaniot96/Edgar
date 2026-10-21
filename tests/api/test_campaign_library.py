"""Library cleanup never mutates historical preparation or saved-game data."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from apps.api.routers import adventures
from apps.api.schemas.adventure import AdventureLibraryUpdate, AdventureRead
from apps.api.services import adventures_meta, campaign_library


@pytest.fixture
def library(tmp_path, monkeypatch):
    directory = tmp_path / "adventures"
    directory.mkdir()
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", directory)
    return directory


def test_archive_and_restore_keep_source_bytes_and_checkpoints(library):
    metadata = library / "old_campaign.json"
    metadata.write_text('{"status":"failed","paid_response":"retained"}\n')
    checkpoint = library / "old_campaign" / "graph.sqlite3"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"retained paid checkpoint")
    original = {path: path.read_bytes() for path in (metadata, checkpoint)}
    campaign_library.set_archived("old_campaign", True)
    campaign_library.set_archived("other_campaign", True)
    assert campaign_library.archived_slugs() == {"old_campaign", "other_campaign"}
    campaign_library.set_archived("old_campaign", False)
    assert campaign_library.archived_slugs() == {"other_campaign"}
    assert {path: path.read_bytes() for path in original} == original
    assert adventures_meta.metadata_slugs() == {"old_campaign"}


def test_concurrent_archive_preferences_do_not_lose_other_campaigns(library):
    slugs = [f"campaign_{index}" for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda slug: campaign_library.set_archived(slug, True), slugs))
    assert campaign_library.archived_slugs() == set(slugs)


def test_library_patch_archives_immutable_failed_source_without_saving_metadata(library, monkeypatch):
    source = AdventureRead(slug="old_campaign", title="Old campaign", status="failed")
    monkeypatch.setattr(adventures, "_get_adventure", lambda slug: source)
    monkeypatch.setattr(
        adventures, "_save_adventure_metadata",
        lambda *args: pytest.fail("Library preferences must not edit source metadata"),
    )
    result = adventures.update_adventure_library(
        "old_campaign", AdventureLibraryUpdate(archived=True)
    )
    assert result.library_archived is True
    assert campaign_library.archived_slugs() == {"old_campaign"}


def test_running_preparation_cannot_be_archived(library, monkeypatch):
    monkeypatch.setattr(
        adventures, "_get_adventure",
        lambda slug: AdventureRead(slug=slug, title="Running", status="processing"),
    )
    with pytest.raises(HTTPException) as error:
        adventures.update_adventure_library("running", AdventureLibraryUpdate(archived=True))
    assert error.value.status_code == 409
    assert not campaign_library.archived_slugs()


def test_display_title_overlay_preserves_source_and_archive_preferences(library, monkeypatch):
    metadata = library / "lost_mine.json"
    metadata.write_text('{"title":"Lost Mine - validation","status":"processing"}\n')
    original = metadata.read_bytes()
    source = AdventureRead(slug="lost_mine", title="Lost Mine - validation", status="processing")
    monkeypatch.setattr(adventures, "_get_adventure", lambda slug: source)
    campaign_library.set_archived("older_copy", True)
    result = adventures.update_adventure_library(
        "lost_mine", AdventureLibraryUpdate(title=" Lost Mine of Phandelver ")
    )
    assert result.title == "Lost Mine of Phandelver"
    assert result.library_archived is False
    assert campaign_library.display_title("lost_mine", "fallback") == result.title
    assert campaign_library.archived_slugs() == {"older_copy"}
    campaign_library.set_archived("older_copy", False)
    assert campaign_library.display_title("lost_mine", "fallback") == result.title
    assert metadata.read_bytes() == original
