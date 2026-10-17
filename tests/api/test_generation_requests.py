"""Retry-safe identity for AI campaign generation (EDG-157).

Uses a disposable adventure data directory and records dispatched work instead of calling a model.
No database or Docker is involved: adventure metadata is file-backed.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import BackgroundTasks, HTTPException
from pydantic import ValidationError

from apps.api.routers import adventures
from apps.api.schemas.adventure import AdventureGenerateRequest
from apps.api.services import adventures_meta as store, generation_requests

REQUEST_ID = "0b6f5c1e-8d0a-4f3e-9a51-2c7d4e9b1a20"
OTHER_ID = "7e2a9d43-1c5b-4b8e-8f60-9d3a1c5e7b42"


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Disposable metadata root; no collections, setup or provider are touched."""
    monkeypatch.setattr(store, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventures, "_all_collection_names", lambda: [])

    async def no_setup(_setup):
        return None

    monkeypatch.setattr("apps.api.services.preparation_commission.capture_setup", no_setup)
    return tmp_path / "adventures"


def _body(**overrides) -> AdventureGenerateRequest:
    fields = {"theme": "a haunted lighthouse", "size": "small", "request_id": REQUEST_ID}
    fields.update(overrides)
    return AdventureGenerateRequest(**fields)


async def _generate(body: AdventureGenerateRequest) -> tuple[dict, list]:
    background = BackgroundTasks()
    response = await adventures.generate_adventure(body, background)
    return response.model_dump(), background.tasks


def _module_count() -> int:
    return len(store.metadata_slugs())


@pytest.mark.asyncio
async def test_without_identity_each_call_keeps_the_original_behaviour(data_dir):
    first, first_tasks = await _generate(_body(request_id=None))
    second, second_tasks = await _generate(_body(request_id=None))
    assert first["slug"] != second["slug"]
    assert len(first_tasks) == len(second_tasks) == 1
    assert _module_count() == 2
    stored = store.load_meta(first["slug"])
    assert "request_id" not in stored["generation"]


@pytest.mark.asyncio
async def test_same_identity_and_body_replays_one_module_and_dispatches_once(data_dir):
    first, first_tasks = await _generate(_body())
    replay, replay_tasks = await _generate(_body())

    assert replay == first
    assert len(first_tasks) == 1
    assert replay_tasks == []
    assert _module_count() == 1
    stored = store.load_meta(first["slug"])
    assert stored["generation"]["request_id"] == REQUEST_ID
    assert len(stored["generation"]["request_hash"]) == 64


@pytest.mark.asyncio
async def test_replay_reports_the_stored_state_not_a_fresh_stub(data_dir):
    first, _ = await _generate(_body(title="The Lighthouse"))
    meta = store.load_meta(first["slug"])
    meta["status"] = "ready"
    store.save_meta(first["slug"], meta)

    replay, tasks = await _generate(_body(title="The Lighthouse"))
    assert replay["slug"] == first["slug"]
    assert replay["status"] == "ready"
    assert replay["title"] == "The Lighthouse"
    assert tasks == []


@pytest.mark.asyncio
async def test_replay_does_not_depend_on_current_setup(data_dir, monkeypatch):
    first, _ = await _generate(_body())

    async def gone(_setup):
        raise HTTPException(status_code=422, detail="Choose a hero and a valid preparation budget.")

    monkeypatch.setattr("apps.api.services.preparation_commission.capture_setup", gone)
    replay, tasks = await _generate(_body())
    assert replay["slug"] == first["slug"]
    assert tasks == []


@pytest.mark.asyncio
async def test_same_identity_with_a_different_body_is_a_safe_conflict(data_dir):
    first, tasks = await _generate(_body())
    with pytest.raises(HTTPException) as caught:
        await _generate(_body(size="gigantic"))

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "validation_failed"
    assert _module_count() == 1
    assert len(tasks) == 1
    assert store.load_meta(first["slug"])["generation"]["size"] == "small"


@pytest.mark.asyncio
async def test_different_identities_create_different_modules(data_dir):
    first, _ = await _generate(_body())
    second, tasks = await _generate(_body(request_id=OTHER_ID))
    assert second["slug"] != first["slug"]
    assert len(tasks) == 1
    assert _module_count() == 2


def test_identity_is_bounded_and_shaped(data_dir):
    with pytest.raises(ValidationError):
        _body(request_id="short")
    with pytest.raises(ValidationError):
        _body(request_id="x" * 37)
    with pytest.raises(ValidationError):
        _body(request_id="has spaces in it, not allowed")
    assert _body(request_id=None).request_id is None


@pytest.mark.asyncio
async def test_concurrent_duplicates_yield_one_module_and_one_dispatch(data_dir):
    requests = 8
    barrier = Barrier(requests)
    body = _body()
    identity = generation_requests.identity_for(body)
    assert identity is not None
    meta_template = {
        "status": "processing",
        "generation": {"kind": "ai", "size": "small", "theme": body.theme, "title_hint": ""},
    }

    def claim_once(_index):
        barrier.wait(timeout=5)
        meta = {**meta_template, "generation": dict(meta_template["generation"])}
        return generation_requests.claim(identity, "lighthouse", meta, adventures._reserve_slug)

    with ThreadPoolExecutor(max_workers=requests) as pool:
        results = list(pool.map(claim_once, range(requests)))

    slugs = {slug for slug, _, _ in results}
    created = [slug for slug, _, was_created in results if was_created]
    assert len(slugs) == 1
    assert len(created) == 1
    assert _module_count() == 1
    assert store.load_meta(created[0])["generation"]["request_id"] == REQUEST_ID


@pytest.mark.asyncio
async def test_concurrent_route_duplicates_dispatch_once(data_dir):
    body = _body()
    background_runs = []

    async def run():
        background = BackgroundTasks()
        response = await adventures.generate_adventure(body, background)
        background_runs.append(len(background.tasks))
        return response.slug

    slugs = await asyncio.gather(*(run() for _ in range(6)))
    assert len(set(slugs)) == 1
    assert sorted(background_runs) == [0, 0, 0, 0, 0, 1]
    assert _module_count() == 1
