"""Publish exact independently reviewed semantic assembly through the existing index primitive."""

import json
from contextlib import contextmanager

from openai import OpenAI

from edgar_core.config import EMBEDDING_MODEL, OPENAI_API_KEY
from edgar_core.manifests import AdventureManifest, manifest_digest
from ingestion.embed import _embed_and_upsert, _prepare_extraction
from ingestion.manifest import manifest_chunks
from ingestion.readiness import report_summary

from .. import adventures_meta
from .compile import make_extraction
from .source import file_hash
from .store import NeedsAttention, Store, atomic_json, digest


def retained_embeddings(store, texts):
    """Retain paid vectors before indexing; ambiguous submissions never repeat blindly."""
    request = {"model": EMBEDDING_MODEL, "input": texts}
    task_id = digest({"node": "embedding", "request": request})
    row = store.task(task_id)
    store.check()
    if row and not row["response"]:
        raise NeedsAttention("Embedding submission needs reconciliation: " + task_id)
    if row:
        response = json.loads(row["response"])
    else:
        store.dispatch(task_id, "embedding", request)
        client = OpenAI(api_key=OPENAI_API_KEY, max_retries=0)
        try:
            result = client.embeddings.create(**request, extra_headers={"Idempotency-Key": task_id})
        finally:
            client.close()
        response = {**result.model_dump(mode="json"), "status": "completed"}
        store.response(task_id, response)
    store.check()
    values = [r["embedding"] for r in sorted(response["data"], key=lambda r: r["index"])]
    if len(values) != len(texts):
        raise NeedsAttention("Retained embedding response does not cover the requested texts.")
    store.accept(task_id, {"response_id": response.get("id"), "vector_count": len(values)})
    return values


def publish(store: Store):
    store.check()
    compiled = store.get("compiled")
    extraction = make_extraction(compiled)
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "campaign-graph-v2-independent-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    report = {}
    chunks = _prepare_extraction(
        extraction,
        "generated",
        1000,
        0,
        store.directory / "playable",
        report.update,
        prepared_chunks=manifest_chunks(extraction, manifest),
    )

    @contextmanager
    def guard():
        # No legacy preparation scheduler/attempt semantics participate in publication.
        with adventures_meta.lifecycle_lock(store.slug):
            store.check()
            yield

    count = _embed_and_upsert(
        chunks,
        store.slug,
        replace=True,
        publication_guard=guard,
        embedding_provider=lambda texts: retained_embeddings(store, texts),
    )
    receipt = {
        "workflow": "campaign-graph-2.0",
        "semantic_sha256": compiled["semantic_sha256"],
        "manifest_sha256": manifest.reviewed_sha256,
        "compiled_sha256": digest(compiled),
        "index_version": report["index_version"],
        "chunk_count": count,
        "request_sha256": store.state()["request_sha256"],
        "review_sha256": digest(store.get("review-coverage")),
        "findings_sha256": digest(store.get("findings")),
        "guide_sha256": file_hash(store.directory / "dm-guide.md"),
        "playable_sha256": file_hash(store.directory / "playable.json"),
    }
    store.put("publication", receipt)
    atomic_json(store.directory / "publication.json", receipt)
    with adventures_meta.lifecycle_lock(store.slug):
        store.check()
        meta = adventures_meta.load_meta(store.slug)
        if meta.get("preparation_workflow") != "campaign-graph-2.0":
            raise NeedsAttention("Publication source no longer belongs to this graph.")
        meta.update(
            status="ready",
            title=store.get("spine")["title"],
            description=store.get("spine")["hook"],
            level_range=store.get("spine")["level_recommendation"] or None,
            chunks=count,
            error=None,
            failure_code=None,
            manifest=manifest.model_dump(mode="json"),
            ingestion=report_summary(report),
            prepared_bundle={"bundle_sha256": compiled["semantic_sha256"], **receipt},
            preparation_publication_receipt=receipt,
        )
        meta["job"].update(
            state="completed",
            stage="Ready to play",
            retryable=False,
            checkpoint_available=True,
            retained_work="published",
            completed_chapters=len(store.get("bundles")["chapters"]),
            total_chapters=len(store.get("bundles")["chapters"]),
        )
        adventures_meta.save_meta(store.slug, meta)
