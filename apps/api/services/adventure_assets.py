"""Inert retained-asset readers and session-safe projections. No generation or preparation dispatch."""

from __future__ import annotations

import base64
import contextvars
import hashlib
import io
import json
import os
import re
import warnings
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from edgar_core.assets import (
    IndependentVisualReviewArtifact,
    PlayerMapProjection,
)
from edgar_core.manifests import AdventureManifest
from edgar_core.story import StoryFact

from . import adventures_meta

ASSET_ROOT = adventures_meta.DATA_DIR / "adventure-assets"


_ASSET_ROOT_OVERRIDE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "adventure_asset_root_override", default=None
)


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class AssetServiceError(ValueError):
    """A local asset request cannot be safely fulfilled."""


class AssetArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str
    plan_asset_id: str
    job_key: str
    collection: str
    source_sha256: str
    extraction_sha256: str
    manifest_sha256: str
    prepared_sha256: str
    kind: str
    sha256: str
    media_type: str
    scene_ids: list[str] = Field(default_factory=list)
    passage_ids: list[str] = Field(default_factory=list)
    entity_id: str | None = None
    profile_digest: str | None = None
    profile_settings: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    visual_review: IndependentVisualReviewArtifact | None = None
    relative_file: str


class PlayerMapRead(PlayerMapProjection):
    journal_entry_ids: dict[str, str] = Field(default_factory=dict)
    known_place_entries: list[dict[str, Any]] = Field(default_factory=list)
    artwork: list[dict[str, Any]] = Field(default_factory=list)


class AssetCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = 1
    entries: dict[str, AssetArtifactRecord] = Field(default_factory=dict)


def _sanitize_raster(content: bytes, media_type: str) -> bytes:
    """Decode and re-encode raster artwork to PNG without source metadata."""
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise AssetServiceError("Raster image format is unsupported.")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - packaging error in production
        raise AssetServiceError("The local raster sanitizer is unavailable.") from exc
    if not content or len(content) > _MAX_ARTIFACT_BYTES:
        raise AssetServiceError("Raster image exceeds the retained artifact size limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                expected_format = {
                    "image/png": "PNG",
                    "image/jpeg": "JPEG",
                    "image/webp": "WEBP",
                }[media_type]
                if source.format != expected_format:
                    raise AssetServiceError("Raster bytes do not match their declared media type.")
                if source.width * source.height > 40_000_000:
                    raise AssetServiceError("Raster dimensions exceed the safe image limit.")
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                image = ImageOps.exif_transpose(source)
                if image.width * image.height > 40_000_000:
                    raise AssetServiceError("Raster dimensions exceed the safe image limit.")
                image.load()
                mode = "RGBA" if "A" in image.getbands() else "RGB"
                normalized = image.convert(mode)
                normalized.info.clear()
                output = io.BytesIO()
                normalized.save(output, format="PNG", optimize=False)
                data = output.getvalue()
    except AssetServiceError:
        raise
    except Exception as exc:
        raise AssetServiceError("Raster bytes failed image validation.") from exc
    if not data or len(data) > _MAX_ARTIFACT_BYTES:
        raise AssetServiceError("Sanitized raster exceeds the retained artifact size limit.")
    return data


def _check_binding(collection: str, source_sha256: str, manifest_sha256: str) -> None:
    if not _SLUG_RE.fullmatch(collection):
        raise AssetServiceError("Asset source binding is invalid.")
    if not _HASH_RE.fullmatch(source_sha256) or not _HASH_RE.fullmatch(manifest_sha256):
        raise AssetServiceError("Asset source version is invalid.")


def _version_dir(collection: str, source_sha256: str, manifest_sha256: str) -> Path:
    _check_binding(collection, source_sha256, manifest_sha256)
    return (_ASSET_ROOT_OVERRIDE.get() or ASSET_ROOT) / collection / source_sha256 / manifest_sha256


def _read_catalog(path: Path) -> AssetCatalog:
    if not path.is_file():
        return AssetCatalog()
    try:
        return AssetCatalog.model_validate_json(path.read_bytes())
    except Exception as exc:
        raise AssetServiceError(
            "Retained asset catalog is unreadable; recover it before retrying."
        ) from exc


def _catalog_path(collection: str, source_sha256: str, manifest_sha256: str) -> Path:
    return _version_dir(collection, source_sha256, manifest_sha256) / "catalog.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_artifact(record: AssetArtifactRecord) -> bytes:
    directory = _version_dir(record.collection, record.source_sha256, record.manifest_sha256)
    path = (directory / record.relative_file).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise AssetServiceError("Retained asset bytes are unavailable.")
    content = path.read_bytes()
    if _sha256(content) != record.sha256:
        raise AssetServiceError("Retained asset failed its integrity check.")
    if record.media_type in {"image/png", "image/jpeg", "image/webp"}:
        if (
            record.media_type != "image/png"
            or _sanitize_raster(content, record.media_type) != content
        ):
            raise AssetServiceError("Retained raster is not in sanitized metadata-free form.")
    return content


def _public_asset_id(session_id: int, artifact_id: str) -> str:
    raw = f"edgar-session-asset-v1:{session_id}:{artifact_id}".encode()
    return "asset_" + hashlib.sha256(raw).hexdigest()[:32]


def _visual_review_payload(review: IndependentVisualReviewArtifact) -> bytes:
    return json.dumps(
        review.model_dump(mode="json", exclude={"signature"}),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _visual_candidate_id(record: AssetArtifactRecord) -> str:
    """Opaque ID bound to the retained artifact, version, kind and complete scope."""
    identity = {
        "version": 1,
        "collection": record.collection,
        "source_sha256": record.source_sha256,
        "manifest_sha256": record.manifest_sha256,
        "prepared_sha256": record.prepared_sha256,
        "artifact_id": record.artifact_id,
        "artifact_sha256": record.sha256,
        "kind": record.kind,
        "scene_ids": sorted(record.scene_ids),
        "passage_ids": sorted(record.passage_ids),
        "entity_id": record.entity_id,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"vc_{digest[:48]}"


def _valid_visual_review(
    record: AssetArtifactRecord,
    review: IndependentVisualReviewArtifact | None = None,
) -> bool:
    candidate = review or record.visual_review
    if (
        candidate is None
        or record.kind not in {"source_map", "generated_illustration", "original_creature_art"}
        or candidate.decision != "approved"
        or candidate.candidate_id != _visual_candidate_id(record)
        or candidate.artifact_sha256 != record.sha256
        or candidate.manifest_sha256 != record.manifest_sha256
        or candidate.prepared_sha256 != record.prepared_sha256
        or set(candidate.scene_ids) != set(record.scene_ids)
        or set(candidate.passage_ids) != set(record.passage_ids)
        or candidate.entity_id != record.entity_id
        or not (record.scene_ids or record.passage_ids or record.entity_id)
    ):
        return False
    encoded_key = os.environ.get("EDGAR_VISUAL_REVIEW_PUBLIC_KEY")
    if not encoded_key:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded_key, validate=True))
        signature = base64.b64decode(candidate.signature, validate=True)
        key.verify(signature, _visual_review_payload(candidate))
        return True
    except Exception:
        return False


def authorized_player_assets(
    *,
    session_id: int,
    collection: str,
    manifest: AdventureManifest,
    facts: list[StoryFact],
) -> list[dict[str, Any]]:
    """Return only asset references whose scene, passage or encounter was committed here."""
    if not manifest.reviewed_sha256:
        return []
    catalog = _read_catalog(
        _catalog_path(collection, manifest.source_sha256, manifest.reviewed_sha256)
    )
    encountered = {
        fact.target.canonical_id
        for fact in facts
        if fact.kind == "encounter"
        and fact.target.kind == "entity"
        and fact.target.collection == collection
        and fact.target.source_sha256 == manifest.source_sha256
        and fact.target.manifest_sha256 == manifest.reviewed_sha256
    }
    known_passages = {
        source_ref.get("passage_id")
        for fact in facts
        for source_ref in fact.source_refs
        if source_ref.get("audience") == "player"
        and source_ref.get("collection") == collection
        and source_ref.get("source_sha256") == manifest.source_sha256
        and source_ref.get("manifest_sha256") == manifest.reviewed_sha256
    }
    known_places = {
        fact.target.canonical_id
        for fact in facts
        if fact.target.kind == "scene"
        and fact.kind in {"learn_place", "visit", "move"}
        and fact.target.collection == collection
        and fact.target.source_sha256 == manifest.source_sha256
        and fact.target.manifest_sha256 == manifest.reviewed_sha256
    }
    approved: list[dict[str, Any]] = []
    for record in catalog.entries.values():
        if record.kind not in {
            "handout",
            "source_map",
            "generated_illustration",
            "original_creature_art",
        }:
            # Schematic maps are private DM projections. Players only receive the
            # independently rendered, session-filtered /map.svg projection.
            continue
        if not (record.scene_ids or record.passage_ids or record.entity_id):
            continue
        if record.entity_id and record.entity_id not in encountered:
            continue
        if record.scene_ids and not set(record.scene_ids) <= known_places:
            continue
        if record.passage_ids and not set(record.passage_ids) <= known_passages:
            continue
        if record.kind == "handout":
            player_passages = {
                passage.passage_id for passage in manifest.passages if passage.audience == "player"
            }
            if not record.passage_ids or not set(record.passage_ids) <= player_passages:
                continue
        elif not _valid_visual_review(record):
            continue
        try:
            _read_artifact(record)
        except AssetServiceError:
            continue
        approved.append(
            {
                "asset_id": _public_asset_id(session_id, record.artifact_id),
                "kind": record.kind,
                "url": f"/api/sessions/{session_id}/assets/{_public_asset_id(session_id, record.artifact_id)}",
                "media_type": record.media_type,
                "sha256": record.sha256,
            }
        )
    return approved


def get_authorized_player_asset_bytes(
    *,
    session_id: int,
    collection: str,
    manifest: AdventureManifest,
    facts: list[StoryFact],
    asset_id: str,
) -> tuple[AssetArtifactRecord, bytes] | None:
    """Resolve an opaque session alias only after rechecking the same committed-fact gate."""
    if not any(
        item["asset_id"] == asset_id
        for item in authorized_player_assets(
            session_id=session_id, collection=collection, manifest=manifest, facts=facts
        )
    ):
        return None
    if not manifest.reviewed_sha256:
        return None
    catalog = _read_catalog(
        _catalog_path(collection, manifest.source_sha256, manifest.reviewed_sha256)
    )
    for record in catalog.entries.values():
        if _public_asset_id(session_id, record.artifact_id) != asset_id:
            continue
        return record, _read_artifact(record)
    return None
