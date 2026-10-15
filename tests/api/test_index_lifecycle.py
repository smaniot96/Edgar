"""Worker receipts and retained manuscripts are fenced with the index attempt."""

import asyncio
from contextlib import closing
from unittest.mock import AsyncMock

import pytest
from qdrant_client import QdrantClient

from apps.api.routers import adventures
from apps.api.services import (
    adventures_meta as store,
    campaign_generator as generator,
    campaign_sources,
)
from db.vector.embeddings import VECTOR_SIZE
from db.vector.source_status import inspect_source
from db.vector.versions import SupersededIngestion, active_index_version, read_index_state
from ingestion import embed

VECTOR = [1.0] + [0.0] * (VECTOR_SIZE - 1)


@pytest.fixture
def isolated_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    for module, field in [(store, "ADVENTURES_DIR"), (adventures, "_ADVENTURES_DIR")]:
        monkeypatch.setattr(module, field, tmp_path / "adventures")
    for module, field in [
        (store, "PDF_DIR"),
        (adventures, "_PDF_DIR"),
        (campaign_sources, "PDF_DIR"),
    ]:
        monkeypatch.setattr(module, field, tmp_path / "pdfs")
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(
            campaign_sources,
            "active_index_version",
            lambda slug: active_index_version(slug, client=client),
        )
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        monkeypatch.setattr(
            campaign_sources,
            "inspect_source",
            lambda slug, **kwargs: inspect_source(slug, client=client, **kwargs),
        )
        yield client


def manuscript(label):
    return [
        (
            n,
            f"# Chapter {n}: {label}\n\nThe party explores the {label} landscape beside the river, meets the village guard and chooses a path through the quiet forest.",
        )
        for n in range(1, 4)
    ]


async def generate(monkeypatch, label):
    monkeypatch.setattr(
        generator,
        "generate_campaign",
        AsyncMock(return_value=(label, f"Premise {label}", manuscript(label))),
    )
    await generator.build_generated_campaign("authored", label, "River", "small")


async def test_failed_reauthored_generation_retains_previous_receipt_and_canonical_manuscript(
    isolated_lifecycle, monkeypatch
):
    store.save_meta(
        "authored", {"status": "processing", "rules_edition": "2014", "generation": {"kind": "ai"}}
    )
    await generate(monkeypatch, "original")
    before = store.load_meta("authored")
    canonical = store.ADVENTURES_DIR / "authored" / "authored.md"
    manuscript_before = canonical.read_text()
    active_before = read_index_state(isolated_lifecycle, "authored")["sources"]["authored"][
        "active"
    ]

    def fail(_):
        raise ConnectionError("disposable provider failure")

    monkeypatch.setattr(embed, "get_embeddings", fail)
    await generate(monkeypatch, "replacement")
    after = store.load_meta("authored")
    assert after["status"] == "ready"
    assert after["ingestion"] == before["ingestion"]
    assert after["last_attempt"]["status"] == "failed"
    assert after["last_attempt"]["failure_code"] == "generation_error"
    assert canonical.read_text() == manuscript_before
    assert campaign_sources.source_read("authored").status == "ready"
    assert (
        read_index_state(isolated_lifecycle, "authored")["sources"]["authored"]["active"]
        == active_before
    )
    assert not (store.PDF_DIR / "authored.pdf").exists()
    from fastapi import BackgroundTasks, HTTPException

    background = BackgroundTasks()
    assert adventures._retry_adventure("authored", background).status == "processing"
    assert background.tasks[0].kwargs.get("attempt_id")
    with pytest.raises(HTTPException) as caught:
        adventures._retry_adventure("authored", BackgroundTasks())
    assert caught.value.status_code == 409


async def test_generation_finishing_late_cannot_replace_newer_manuscript_or_metadata(
    isolated_lifecycle, monkeypatch
):
    store.save_meta("authored", {"status": "processing", "rules_edition": "2014"})
    started, release = asyncio.Event(), asyncio.Event()

    async def author(title, *_):
        if title == "obsolete":
            started.set()
            await release.wait()
        return title, f"Premise {title}", manuscript(title)

    monkeypatch.setattr(generator, "generate_campaign", author)
    first = store.begin_attempt("authored")
    old = asyncio.create_task(
        generator.build_generated_campaign(
            "authored", "obsolete", "River", "small", attempt_id=first
        )
    )
    await started.wait()
    second = store.begin_attempt("authored")
    await generator.build_generated_campaign(
        "authored", "accepted", "River", "small", attempt_id=second
    )
    metadata = store.load_meta("authored")
    release.set()
    await old
    assert store.load_meta("authored") == metadata
    assert metadata["title"] == "accepted" and metadata["status"] == "ready"
    canonical = (store.ADVENTURES_DIR / "authored" / "authored.md").read_text()
    assert "accepted" in canonical and "obsolete" not in canonical
    assert campaign_sources.source_read("authored").status == "ready"


async def test_restart_after_index_activation_resolves_durable_receipt_and_staged_manuscript(
    isolated_lifecycle, monkeypatch
):
    store.save_meta("authored", {"status": "processing", "rules_edition": "2014"})
    await generate(monkeypatch, "original")
    canonical = store.ADVENTURES_DIR / "authored" / "authored.md"
    old_manuscript = canonical.read_text()
    finish = store.finish_attempt

    def crash(*args, **kwargs):
        if kwargs.get("success"):
            raise SystemExit("simulated process exit after atomic index activation")
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", crash)
    with pytest.raises(SystemExit):
        await generate(monkeypatch, "accepted")
    assert canonical.read_text() == old_manuscript
    assert store.load_meta("authored")["status"] == "processing"
    active = campaign_sources.active_metadata("authored")
    assert active["title"] == "accepted" and active["status"] == "ready"
    assert campaign_sources.source_read("authored").status == "ready"
    assert campaign_sources.source_read("authored").extraction_preview_current
    assert "accepted" in adventures._extraction_path("authored", "authored.md").read_text()
    assert adventures.get_extraction_preview("authored")["source_current"]
    store.fail_stale_processing()
    assert campaign_sources.active_metadata("authored")["title"] == "accepted"
    assert campaign_sources.source_read("authored").status == "ready"


async def test_restart_before_activation_preserves_ready_receipt_and_fences_old_attempt(
    isolated_lifecycle, monkeypatch
):
    store.save_meta("authored", {"status": "processing", "rules_edition": "2014"})
    await generate(monkeypatch, "original")
    token = store.begin_attempt("authored")
    assert campaign_sources.source_read("authored").status == "ready"
    assert store.fail_stale_processing() == ["authored"]
    assert store.load_meta("authored")["status"] == "ready"
    assert campaign_sources.source_read("authored").status == "ready"
    with pytest.raises(SupersededIngestion):
        with store.attempt_guard("authored", token):
            pytest.fail("An obsolete process retained ownership after startup reconciliation")


async def test_failed_attempt_after_recovered_activation_preserves_the_new_accepted_receipt(
    isolated_lifecycle, monkeypatch
):
    store.save_meta("authored", {"status": "processing", "rules_edition": "2014"})
    await generate(monkeypatch, "original")
    finish = store.finish_attempt

    def crash(*args, **kwargs):
        if kwargs.get("success"):
            raise SystemExit("accepted before worker exit")
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", crash)
    with pytest.raises(SystemExit):
        await generate(monkeypatch, "accepted")
    monkeypatch.setattr(store, "finish_attempt", finish)
    monkeypatch.setattr(
        embed,
        "get_embeddings",
        lambda _: (_ for _ in ()).throw(ConnectionError("third attempt failed")),
    )
    await generate(monkeypatch, "rejected")
    assert campaign_sources.active_metadata("authored")["title"] == "accepted"
    assert campaign_sources.source_read("authored").status == "ready"
    assert "accepted" in adventures._extraction_path("authored", "authored.md").read_text()


def test_compose_vector_callers_share_one_lock_mount():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    for name in ("api", "ingestion", "rules_engine"):
        service = compose["services"][name]
        assert "EDGAR_INDEX_LOCK_DIR=/index-locks" in service["environment"]
        assert "./data/.index-locks:/index-locks" in service["volumes"]
