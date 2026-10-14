"""Adventure lifecycle metadata stays intact during failed and concurrent writes."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from apps.api.routers import adventures
from apps.api.services import adventures_meta


@pytest.fixture
def metadata_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path)
    return tmp_path


def test_failed_metadata_serialization_keeps_previous_state(metadata_dir):
    previous = {"title": "The Keep", "status": "ready", "chunks": 5}
    adventures_meta.save_meta("keep", previous)
    with pytest.raises(TypeError):
        adventures_meta.save_meta("keep", {"status": "processing", "bad": object()})
    assert adventures_meta.load_meta("keep") == previous
    assert sorted(p.name for p in metadata_dir.iterdir()) == ["keep.json"]


def test_metadata_is_complete_before_atomic_publish(metadata_dir, monkeypatch):
    previous = {"title": "The Keep", "status": "processing"}
    updated = {"title": "The Keep", "status": "ready", "chunks": 5}
    adventures_meta.save_meta("keep", previous)
    replace = adventures_meta.Path.replace

    def inspect_publish(path, target):
        # A reader just before the rename sees the previous complete document.
        assert adventures_meta.load_meta("keep") == previous
        return replace(path, target)

    monkeypatch.setattr(adventures_meta.Path, "replace", inspect_publish)
    adventures_meta.save_meta("keep", updated)
    assert adventures_meta.load_meta("keep") == updated
    assert adventures_meta.metadata_slugs() == {"keep"}


def test_same_title_requests_reserve_distinct_slugs(metadata_dir, monkeypatch):
    # Force every request to see the same initial inventory, reproducing the race.
    requests = 8
    barrier = Barrier(requests)
    monkeypatch.setattr(adventures, "_all_collection_names", lambda: [])

    def simultaneous_inventory():
        barrier.wait(timeout=5)
        return set()

    monkeypatch.setattr(adventures, "metadata_slugs", simultaneous_inventory)

    def reserve(index):
        return adventures._reserve_slug("keep", {"status": "processing", "request": index})

    with ThreadPoolExecutor(max_workers=requests) as pool:
        slugs = list(pool.map(reserve, range(requests)))
    assert len(set(slugs)) == requests
    assert adventures_meta.metadata_slugs() == set(slugs)
    assert {adventures_meta.load_meta(slug)["request"] for slug in slugs} == set(range(requests))
    assert len(list(metadata_dir.iterdir())) == requests


def test_reservation_does_not_overwrite_metadata_or_existing_collection(metadata_dir, monkeypatch):
    adventures_meta.save_meta("keep", {"status": "ready"})
    assert not adventures_meta.reserve_meta("keep", {"status": "processing"})
    assert adventures_meta.load_meta("keep") == {"status": "ready"}
    monkeypatch.setattr(adventures, "_all_collection_names", lambda: ["keep_2"])
    assert adventures._reserve_slug("keep", {"status": "processing"}) == "keep_3"
    assert sorted(p.name for p in metadata_dir.iterdir()) == ["keep.json", "keep_3.json"]
