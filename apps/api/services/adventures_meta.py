"""Adventure metadata store (JSON beside the /data PDF store).

Shared by the adventures router and the background ingest/generation workers so neither has to
import the other. Metadata persists across DB resets and tracks the ingestion lifecycle
(processing -> ready | failed) plus title/description/chunk count.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

import structlog

log = structlog.get_logger()

DATA_DIR = Path("/data")
ADVENTURES_DIR = DATA_DIR / "adventures"
PDF_DIR = DATA_DIR / "pdfs"


def meta_path(slug: str) -> Path:
    return ADVENTURES_DIR / f"{slug}.json"


def load_meta(slug: str) -> dict:
    p = meta_path(slug)
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.warning("adventure_metadata_parse_error", slug=slug, path=str(p))
    return {}


def _write_meta(slug: str, data: dict, *, create: bool = False) -> None:
    """Publish complete JSON atomically; `create` must not replace an existing reservation."""
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=ADVENTURES_DIR, suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file, indent=2)
        if create:
            # Linking a completed temporary file claims the name without overwriting it.
            # Competing API processes receive FileExistsError and choose another slug.
            os.link(temporary, meta_path(slug))
        else:
            temporary.replace(meta_path(slug))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_meta(slug: str, data: dict) -> None:
    """Replace metadata without exposing a partially written JSON document to readers."""
    _write_meta(slug, data)


def reserve_meta(slug: str, data: dict) -> bool:
    """Create a complete reservation only if the slug is still free."""
    try:
        _write_meta(slug, data, create=True)
    except FileExistsError:
        return False
    return True


def metadata_slugs() -> set[str]:
    if not ADVENTURES_DIR.is_dir():
        return set()
    return {p.stem for p in ADVENTURES_DIR.glob("*.json")}


@contextmanager
def lifecycle_lock(slug: str):
    ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    with (ADVENTURES_DIR / f"{slug}.lifecycle.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def begin_attempt(slug: str) -> str:
    """Reserve a worker before dispatch; retain the complete last ready receipt."""
    with lifecycle_lock(slug):
        meta = load_meta(slug)
        if meta.get("status") == "ready":
            meta["ready_receipt"] = {
                k: v
                for k, v in meta.items()
                if k not in ("attempt", "ready_receipt", "index_receipts", "last_attempt")
            }
        token = uuid.uuid4().hex
        meta.pop("last_attempt", None)
        meta.update(status="processing", error=None, failure_code=None, attempt={"id": token})
        save_meta(slug, meta)
        return token


@contextmanager
def attempt_guard(slug: str, token: str):
    from db.vector.versions import SupersededIngestion

    with lifecycle_lock(slug):
        meta = load_meta(slug)
        if (meta.get("attempt") or {}).get("id") != token:
            raise SupersededIngestion("A newer accepted adventure attempt owns this source.")
        yield meta


def attempt_dir(slug: str, token: str) -> Path:
    return ADVENTURES_DIR / slug / ".attempts" / token


def prepare_attempt(slug: str, token: str, report: dict, updates: dict | None = None) -> None:
    """Persist the proposed receipt before activation, so a restart can reconcile it."""
    from ingestion.readiness import report_summary

    with attempt_guard(slug, token) as meta:
        summary = report_summary(report)
        meta["attempt"]["ingestion"] = summary
        if not meta.get("ready_receipt"):
            meta["ingestion"] = summary
        version = report.get("index_version")
        if version and report["quality_passed"]:
            receipt = {
                k: v
                for k, v in meta.items()
                if k not in ("attempt", "ready_receipt", "index_receipts", "last_attempt")
            }
            receipt.update(
                updates or {},
                status="ready",
                chunks=report["chunk_count"],
                ingestion=summary,
                error=None,
                failure_code=None,
            )
            if receipt.get("manifest"):
                receipt.pop("manifest_draft", None)
            receipt["artifact_attempt"] = token
            meta.setdefault("index_receipts", {})[version] = receipt
            previous_version = (
                meta.get("ready_receipt", {}).get("ingestion") or meta.get("ingestion") or {}
            ).get("index_version")
            meta["index_receipts"] = {
                k: v for k, v in meta["index_receipts"].items() if k in (version, previous_version)
            }
        save_meta(slug, meta)


def finish_attempt(slug: str, token: str, updates: dict, *, success: bool = False) -> bool:
    """Only the current worker may publish artifacts/metadata or record a failure."""
    from db.vector.versions import SupersededIngestion

    try:
        with attempt_guard(slug, token) as meta:
            previous = meta.get("ready_receipt")
            report = meta["attempt"].get("ingestion")
            if success or not previous:
                staged = attempt_dir(slug, token)
                destination = ADVENTURES_DIR / slug
                destination.mkdir(parents=True, exist_ok=True)
                for path in staged.glob(f"{slug}.*"):
                    # Retain the immutable attempt copy; the canonical manuscript paths
                    # remain backward compatible for local review and retry.
                    with tempfile.NamedTemporaryFile(dir=destination, delete=False) as out:
                        temporary = Path(out.name)
                    try:
                        shutil.copyfile(path, temporary)
                        temporary.replace(destination / path.name)
                    finally:
                        temporary.unlink(missing_ok=True)
                meta.update(updates)
                if report:
                    meta["ingestion"] = report
                if success:
                    meta.pop("ready_receipt", None)
                    if updates.get("manifest"):
                        meta.pop("manifest_draft", None)
            else:
                meta.update(previous)
                meta["last_attempt"] = updates
            meta.pop("attempt", None)
            save_meta(slug, meta)
            return True
    except SupersededIngestion:
        return False


STALE_PROCESSING_ERROR = (
    "Processing was interrupted by a server restart. Use retry to process it again."
)


def fail_stale_processing() -> list[str]:
    """Mark every adventure still in "processing" as failed; return the affected slugs.

    Call once at startup: ingestion/generation run as in-process background tasks, so anything
    still "processing" when the process starts belongs to a run that no longer exists.
    """
    stale: list[str] = []
    for slug in sorted(metadata_slugs()):
        meta = load_meta(slug)
        if meta.get("status") == "processing":
            previous = meta.get("ready_receipt")
            if previous:
                meta.update(previous)
                meta["last_attempt"] = {"status": "failed", "error": STALE_PROCESSING_ERROR}
            else:
                meta.update({"status": "failed", "error": STALE_PROCESSING_ERROR})
            meta.pop("attempt", None)
            save_meta(slug, meta)
            stale.append(slug)
    return stale
