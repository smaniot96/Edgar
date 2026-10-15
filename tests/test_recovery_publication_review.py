"""Independent real extraction/index/restart recovery checks on disposable storage."""

import hashlib
from contextlib import closing

import pymupdf
import pytest
from fastapi import BackgroundTasks
from qdrant_client import QdrantClient

from apps.api.routers import adventures
from apps.api.schemas.adventure import AdventureRetryRequest
from apps.api.services import adventures_meta as store, campaign_sources
from db.vector.embeddings import VECTOR_SIZE
from db.vector.source_status import inspect_source
from db.vector.versions import active_index_version
from ingestion import embed
from ingestion.extract import extract_document, save_extraction


@pytest.fixture
def private_index(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    for module, name in [(store, "ADVENTURES_DIR"), (adventures, "_ADVENTURES_DIR")]:
        monkeypatch.setattr(module, name, tmp_path / "adventures")
    for module, name in [
        (store, "PDF_DIR"),
        (adventures, "_PDF_DIR"),
        (campaign_sources, "PDF_DIR"),
    ]:
        monkeypatch.setattr(module, name, tmp_path / "pdfs")
    store.PDF_DIR.mkdir()
    with closing(QdrantClient(":memory:")) as client:
        monkeypatch.setattr(client, "create_payload_index", lambda **_: None)
        monkeypatch.setattr(embed, "get_qdrant_client", lambda: client)
        monkeypatch.setattr(
            embed,
            "get_embeddings",
            lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts],
        )
        monkeypatch.setattr(embed, "BATCH_DELAY_SEC", 0)
        monkeypatch.setattr(
            campaign_sources,
            "active_index_version",
            lambda slug: active_index_version(slug, client=client),
        )
        monkeypatch.setattr(
            campaign_sources,
            "inspect_source",
            lambda slug, **kw: inspect_source(slug, client=client, **kw),
        )
        yield client


def write_pdf(path, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((40, 60), f"{label} village", fontsize=16)
        page.insert_text(
            (40, 100),
            "\n".join(
                f"The {label} guide follows route {i} past the old houses and asks the party to find a witness."
                for i in range(6)
            ),
        )
        document.save(path)


async def test_failed_pdf_replacement_is_inspectable_and_reviewed_retry_survives_activation_restart(
    private_index, tmp_path, monkeypatch
):
    pdf = store.PDF_DIR / "village.pdf"
    write_pdf(pdf, "original")
    original_bytes = pdf.read_bytes()
    store.save_meta("village", {"status": "failed", "title": "Village", "rules_edition": "2014"})
    token = store.begin_attempt("village", job_attempt=True, job_kind="ingestion")
    extraction = extract_document(pdf)
    preview = store.attempt_dir("village", token)
    save_extraction(extraction, preview)
    adventures._ingest_adventure(
        "village",
        str(pdf),
        str(preview / "village.extraction.json"),
        (extraction.source_sha256, extraction.text_sha256()),
        attempt_id=token,
    )
    old = store.load_meta("village")
    assert old["status"] == "ready"
    old_version = active_index_version("village", client=private_index)

    supplied = tmp_path / "supplied" / "different-name.pdf"
    write_pdf(supplied, "replacement")
    token = store.begin_attempt("village", job_attempt=True, job_kind="ingestion", reset_job=True)
    candidate = store.stage_attempt_pdf("village", token, supplied, "different-name.pdf")
    embeddings = embed.get_embeddings

    def lose_embedding_provider(_texts):
        raise ConnectionError("disposable embedding interruption")

    monkeypatch.setattr(embed, "get_embeddings", lose_embedding_provider)
    adventures._ingest_adventure("village", str(candidate), attempt_id=token)
    monkeypatch.setattr(embed, "get_embeddings", embeddings)
    assert pdf.read_bytes() == original_bytes
    assert store.load_meta("village")["ingestion"] == old["ingestion"]
    assert active_index_version("village", client=private_index) == old_version
    assert campaign_sources.source_read("village").status == "ready"
    inspected = adventures.get_extraction_preview("village")
    assert inspected["source_current"] and inspected["quality_passed"]
    assert inspected["source_sha256"] == hashlib.sha256(supplied.read_bytes()).hexdigest()
    assert "replacement" in adventures._extraction_path("village", "village.md").read_text()

    tasks = BackgroundTasks()
    adventures.retry_adventure(
        "village",
        tasks,
        AdventureRetryRequest(
            reviewed_source_sha256=inspected["source_sha256"],
            reviewed_extraction_sha256=inspected["extraction_sha256"],
        ),
    )
    new_token = store.load_meta("village")["attempt"]["id"]
    finish = store.finish_attempt

    def exit_after_activation(*args, **kwargs):
        if kwargs.get("success"):
            with pytest.raises(ValueError, match="cancellable|publication"):
                store.request_job_cancel("village")
            raise SystemExit("process stopped after activation")
        return finish(*args, **kwargs)

    monkeypatch.setattr(store, "finish_attempt", exit_after_activation)
    with pytest.raises(SystemExit, match="after activation"):
        await tasks()
    assert pdf.read_bytes() == original_bytes
    assert campaign_sources.source_read("village").status == "ready"
    assert store.fail_stale_processing() == ["village"]
    recovered = store.load_meta("village")
    assert recovered["artifact_attempt"] == new_token
    assert recovered["source_filename"] == "different-name.pdf"
    assert recovered["job"]["state"] == "completed"
    assert recovered["ingestion"]["source_sha256"] == inspected["source_sha256"]
    assert pdf.read_bytes() == supplied.read_bytes()
    assert campaign_sources.source_read("village").status == "ready"
    assert active_index_version("village", client=private_index) != old_version
