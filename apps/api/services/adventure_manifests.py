"""Review/publish manifests in the existing adventure metadata and extraction lifecycle."""

from edgar_core.manifests import (
    AdventureManifest,
    ManifestError,
    load_published_manifest,
    manifest_digest,
)

from . import adventures_meta as store
from .campaign_sources import active_metadata, validate_binding


def _bundle(slug: str):
    from ingestion.extract import load_reviewed_extraction

    validate_binding(slug)
    raw_meta = store.load_meta(slug)
    if raw_meta.get("private_preparation") is True:
        from .preparation_runtime import private_preparation_ready

        if not private_preparation_ready(slug, raw_meta):
            raise ManifestError("This private adventure is not ready to play yet.")
    meta = active_metadata(slug, raw_meta)
    if raw_meta.get("status") == "processing" and meta.get("artifact_attempt") != (
        raw_meta.get("attempt") or {}
    ).get("id"):
        raise ManifestError(
            "Adventure processing is in progress; wait before reviewing its manifest."
        )
    ingestion = meta.get("ingestion") or {}
    kind = ingestion.get("source_kind")
    if kind not in ("pdf", "generated", "text"):
        raise ManifestError("Source kind is unverified. Recover its canonical extraction first.")
    extraction = load_reviewed_extraction(
        store.source_pdf_path(slug, meta) if kind == "pdf" else None,
        _active_extraction_path(slug, meta),
    )
    if (
        ingestion.get("source_sha256") != extraction.source_sha256
        or ingestion.get("extraction_sha256") != extraction.text_sha256()
    ):
        raise ManifestError(
            "The extraction no longer matches adventure metadata. Recover it first."
        )
    return meta, extraction


def _active_extraction_path(slug: str, meta: dict):
    token = meta.get("artifact_attempt")
    staged = store.attempt_dir(slug, token) / f"{slug}.extraction.json" if token else None
    return (
        staged
        if staged is not None and staged.is_file()
        else store.ADVENTURES_DIR / slug / f"{slug}.extraction.json"
    )


def campaign_manifests(slugs: list[str]) -> dict[str, dict]:
    """Caller already enforces live bound-source guards; load source-pinned review receipts."""
    return {slug: load_published_manifest(active_metadata(slug)).model_dump() for slug in slugs}


def inspect_manifest(slug: str) -> dict:
    """Text appears in the inspection response only; metadata stores offsets/structure."""
    meta, extraction = _bundle(slug)
    value = meta.get("manifest_draft") or meta.get("manifest")
    if not value:
        raise ManifestError("Compile a manifest draft before inspecting or publishing it.")
    manifest = AdventureManifest.model_validate(value)
    from ingestion.manifest import validate_spans

    validate_spans(extraction, manifest)
    pages = dict(extraction.text_pages())
    return {
        "manifest": manifest.model_dump(),
        "reviewed_digest": manifest_digest(manifest),
        "passages": [
            {"passage_id": p.passage_id, "text": pages[p.page][p.start : p.end]}
            for p in manifest.passages
        ],
        "review_note": "Inspect every span, audience, scene, link and discovery. Automatic candidates are unreviewed.",
    }


def compile_draft(slug: str) -> dict:
    from ingestion.manifest import compile_manifest

    meta, extraction = _bundle(slug)
    draft = compile_manifest(extraction, rules_edition=meta.get("rules_edition", "unspecified"))
    meta["manifest_draft"] = draft.model_dump()
    store.save_meta(slug, meta)
    return inspect_manifest(slug)


def edit_draft(slug: str, manifest: AdventureManifest) -> dict:
    from ingestion.artifacts import text_digest
    from ingestion.manifest import validate_spans

    meta, extraction = _bundle(slug)
    validate_spans(extraction, manifest, check_digests=False)
    pages = dict(extraction.text_pages())
    for passage in manifest.passages:
        passage.text_sha256 = text_digest(pages[passage.page][passage.start : passage.end])
    if manifest.rules_edition != meta.get("rules_edition", "unspecified"):
        raise ManifestError("Manifest edition must match the declared source edition.")
    manifest.reviewed_by = manifest.reviewed_sha256 = None
    meta["manifest_draft"] = manifest.model_dump()
    store.save_meta(slug, meta)
    return inspect_manifest(slug)


def publish_manifest(slug: str, *, reviewed_digest: str, reviewer: str) -> dict:
    """Index exact reviewed passages, then publish metadata; failures never certify a draft."""
    from ingestion.embed import _embed_and_upsert, _prepare_extraction, run_pipeline
    from ingestion.manifest import manifest_chunks, review_manifest
    from ingestion.readiness import report_summary

    meta, extraction = _bundle(slug)
    draft = meta.get("manifest_draft")
    if draft is None:
        raise ManifestError("No editable manifest draft is awaiting review.")
    manifest = review_manifest(
        extraction, draft, reviewed_digest=reviewed_digest, reviewer=reviewer
    )
    if manifest.rules_edition != meta.get("rules_edition", "unspecified"):
        raise ManifestError("The source edition changed. Inspect and review the current draft.")
    token = store.begin_attempt(slug)

    def prepared(report):
        meta["ingestion"] = report_summary(report)
        store.prepare_attempt(slug, token, report, {"manifest": manifest.model_dump()})

    try:
        if meta["ingestion"]["source_kind"] == "pdf":
            chunks = run_pipeline(
                store.source_pdf_path(slug, meta),
                slug,
                replace=True,
                reviewed_extraction=_active_extraction_path(slug, meta),
                reviewed_identity=(manifest.source_sha256, manifest.extraction_sha256),
                reviewed_manifest=manifest.model_dump(),
                rules_edition=manifest.rules_edition,
                save_markdown=False,
                on_prepared=prepared,
                publication_guard=lambda: store.attempt_guard(slug, token),
            )
        else:
            extraction.rules_edition = manifest.rules_edition
            payloads = _prepare_extraction(
                extraction,
                meta["ingestion"]["source_kind"],
                1000,
                0,
                None,
                prepared,
                prepared_chunks=manifest_chunks(extraction, manifest),
            )
            chunks = _embed_and_upsert(
                payloads,
                slug,
                replace=True,
                publication_guard=lambda: store.attempt_guard(slug, token),
            )
    except Exception:
        store.finish_attempt(
            slug,
            token,
            {
                "status": "failed",
                "failure_code": "manifest_index_failed",
                "error": "Reviewed manifest indexing failed. Inspect the draft and retry publication.",
            },
        )
        raise
    store.finish_attempt(
        slug,
        token,
        {
            "status": "ready",
            "chunks": chunks,
            "manifest": manifest.model_dump(),
            "error": None,
            "failure_code": None,
        },
        success=True,
    )
    return {"status": "ready", "manifest_sha256": manifest.reviewed_sha256, "chunks": chunks}
