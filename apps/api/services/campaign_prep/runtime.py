"""Application boundary for the sole active preparation graph and inert archives."""

from __future__ import annotations

import asyncio
import fcntl
from contextlib import contextmanager
from pathlib import Path

import structlog

from edgar_core.manifests import load_published_manifest, manifest_digest
from edgar_core.preparation_status import PreparationJobStatus, PublicPreparationStatus

from .. import adventures_meta as meta_store
from .ai_generation import AI_WORKFLOW
from .source import file_hash
from .store import VERSION, Cancelled, NeedsAttention, Store, digest

log = structlog.get_logger(__name__)
_TASKS: set[asyncio.Task] = set()
_WORKERS = asyncio.Semaphore(2)


async def capture_setup(setup=None):
    from ...schemas.preparation_setup import PreparationSetup

    return {"request": (setup or PreparationSetup()).model_dump()}


def preparation_setup_needed(meta):
    return meta.get("preparation_workflow") == VERSION and meta.get("rules_edition") not in (
        "2014",
        "2024",
    )


def failed_source_preview_dir(slug, meta=None):
    # Campaign OCR evidence is private; the old public native-extraction preview is inert.
    return None


def discard_unbound_source(slug, meta):
    if meta.get("preparation_workflow") != VERSION:
        raise ValueError(
            "Archived preparation cannot be resumed or rewritten. Create a fresh adventure."
        )
    store = Store(slug)
    with worker_lock(store), store.db() as db:
        if (
            db.execute("SELECT 1 FROM task").fetchone()
            or db.execute("SELECT 1 FROM artifact").fetchone()
        ):
            raise ValueError(
                "This preparation has retained work. Create a fresh adventure to change its setup."
            )
        db.execute("DELETE FROM run")


@contextmanager
def worker_lock(store):
    with (store.directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Preparation is already running.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def frozen_request(slug, meta, pdf_path=None):
    setup = (meta.get("preparation_setup") or {}).get("request") or {}
    generation = meta.get("generation") or {}
    mode = "ai" if generation.get("kind") == "ai" else "pdf"
    return {
        "source_kind": mode,
        "rules_edition": meta.get("rules_edition", "2014"),
        "preferences": setup.get("preferences", ""),
        "cost_target_microusd": setup.get("cost_target_microusd", 5_000_000),
        "generation": {key: generation.get(key) for key in ("title_hint", "theme", "size")}
        if mode == "ai"
        else None,
        "pdf_path": str(pdf_path or meta_store.source_pdf_path(slug, meta))
        if mode == "pdf"
        else None,
        **({"ai_workflow": AI_WORKFLOW} if mode == "ai" else {}),
    }


async def prepare_adventure(slug, *, pdf_path=None, attempt_token=None, model_profiles=None):
    meta = meta_store.load_meta(slug)
    if meta.get("preparation_workflow") != VERSION:
        raise ValueError("Archived preparation is held; no model dispatch is permitted.")
    store = Store(slug)
    # On recovery the original request remains the identity. No configuration rebinding.
    if not store.exists():
        store.initialize(frozen_request(slug, meta, pdf_path))
    async with _WORKERS:
        with worker_lock(store):
            if store.state().get("status") == "ready":
                return
            from .graph import Workflow

            try:
                store.check()
                if store.state()["request"]["rules_edition"] not in ("2014", "2024"):
                    raise NeedsAttention(
                        "Select a supported explicit rules edition before source acquisition."
                    )
                store.update(status="running", error=None)
                await Workflow(store).run()
                log.info(
                    "campaign_preparation_ready",
                    slug=slug,
                    **{k: v for k, v in store.usage().items() if k not in ("calls", "pricing")},
                )
            except Cancelled:
                store.update(status="cancelled", error="Cancelled by user")
                _project_meta(slug, "cancelled")
                _track(_recover_cancellation(store), "cancel-campaign-v2-" + slug)
            except asyncio.CancelledError:
                # Server shutdown retains the exact running provider receipt for recovery.
                raise
            except Exception as exc:
                store.update(status="blocked", error=str(exc), error_type=type(exc).__name__)
                _project_meta(slug, "blocked")
                log.warning(
                    "campaign_preparation_held",
                    slug=slug,
                    node=store.state().get("node"),
                    error_type=type(exc).__name__,
                )


def _project_meta(slug, status):
    with meta_store.lifecycle_lock(slug):
        meta = meta_store.load_meta(slug)
        meta.update(
            status="failed",
            error="Preparation is saved and needs attention."
            if status == "blocked"
            else "Preparation cancelled.",
        )
        meta["job"].update(
            state="failed" if status == "blocked" else "cancelled",
            stage="Preparation needs attention" if status == "blocked" else "Cancelled",
            checkpoint_available=True,
            retryable=status == "blocked",
        )
        meta_store.save_meta(slug, meta)


def retry_preparation(slug):
    meta = meta_store.load_meta(slug)
    if meta.get("preparation_workflow") != VERSION:
        raise ValueError("Archived preparation is held. Create a fresh adventure.")
    store = Store(slug)
    if store.state().get("status") == "ready" or store.state().get("cancel_requested"):
        raise ValueError("A published or cancelled preparation cannot be resubmitted.")
    with worker_lock(store):
        # A new round grants sequential AI steps fresh revision attempts.
        store.update(
            status="queued", error=None, retry_round=store.state().get("retry_round", 0) + 1
        )
    token = meta_store.begin_attempt(
        slug,
        job_attempt=True,
        job_kind="generation"
        if (meta.get("generation") or {}).get("kind") == "ai"
        else "ingestion",
    )
    return store, token


def cancel_preparation(slug):
    meta = meta_store.load_meta(slug)
    if meta.get("preparation_workflow") != VERSION:
        raise ValueError("Archived records are held and immutable.")
    store = Store(slug)
    with meta_store.lifecycle_lock(slug):
        if (
            store.state().get("status") == "ready"
            or meta_store.load_meta(slug).get("status") == "ready"
        ):
            raise ValueError("This adventure is already published.")
        store.update(cancel_requested=True)
    _project_meta(slug, "cancelled")


def private_preparation_ready(slug, meta=None):
    meta = meta_store.load_meta(slug) if meta is None else meta
    if meta.get("preparation_workflow") != VERSION:
        from .archive import ready

        return ready(slug, meta)
    if meta.get("status") != "ready":
        return False
    try:
        store = Store(slug)
        compiled, publication = store.get("compiled"), store.get("publication")
        from .playability import require_compatibility_review

        require_compatibility_review(store, compiled)
        if (
            store.state().get("status") != "ready"
            or not publication
            or digest(compiled) != publication["compiled_sha256"]
            or meta.get("preparation_publication_receipt") != publication
            or digest(store.get("review-coverage")) != publication["review_sha256"]
            or digest(store.get("findings")) != publication["findings_sha256"]
            or (
                "compatibility_review_sha256" in publication
                and digest(store.get("compatibility-review"))
                != publication["compatibility_review_sha256"]
            )
            or any(f["value"]["severity"] == "material" for f in store.get("findings") or [])
            or file_hash(store.directory / "dm-guide.md") != publication["guide_sha256"]
            or file_hash(store.directory / "playable.json") != publication["playable_sha256"]
        ):
            return False
        from .graph import Workflow

        workflow = Workflow(store, semantic=object())
        registry, coverage = workflow.registry(), store.get("review-coverage")
        if any(coverage.get(key) != value for key, value in workflow.fingerprints(registry).items()):
            return False
        manifest = load_published_manifest(meta)
        if manifest_digest(manifest) != publication["manifest_sha256"]:
            return False
        from db.vector.versions import active_index_version

        if active_index_version(slug) != publication["index_version"]:
            return False
        source = store.get("source")
        if source["kind"] == "pdf" and file_hash(Path(source["pdf_path"])) != source["sha256"]:
            return False
        return True
    except Exception:
        return False


def safe_preparation_status(meta, slug):
    if meta.get("private_preparation") is not True:
        return None
    job_id = (meta.get("job") or {}).get("job_id", "")
    if meta.get("preparation_workflow") != VERSION:
        ready = private_preparation_ready(slug, meta)
        return PublicPreparationStatus(
            run_id=meta.get("preparation_run_id", "prep-" + job_id),
            status=PreparationJobStatus.READY if ready else PreparationJobStatus.BLOCKED,
            stage="Ready to play" if ready else "Archived preparation held",
            retryable=False,
            action="none" if ready else "restart",
        )
    store = Store(slug)
    state = store.state()
    status = state.get("status", "queued")
    if status == "ready" and not private_preparation_ready(slug, meta):
        status = "invalid_publication"
    mapping = {
        "source": "Reading campaign pages",
        "spine": "Establishing the campaign",
        "ai_premise": "Shaping the premise",
        "ai_cast": "Creating the cast and secrets",
        "ai_outline": "Outlining the chapters",
        "ai_chapter": "Writing the chapters",
        "ai_map": "Drawing the map",
        "ai_compose": "Joining the chapters",
        "bundles": "Preparing chapter bundles",
        "continuity": "Joining shared identities",
        "assemble": "Assembling playable material",
        "review": "Independent final review",
        "repair": "Repairing affected records",
        "publish": "Publishing your adventure",
    }
    mapped = {
        "running": PreparationJobStatus.PREPARING,
        "ready": PreparationJobStatus.READY,
        "blocked": PreparationJobStatus.BLOCKED,
        "invalid_publication": PreparationJobStatus.BLOCKED,
        "queued": PreparationJobStatus.QUEUED,
        "cancelled": PreparationJobStatus.CANCELLED,
    }.get(status, PreparationJobStatus.BLOCKED)
    return PublicPreparationStatus(
        run_id="graph-" + job_id,
        status=mapped,
        stage="Ready to play"
        if status == "ready"
        else "Publication needs verification"
        if status == "invalid_publication"
        else "Preparation needs attention"
        if status == "blocked"
        else "Cancelled"
        if status == "cancelled"
        else mapping.get(state.get("node"), "Queued"),
        retryable=status == "blocked",
        action="none"
        if status in ("ready", "cancelled", "invalid_publication")
        else "retry"
        if status == "blocked"
        else "wait",
    )


def published_world_map(slug):
    """The prepared world map of a Ready AI adventure, or None."""
    try:
        # Never create a store as a side effect of reading a map.
        if not (meta_store.adventure_path(slug, "preparation-v2") / "graph.sqlite3").exists():
            return None
        store = Store(slug)
        if store.state().get("status") != "ready":
            return None
        return store.get("world-map")
    except Exception:
        return None


def published_scene_places(slug, manifest):
    """Private geographic identities from the exact published semantic assembly.

    These names are recognition candidates, never disclosures. Callers must earn
    their wording from player text before displaying it. Reading this retained
    registry does not recompile or alter a published campaign.
    """
    try:
        if not (meta_store.adventure_path(slug, "preparation-v2") / "graph.sqlite3").exists():
            return {}
        store = Store(slug)
        compiled, publication = store.get("compiled"), store.get("publication")
        if (
            store.state().get("status") != "ready"
            or not publication
            or digest(compiled) != publication.get("compiled_sha256")
            or compiled["manifest"]["source_sha256"] != manifest.source_sha256
            or compiled["manifest_sha256"] != manifest.reviewed_sha256
        ):
            return {}
        registry = compiled["registry"]
        places = {}
        for identity, record in registry.items():
            if record["kind"] != "event":
                continue
            location_id = record["value"]["location_id"]
            location = registry.get(location_id, {})
            if location.get("kind") != "entity" or location["value"].get("kind") != "place":
                continue
            value = location["value"]
            places[identity] = {
                "place_id": location_id,
                "names": [value["name"], *value.get("aliases", [])],
                "source_sha256": manifest.source_sha256,
                "manifest_sha256": manifest.reviewed_sha256,
            }
        return places
    except (KeyError, TypeError, ValueError, OSError):
        return {}


def preparation_job_spend(slug):
    from ...limits import JobSpend

    if meta_store.load_meta(slug).get("preparation_workflow") != VERSION:
        return None
    store = Store(slug)
    return JobSpend(
        spent_microusd=store.usage()["cost_microusd"],
        terminal=store.state().get("status") in ("ready", "blocked", "cancelled"),
    )


async def recover_private_preparations():
    """Dispatch only explicitly marked v2 jobs. Never scan or mutate old checkpoints."""
    for slug in sorted(meta_store.metadata_slugs()):
        meta = meta_store.load_meta(slug)
        if meta.get("preparation_workflow") != VERSION:
            continue
        store = Store(slug)
        if store.state().get("cancel_requested"):
            _track(_recover_cancellation(store), "cancel-campaign-v2-" + slug)
        elif store.state().get("status", "queued") in ("queued", "running"):
            _track(prepare_adventure(slug), "campaign-v2-" + slug)


def _track(coroutine, name):
    task = asyncio.create_task(coroutine, name=name)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _recover_cancellation(store):
    from .provider import SemanticClient

    semantic = SemanticClient(store)
    try:
        delay = 2
        while await semantic.reconcile_cancellation():
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        store.update(status="cancelled", cancellation_reconciled=True)
    except NeedsAttention:
        store.update(cancellation_reconciled=False)
        log.warning("campaign_cancellation_needs_reconciliation", slug=store.slug)


async def shutdown():
    for task in tuple(_TASKS):
        task.cancel()
    if _TASKS:
        await asyncio.gather(*_TASKS, return_exceptions=True)
