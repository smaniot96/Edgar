"""Extract -> chunk -> embed -> upsert a PDF into a Qdrant collection.

Each attempt stages points under a digest of the complete source payloads, chunk boundaries,
reviewed manifest, edition and embedding model. Existing verified points of exactly that version
are reused on retry. All stored payloads/vectors are validated before one unembedded Qdrant
control point activates the version. Readers use the active route; partial stages stay hidden.
Failures keep the previous route and unrelated sources intact. After activation obsolete points
of only the replaced source are pruned. ``replace`` remains an explicit PDF review flag; safe
source replacement and resume apply to every ingestion entry point.

Payload: `{text, page, page_end, source, chunk_index, section, chapter, heading_path,
stat_block}`; retrieval treats every field except `text`/`page`/`source` as optional so older
collections keep working. A payload index on `page` is created with the collection so the
session intro can read the first pages in order (`order_by=page`).

Batching: embeddings are computed `BATCH_SIZE` chunks at a time with a small `BATCH_DELAY_SEC`
between batches as a courtesy to OpenAI rate limits. The retry policy lives in
`db.vector.embeddings`; here we just propagate failures after logging which batch died.
"""

import hashlib
import math
import re
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
)

from db.vector import ensure_collection, get_embeddings, get_qdrant_client
from db.vector.collections import is_rules_collection
from db.vector.creatures import creature_payload_fields
from db.vector.embeddings import VECTOR_SIZE
from db.vector.versions import (
    SupersededIngestion,
    begin_index,
    index_lock,
    index_version,
    promote_index,
    read_index_state,
    version_filter,
)
from edgar_core.log_safety import error_summary
from edgar_core.sources import SourceEdition
from ingestion.chunk import chunk_page_runs
from ingestion.extract import (
    ExtractedPage,
    Extraction,
    extract_document,
    load_reviewed_extraction,
    save_extraction,
)
from ingestion.paths import default_markdown_dir
from ingestion.quality import check_text, require_separate_stat_chunks
from ingestion.readiness import prepare_report, require_ready, stamp_chunks

BATCH_SIZE = 50
BATCH_DELAY_SEC = 0.5


def run_pipeline(
    pdf_path: str | Path,
    collection_name: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    save_markdown: bool = True,
    markdown_dir: Path | None = None,
    replace: bool = False,
    reviewed_extraction: str | Path | None = None,
    preview_dir: Path | None = None,
    on_prepared: Callable[[dict], None] | None = None,
    reviewed_identity: tuple[str, str] | None = None,
    rules_edition: SourceEdition | None = None,
    reviewed_manifest: dict | None = None,
    publication_guard: Callable | None = None,
) -> int:
    """Validate and persist the selected extraction before any embedding or client write."""
    pdf_path = Path(pdf_path)
    source = pdf_path.stem

    needs_review = replace or is_rules_collection(collection_name) or collection_name == "dnd_rules"
    extraction = (
        load_reviewed_extraction(pdf_path, reviewed_extraction)
        if reviewed_extraction is not None
        else extract_document(pdf_path)
    )
    if rules_edition is not None:
        if rules_edition not in ("2014", "2024", "unspecified"):
            raise ValueError("Declare rules edition 2014, 2024 or unspecified.")
        if extraction.rules_edition not in ("unspecified", rules_edition):
            raise ValueError("The reviewed source declares a different rules edition.")
        extraction.rules_edition = rules_edition
    if (
        reviewed_identity is not None
        and (extraction.source_sha256, extraction.text_sha256()) != reviewed_identity
    ):
        raise ValueError(
            "The inspected extraction changed. Inspect the current preview again before indexing."
        )
    out_dir = preview_dir
    if out_dir is None and (save_markdown or (needs_review and reviewed_extraction is None)):
        out_dir = markdown_dir or default_markdown_dir()
    manifest_payloads = None
    if reviewed_manifest is not None:
        if reviewed_extraction is None:
            raise ValueError("Manifest indexing requires its exact reviewed extraction bundle.")
        from ingestion.manifest import manifest_chunks

        manifest_payloads = manifest_chunks(extraction, reviewed_manifest)
    chunks = _prepare_extraction(
        extraction,
        "pdf",
        chunk_size,
        overlap,
        out_dir,
        on_prepared,
        prepared_chunks=manifest_payloads,
    )
    if needs_review and reviewed_extraction is None:
        out_path, report_path = out_dir / f"{source}.md", out_dir / f"{source}.extraction.json"
        raise ValueError(
            "Rulebook indexing and PDF replacement require a reviewed extraction preview. "
            f"Inspect {out_path} and {report_path}, then pass --reviewed-extraction {report_path}."
        )
    return _embed_and_upsert(
        chunks, collection_name, replace=replace, publication_guard=publication_guard
    )


def ingest_pages(
    pages: list[tuple[int, str]],
    source: str,
    collection_name: str,
    chunk_size: int = 1000,
    overlap: int = 100,
    replace: bool = False,
    preview_dir: Path | None = None,
    on_prepared: Callable[[dict], None] | None = None,
    expected_pages: int | None = None,
    rules_edition: SourceEdition = "unspecified",
    publication_guard: Callable | None = None,
) -> int:
    """Chunk + embed + upsert already-extracted `(page, text)` pages (e.g. AI-generated text).

    Mirrors `run_pipeline` but skips the PDF extraction step, so callers that already have text
    (a generated campaign, a markdown doc) can reuse the same chunking/embedding/idempotency.
    """
    extraction = Extraction(
        source,
        "",
        expected_pages if expected_pages is not None else len(pages),
        [ExtractedPage(n, text, "generated", check_text(text)) for n, text in pages],
        rules_edition=rules_edition,
    )
    extraction.source_sha256 = extraction.text_sha256()
    issues = []
    for page, text in pages:
        body = re.sub(r"(?m)^#{1,6}\s+.*$", "", text).strip()
        if expected_pages is not None and len(re.findall(r"\b\w+\b", body)) < 12:
            issues.append(
                {
                    "code": "incomplete_generated_page",
                    "page": page,
                    "message": "A generated chapter contains too little manuscript text. Retry generation.",
                }
            )
    chunks = _prepare_extraction(
        extraction,
        "generated" if expected_pages is not None else "text",
        chunk_size,
        overlap,
        preview_dir,
        on_prepared,
        issues,
    )
    return _embed_and_upsert(
        chunks, collection_name, replace=replace, publication_guard=publication_guard
    )


def _prepare_extraction(
    extraction: Extraction,
    source_kind: str,
    chunk_size: int,
    overlap: int,
    preview_dir: Path | None,
    on_prepared: Callable[[dict], None] | None,
    extra_issues: list[dict] | None = None,
    prepared_chunks: list[dict] | None = None,
    degraded_page_budget: float | None = None,
) -> list[dict]:
    """One quality/coverage gate shared by PDF and generated manuscript entry points."""
    chunks = (
        prepared_chunks
        if prepared_chunks is not None
        else chunk_page_runs(
            extraction.text_pages(),
            source=extraction.source,
            chunk_size=chunk_size,
            overlap=overlap,
        )
    )
    issues = list(extra_issues or [])
    try:
        require_separate_stat_chunks(chunks)
    except ValueError as exc:
        issues.append({"code": "stat_chunk_integrity", "message": str(exc)})
    report = prepare_report(
        extraction,
        source_kind=source_kind,
        chunks=chunks,
        extra_issues=issues,
        degraded_page_budget=degraded_page_budget,
    )
    for degraded in report.get("degraded_pages", []):
        logger.warning(
            "Page {} kept as degraded ({}); within the allowance of {} pages",
            degraded["page"],
            ", ".join(degraded["causes"]),
            report.get("degraded_page_allowance"),
        )
    for skipped in report.get("non_chunkable_pages", []):
        logger.warning(
            "Page {} has {} characters but no chunkable text ({}); not required for coverage",
            skipped["page"],
            skipped["chars"],
            skipped["reason"],
        )
    if report["quality_passed"]:
        stamp_chunks(chunks, report)
        report["index_version"] = index_version([_chunk_payload(c) for c in chunks])
    if preview_dir is not None:
        out_path, report_path = save_extraction(extraction, preview_dir, readiness=report)
        logger.info("Saved extraction preview to {} and {}", out_path, report_path)
    if on_prepared is not None:
        on_prepared(report)
    require_ready(report)
    return chunks


def _chunk_payload(c: dict) -> dict:
    payload = {
        "text": c["text"],
        "page": c["page"],
        "source": c["source"],
        "chunk_index": c["index"],
    }
    for key in (
        "page_end",
        "source_pages",
        "source_sha256",
        "extraction_sha256",
        "rules_edition",
        "excluded_pages",
        "exclusions_sha256",
        "text_corrections",
        "corrections_sha256",
        "corrected_pages",
        "section",
        "chapter",
        "heading_path",
        "stat_block",
        "rule_policy",
        "manifest_sha256",
        "passage_id",
        "scene_id",
        "scene_ids",
        "entity_ids",
        "audience",
        "requires",
        "span_start",
        "span_end",
        "text_sha256",
        "index_version",
    ):
        if c.get(key) is not None:
            payload[key] = c[key]
    payload.update(creature_payload_fields(c))
    return payload


def _ensure_collection(client, collection_name: str) -> None:
    """Create the collection (Cosine, VECTOR_SIZE) if missing; ensure the `page` payload index."""
    if ensure_collection(client, collection_name):
        logger.info("Created collection {}", collection_name)
    try:
        # Idempotent; lets the session intro scroll the first pages with order_by=page.
        client.create_payload_index(
            collection_name=collection_name,
            field_name="page",
            field_schema=PayloadSchemaType.INTEGER,
        )
    except Exception as e:  # index is an optimisation only
        logger.warning(
            "Could not create page index on {}: {}: {}", collection_name, type(e).__name__, e
        )


def _prune_stale(client, collection_name: str, source: str, keep_ids: set[int]) -> int:
    """Delete points of `source` in the collection whose id is not in `keep_ids`."""
    stale: list = []
    offset = None
    source_filter = Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])
    while True:
        points, offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=source_filter,
            limit=1000,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        stale.extend(p.id for p in points if p.id not in keep_ids)
        if offset is None:
            break
    for i in range(0, len(stale), 1000):
        client.delete(
            collection_name=collection_name,
            points_selector=PointIdsList(points=stale[i : i + 1000]),
        )
    return len(stale)


def _embed_and_upsert(
    chunks: list[dict],
    collection_name: str,
    *,
    replace: bool = False,
    publication_guard: Callable | None = None,
    embedding_provider: Callable | None = None,
) -> int:
    """Stage one immutable source version, validate its stored points, then activate it.

    Replacement is always source-scoped; ``replace`` remains a compatibility/review flag.
    Failed and interrupted stages never enter the active read route. An identical retry
    reuses verified points, including batches accepted before an interrupted response.
    """
    sources = {c["source"] for c in chunks}
    if len(sources) != 1 or not chunks:
        raise ValueError("An index attempt must contain exactly one nonempty source.")
    source = next(iter(sources))
    payloads = [_chunk_payload(c) for c in chunks]
    version = index_version(payloads)
    payloads = [{**p, "index_version": version} for p in payloads]
    ids = [
        int(hashlib.sha256(f"{version}:{i}".encode()).hexdigest()[:16], 16)
        for i in range(len(chunks))
    ]
    client = get_qdrant_client()
    _ensure_collection(client, collection_name)
    token = begin_index(client, collection_name, source, version, guard=publication_guard)

    def usable(point, payload):
        vector = point.vector
        return (
            point.payload == payload
            and isinstance(vector, list)
            and len(vector) == VECTOR_SIZE
            and any(vector)
            and all(math.isfinite(v) for v in vector)
        )

    def validate():
        points = []
        offset = None
        while True:
            batch, offset = client.scroll(
                collection_name=collection_name,
                scroll_filter=version_filter(source, version),
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            points.extend(batch)
            if offset is None:
                break
        expected = dict(zip(ids, payloads, strict=True))
        if len(points) != len(ids) or any(
            p.id not in expected or not usable(p, expected[p.id]) for p in points
        ):
            raise ValueError("Staged index does not match its complete source version.")

    def cleanup():
        try:
            _prune_stale(client, collection_name, source, set(ids))
        except Exception:
            # The active route already excludes obsolete points. Cleanup can be retried by
            # the next identical ingest; it must not report a successful promotion as failed.
            logger.exception("Inactive source point cleanup failed for {}", source)

    total_ingested = 0
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        batch_ids = ids[i : i + BATCH_SIZE]
        batch_payloads = payloads[i : i + BATCH_SIZE]
        stored = {
            p.id: p
            for p in client.retrieve(
                collection_name=collection_name, ids=batch_ids, with_payload=True, with_vectors=True
            )
        }
        missing = [
            (pid, payload)
            for pid, payload in zip(batch_ids, batch_payloads, strict=True)
            if pid not in stored or not usable(stored[pid], payload)
        ]
        texts = [p["text"] for _, p in missing]

        try:
            embeddings = (embedding_provider or get_embeddings)(texts) if texts else []
            if len(embeddings) != len(texts) or any(
                len(v) != VECTOR_SIZE or not all(math.isfinite(x) for x in v) or not any(v)
                for v in embeddings
            ):
                raise ValueError(
                    "Embedding response does not cover every chunk with a usable vector."
                )
        except Exception as e:
            logger.error("Embedding failed for batch {}: {}", i, error_summary(e))
            raise

        points = [
            PointStruct(
                id=pid,
                vector=embeddings[j],
                payload=payload,
            )
            for j, (pid, payload) in enumerate(missing)
        ]

        try:
            with index_lock(collection_name):
                state = read_index_state(client, collection_name)
                if (state["sources"][source].get("pending") or {}).get("token") != token:
                    raise SupersededIngestion(
                        "This source ingestion was superseded by a newer attempt."
                    )
                if points:
                    client.upsert(collection_name=collection_name, points=points, wait=True)
        except Exception as e:
            logger.error("Upsert failed for batch {}: {}", i, e)
            raise

        total_ingested += len(batch)
        logger.info("Upserted batch {}-{} ({} chunks)", i, i + len(batch), total_ingested)

        # Skip the delay after the final batch.
        if i + BATCH_SIZE < len(chunks):
            time.sleep(BATCH_DELAY_SEC)

    receipt = {
        key: payloads[0].get(key)
        for key in (
            "source_sha256",
            "extraction_sha256",
            "manifest_sha256",
            "rules_edition",
            "excluded_pages",
            "exclusions_sha256",
        )
    }
    # Reviewed corrections appear in the receipt only when present, so ordinary receipts keep their shape.
    for key in ("text_corrections", "corrections_sha256"):
        if payloads[0].get(key) is not None:
            receipt[key] = payloads[0][key]
    receipt["chunk_count"] = len(chunks)
    promote_index(
        client,
        collection_name,
        source,
        version,
        token,
        receipt,
        validate=validate,
        guard=publication_guard,
        cleanup=cleanup,
    )

    return total_ingested
