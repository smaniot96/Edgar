"""Hash-checked local access to a published DM guide or retained archived manuscript.

The private manuscript is intentionally not added to player source projections or HTTP links.
Authors can export the active retained version with ``python -m apps.api.services.manuscript``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

from . import adventures_meta as store
from .campaign_sources import active_metadata


class ManuscriptUnavailable(ValueError):
    """The source has no retained canonical original, including honest legacy records."""


@dataclass(frozen=True)
class CanonicalManuscript:
    path: Path
    version: str
    sha256: str
    source_sha256: str
    extraction_sha256: str
    index_version: str | None


def _validate_binding(slug: str) -> None:
    # Match the existing adventure source binding contract before using the slug in paths.
    from .campaign_sources import validate_binding

    try:
        validate_binding(slug)
    except HTTPException as exc:
        raise ManuscriptUnavailable("The adventure source binding is invalid.") from exc


def resolve_canonical_manuscript(slug: str) -> CanonicalManuscript:
    """Resolve only the source artifact referenced by the active metadata/index receipt.

    Never reconstruct prose from overlapping vectors or fall back to a legacy preview. The
    independent ``attempt_id`` remains valid when a later manifest-only attempt changes the
    extraction artifact's active attempt token.
    """
    _validate_binding(slug)
    # Resolve the last published receipt while processing. This keeps an incomplete candidate
    # hidden while preserving local access to the previous original if vectors are unavailable.
    raw_meta = store.load_meta(slug)
    if raw_meta.get("preparation_workflow") == "campaign-graph-2.0":
        return _resolve_prepared_guide(slug, raw_meta)
    meta = active_metadata(slug, raw_meta)
    if meta.get("status") != "ready" and isinstance(raw_meta.get("ready_receipt"), dict):
        meta = raw_meta["ready_receipt"]
    record = meta.get("manuscript")
    if not isinstance(record, dict):
        raise ManuscriptUnavailable(
            "This source predates retained original manuscripts. No canonical original is available."
        )
    attempt_id = record.get("attempt_id")
    filename = record.get("filename")
    version = record.get("version")
    if (
        not isinstance(attempt_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
        or not isinstance(filename, str)
        or Path(filename).name != filename
        or filename != f"{slug}.manuscript.md"
        or not isinstance(version, str)
        or not re.fullmatch(r"[0-9a-f]{64}", version)
    ):
        raise ManuscriptUnavailable("The retained manuscript reference is incomplete.")
    path = store.attempt_dir(slug, attempt_id) / filename
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ManuscriptUnavailable(
            "The retained canonical manuscript file is unavailable."
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    if digest != version or digest != record.get("sha256"):
        raise ManuscriptUnavailable("The retained canonical manuscript failed its integrity check.")
    source_sha256 = record.get("source_sha256")
    extraction_sha256 = record.get("extraction_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ManuscriptUnavailable("The retained manuscript has no complete source provenance.")
    if not isinstance(extraction_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", extraction_sha256
    ):
        raise ManuscriptUnavailable("The retained manuscript has no complete source provenance.")
    ingestion = meta.get("ingestion") or {}
    if any(
        ingestion.get(key) and ingestion[key] != record[key]
        for key in ("source_sha256", "extraction_sha256")
    ):
        raise ManuscriptUnavailable("The retained manuscript does not match this source version.")
    return CanonicalManuscript(
        path=path,
        version=version,
        sha256=digest,
        source_sha256=source_sha256,
        extraction_sha256=extraction_sha256,
        index_version=ingestion.get("index_version", record.get("index_version"))
        if isinstance(ingestion.get("index_version", record.get("index_version")), str)
        else None,
    )


def _resolve_prepared_guide(slug: str, meta: dict) -> CanonicalManuscript:
    """Export the guide pinned by the completed publication, even while Qdrant is offline.

    The guide is a deterministic assembly of accepted semantic records. Incomplete
    candidates are private working artifacts and never qualify as a published original.
    """
    from edgar_core.manifests import load_published_manifest, manifest_digest

    from .campaign_prep.store import Store, digest

    try:
        graph_store = Store(slug)
        compiled = graph_store.get("compiled")
        publication = graph_store.get("publication")
        if (
            meta.get("status") != "ready"
            or graph_store.state().get("status") != "ready"
            or not compiled
            or not publication
            or digest(compiled) != publication["compiled_sha256"]
            or meta.get("preparation_publication_receipt") != publication
            or manifest_digest(load_published_manifest(meta)) != publication["manifest_sha256"]
        ):
            raise ManuscriptUnavailable("The prepared DM guide has no current publication receipt.")
        path = graph_store.directory / "dm-guide.md"
        content = path.read_bytes()
        expected = hashlib.sha256(compiled["guide"].encode("utf-8")).hexdigest()
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != expected:
            raise ManuscriptUnavailable("The prepared DM guide failed its integrity check.")
        return CanonicalManuscript(
            path=path,
            version=checksum,
            sha256=checksum,
            source_sha256=compiled["manifest"]["source_sha256"],
            extraction_sha256=compiled["manifest"]["extraction_sha256"],
            index_version=publication["index_version"],
        )
    except ManuscriptUnavailable:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ManuscriptUnavailable("The prepared DM guide is unavailable or changed.") from exc


def export_canonical_manuscript(slug: str, destination: str | Path) -> CanonicalManuscript:
    """Copy the exact UTF-8 bytes of the active original to a local author-chosen path."""
    manuscript = resolve_canonical_manuscript(slug)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            with manuscript.path.open("rb") as source:
                digest = hashlib.sha256()
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    out.write(block)
                    digest.update(block)
            out.flush()
            os.fsync(out.fileno())
            if digest.hexdigest() != manuscript.sha256:
                raise ManuscriptUnavailable(
                    "The retained canonical manuscript changed during export; no file was published."
                )
        temporary.replace(target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return manuscript


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an active retained Edgar manuscript.")
    parser.add_argument("slug", help="Adventure slug")
    parser.add_argument("destination", type=Path, help="Local output file")
    args = parser.parse_args()
    manuscript = export_canonical_manuscript(args.slug, args.destination)
    print(f"Exported {args.slug} manuscript version {manuscript.version} to {args.destination}")


if __name__ == "__main__":
    main()
