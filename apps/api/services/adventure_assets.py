"""Versioned local adventure assets and session-safe map projections.

Opening a map only reads the committed session ledger and already retained assets. Explicit
preparation calls use deterministic SVG/text rendering first; bitmap generation is isolated
behind an image-only provider seam with a durable call reservation and worker fencing.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
import time
import uuid
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.artifacts import text_digest
from edgar_core.assets import (
    AssetCoverage,
    AssetCoverageReceipt,
    AssetPlan,
    AssetRequirement,
    AssetScopeCoverage,
    IndependentVisualReviewArtifact,
    IndependentVisualReviewDecision,
    PlayerMapPlace,
    PlayerMapProjection,
    PlayerMapRoute,
    PrivateVisualCandidate,
    plan_assets as _plan_assets,
    render_player_map_svg,
)
from edgar_core.manifests import AdventureManifest, require_published
from edgar_core.model_profiles import (
    ImageProfile,
    ModelProfileSnapshot,
    current_model_profiles,
    effective_image_profile,
)
from edgar_core.prepared import PreparedCampaign, prepared_digest
from edgar_core.story import StoryFact

from . import adventures_meta

ASSET_ROOT = adventures_meta.DATA_DIR / "adventure-assets"
_ASSET_ROOT_OVERRIDE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "adventure_asset_root_override", default=None
)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")
_ASSET_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class AssetServiceError(ValueError):
    """A local asset request cannot be safely fulfilled."""


class AssetJobBusy(AssetServiceError):
    """An identical image job already has a live worker."""


class ImageBudgetExceeded(AssetServiceError):
    """No reserved image call remains in the campaign's frozen budget."""


class ImageProviderUnavailable(AssetServiceError):
    """The configured image provider cannot return a local retained artifact."""


class GeneratedImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    data: bytes
    media_type: str
    # Set only when the provider returns an authoritative billing amount.
    cost_microusd: int | None = Field(default=None, ge=0)


class ImageGenerationProvider(Protocol):
    async def generate(self, prompt: str, profile: ImageProfile) -> GeneratedImage: ...


class OpenAIImageProvider:
    """Production adapter for the configured image model; SDK retries are disabled."""

    async def generate(self, prompt: str, profile: ImageProfile) -> GeneratedImage:
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - packaging error in production
            raise ImageProviderUnavailable("The image provider client is not installed.") from exc
        try:
            async with AsyncOpenAI(timeout=profile.timeout_seconds, max_retries=0) as client:
                result = await client.images.generate(
                    model=profile.model,
                    prompt=prompt,
                    size=profile.size,
                    quality=profile.quality,
                    output_format=profile.output_format,
                    n=1,
                )
        except Exception as exc:
            # Don't persist provider details, prompt contents or credentials in public errors.
            raise ImageProviderUnavailable("The configured image request failed.") from exc
        payload = result.data[0] if result.data else None
        if payload is None or not payload.b64_json:
            raise ImageProviderUnavailable("The configured image provider returned no image bytes.")
        try:
            data = base64.b64decode(payload.b64_json, validate=True)
        except (ValueError, TypeError) as exc:
            raise ImageProviderUnavailable("The image provider returned invalid image data.") from exc
        if not data or len(data) > _MAX_ARTIFACT_BYTES:
            raise ImageProviderUnavailable("The image provider returned an unsupported image size.")
        return GeneratedImage(data=data, media_type=f"image/{profile.output_format}")


class AssetImageBudget(BaseModel):
    """Frozen call and spend caps; price revision and settings come from commissioning."""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_image_calls: int = Field(ge=0, le=500)
    max_image_cost_microusd: int = Field(default=0, ge=0)
    max_image_call_cost_microusd: int = Field(default=0, ge=0)
    image_price_revision: str | None = Field(default=None, max_length=128)
    image_price_model: str | None = Field(default=None, max_length=128)
    image_price_size: str | None = Field(default=None, max_length=32)
    image_price_quality: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _validate_cost_caps(self) -> AssetImageBudget:
        if self.max_image_calls and self.max_image_call_cost_microusd <= 0:
            raise ValueError("A positive frozen per-call image cost cap is required.")
        if self.max_image_calls and self.max_image_cost_microusd <= 0:
            raise ValueError("A positive frozen total image cost cap is required.")
        if self.max_image_call_cost_microusd > self.max_image_cost_microusd:
            raise ValueError("The per-call image cost cap exceeds the total image cost cap.")
        if self.max_image_calls and not all(
            (self.image_price_revision, self.image_price_model, self.image_price_size, self.image_price_quality)
        ):
            raise ValueError("An exact frozen image price record is required for paid image calls.")
        return self


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


class TrustedImagePriceRecord(BaseModel):
    """Server-owned documented price for one exact image configuration."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=128)
    size: str = Field(min_length=1, max_length=32)
    quality: str = Field(min_length=1, max_length=32)
    unit_cost_microusd: int = Field(gt=0)


def _trusted_image_price_record() -> TrustedImagePriceRecord | None:
    raw = os.environ.get("EDGAR_TRUSTED_IMAGE_PRICE_RECORD")
    if not raw:
        return None
    try:
        return TrustedImagePriceRecord.model_validate_json(raw)
    except Exception:
        return None


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


def plan_assets(
    manifest: AdventureManifest | dict,
    required_maps: list[AssetRequirement | dict],
    required_handouts: list[AssetRequirement | dict],
) -> AssetPlan:
    """Public preparation adapter for P07; coverage is code-derived from canonical topology."""
    return _plan_assets(manifest, required_maps, required_handouts)


def _check_binding(collection: str, source_sha256: str, manifest_sha256: str) -> None:
    if not _SLUG_RE.fullmatch(collection):
        raise AssetServiceError("Asset source binding is invalid.")
    if not _HASH_RE.fullmatch(source_sha256) or not _HASH_RE.fullmatch(manifest_sha256):
        raise AssetServiceError("Asset source version is invalid.")


def _version_dir(collection: str, source_sha256: str, manifest_sha256: str) -> Path:
    _check_binding(collection, source_sha256, manifest_sha256)
    return (_ASSET_ROOT_OVERRIDE.get() or ASSET_ROOT) / collection / source_sha256 / manifest_sha256


def _entry_path(collection: str, source_sha256: str, manifest_sha256: str, asset_id: str) -> Path:
    if not _ASSET_RE.fullmatch(asset_id):
        raise AssetServiceError("Asset ID is invalid.")
    return _version_dir(collection, source_sha256, manifest_sha256) / f"{asset_id}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".asset-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_catalog(path: Path) -> AssetCatalog:
    if not path.is_file():
        return AssetCatalog()
    try:
        return AssetCatalog.model_validate_json(path.read_bytes())
    except Exception as exc:
        raise AssetServiceError("Retained asset catalog is unreadable; recover it before retrying.") from exc


def _catalog_path(collection: str, source_sha256: str, manifest_sha256: str) -> Path:
    return _version_dir(collection, source_sha256, manifest_sha256) / "catalog.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _job_key(
    *,
    collection: str,
    manifest: AdventureManifest,
    prepared: PreparedCampaign,
    item_id: str,
    prompt: str,
    profile: ImageProfile,
    profile_digest: str,
) -> str:
    payload = {
        "collection": collection,
        "source_sha256": manifest.source_sha256,
        "extraction_sha256": manifest.extraction_sha256,
        "manifest_sha256": manifest.reviewed_sha256,
        "prepared_sha256": prepared_digest(prepared),
        "asset_id": item_id,
        "prompt_sha256": text_digest(prompt),
        "profile_digest": profile_digest,
        "profile": profile.to_dict(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _budget_file(collection: str, source_sha256: str, manifest_sha256: str, prepared_sha256: str) -> Path:
    return _version_dir(collection, source_sha256, manifest_sha256) / f"budget-{prepared_sha256}.json"


def _reserve_image_call(
    collection: str,
    source_sha256: str,
    manifest_sha256: str,
    prepared_sha256: str,
    job_key: str,
    max_image_calls: int,
    max_image_cost_microusd: int,
    max_image_call_cost_microusd: int,
    image_price_revision: str,
    image_price_model: str,
    image_price_size: str,
    image_price_quality: str,
    timeout_seconds: float,
) -> str:
    version_dir = _version_dir(collection, source_sha256, manifest_sha256)
    version_dir.mkdir(parents=True, exist_ok=True)
    budget_path = _budget_file(collection, source_sha256, manifest_sha256, prepared_sha256)
    job_path = version_dir / "jobs" / f"{job_key}.json"
    lock_path = version_dir / "jobs.lock"
    attempt_id = uuid.uuid4().hex
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            job = json.loads(job_path.read_text()) if job_path.is_file() else {}
            if job.get("state") == "completed":
                raise AssetServiceError("Image job completed during reservation; load its retained result.")
            if job.get("state") == "running":
                if float(job.get("stale_after", 0)) > time.time():
                    raise AssetJobBusy("An identical image job is already running.")
                job["calls_failed"] = int(job.get("calls_failed", 0)) + 1
                job["cost_failed_microusd"] = int(job.get("cost_failed_microusd", 0)) + int(
                    job.get("active_call_cost_microusd", 0)
                )
            price_record = {
                "revision": image_price_revision,
                "model": image_price_model,
                "size": image_price_size,
                "quality": image_price_quality,
            }
            budget = json.loads(budget_path.read_text()) if budget_path.is_file() else {
                "limit_calls": max_image_calls,
                "limit_cost_microusd": max_image_cost_microusd,
                "max_image_call_cost_microusd": max_image_call_cost_microusd,
                "price_record": price_record,
                "reserved_calls": 0,
                "reserved_cost_microusd": 0,
            }
            if (
                budget.get("limit_calls") != max_image_calls
                or budget.get("limit_cost_microusd") != max_image_cost_microusd
                or budget.get("max_image_call_cost_microusd") != max_image_call_cost_microusd
                or budget.get("price_record") != price_record
            ):
                raise AssetServiceError("Image budget changed for a frozen preparation run.")
            if budget.get("reserved_calls", 0) >= max_image_calls:
                raise ImageBudgetExceeded("The frozen image generation budget is exhausted.")
            if (
                int(budget.get("reserved_cost_microusd", 0)) + max_image_call_cost_microusd
                > max_image_cost_microusd
            ):
                raise ImageBudgetExceeded("The frozen image spend budget is exhausted.")
            budget["reserved_calls"] = int(budget.get("reserved_calls", 0)) + 1
            budget["reserved_cost_microusd"] = (
                int(budget.get("reserved_cost_microusd", 0)) + max_image_call_cost_microusd
            )
            _atomic_write(budget_path, json.dumps(budget, sort_keys=True).encode())
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job.update(
                state="running",
                attempt_id=attempt_id,
                attempts=int(job.get("attempts", 0)) + 1,
                calls_reserved=int(job.get("calls_reserved", 0)) + 1,
                cost_reserved_microusd=int(job.get("cost_reserved_microusd", 0))
                + max_image_call_cost_microusd,
                active_call_cost_microusd=max_image_call_cost_microusd,
                prepared_sha256=prepared_sha256,
                stale_after=time.time() + timeout_seconds + 30,
            )
            _atomic_write(job_path, json.dumps(job, sort_keys=True).encode())
            return attempt_id
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _finish_job(
    *, collection: str, source_sha256: str, manifest_sha256: str, job_key: str,
    attempt_id: str, state: str, artifact: AssetArtifactRecord | None = None,
    actual_cost_microusd: int | None = None,
) -> bool:
    version_dir = _version_dir(collection, source_sha256, manifest_sha256)
    job_path = version_dir / "jobs" / f"{job_key}.json"
    lock_path = version_dir / "jobs.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if not job_path.is_file():
                return False
            job = json.loads(job_path.read_text())
            if job.get("attempt_id") != attempt_id:
                return False
            if artifact is not None:
                job["artifact_id"] = artifact.artifact_id
            job["state"] = state
            if state == "failed":
                job["calls_failed"] = int(job.get("calls_failed", 0)) + 1
                job["cost_failed_microusd"] = int(job.get("cost_failed_microusd", 0)) + int(
                    job.get("active_call_cost_microusd", 0)
                )
            elif state == "completed":
                call_cap = int(job.get("active_call_cost_microusd", 0))
                actual = call_cap if actual_cost_microusd is None else actual_cost_microusd
                if actual < 0 or actual > call_cap:
                    return False
                job["cost_succeeded_microusd"] = int(
                    job.get("cost_succeeded_microusd", 0)
                ) + actual
                job["cost_refunded_microusd"] = int(
                    job.get("cost_refunded_microusd", 0)
                ) + (call_cap - actual)
                budget_path = _budget_file(
                    collection, source_sha256, manifest_sha256, job["prepared_sha256"]
                )
            job.pop("attempt_id", None)
            job.pop("active_call_cost_microusd", None)
            _atomic_write(job_path, json.dumps(job, sort_keys=True).encode())
            if state == "completed":
                budget = json.loads(budget_path.read_text())
                budget["reserved_cost_microusd"] = int(
                    budget.get("reserved_cost_microusd", 0)
                ) - (call_cap - actual)
                _atomic_write(budget_path, json.dumps(budget, sort_keys=True).encode())
            return True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _record_artifact(
    *,
    collection: str,
    manifest: AdventureManifest,
    prepared: PreparedCampaign,
    item_id: str,
    kind: str,
    content: bytes,
    media_type: str,
    scene_ids: list[str],
    passage_ids: list[str],
    entity_id: str | None,
    profile: ImageProfile | None = None,
    profile_digest: str | None = None,
    provenance: dict[str, Any] | None = None,
    job_key: str = "deterministic",
    expected_attempt_id: str | None = None,
    actual_cost_microusd: int | None = None,
) -> AssetArtifactRecord:
    if len(content) > _MAX_ARTIFACT_BYTES:
        raise AssetServiceError("Asset exceeds the retained artifact size limit.")
    if media_type in {"image/png", "image/jpeg", "image/webp"}:
        content = _sanitize_raster(content, media_type)
        media_type = "image/png"
    manifest_hash = manifest.reviewed_sha256
    if not manifest_hash:
        raise AssetServiceError("A reviewed manifest is required to retain assets.")
    directory = _version_dir(collection, manifest.source_sha256, manifest_hash)
    directory.mkdir(parents=True, exist_ok=True)
    artifact_id = f"{item_id[:100]}-{job_key[:16]}" if job_key != "deterministic" else item_id
    data_path = directory / f"{artifact_id}.{_extension(media_type)}"
    record = AssetArtifactRecord(
        artifact_id=artifact_id,
        plan_asset_id=item_id,
        job_key=job_key,
        collection=collection,
        source_sha256=manifest.source_sha256,
        extraction_sha256=manifest.extraction_sha256,
        manifest_sha256=manifest_hash,
        prepared_sha256=prepared_digest(prepared),
        kind=kind,
        sha256=_sha256(content),
        media_type=media_type,
        scene_ids=scene_ids,
        passage_ids=passage_ids,
        entity_id=entity_id,
        profile_digest=profile_digest,
        profile_settings=profile.to_dict() if profile else {},
        provenance=provenance or {},
        relative_file=data_path.name,
    )
    catalog_path = _catalog_path(collection, manifest.source_sha256, manifest_hash)
    lock_path = directory / "catalog.lock"
    job_lock_path = directory / "jobs.lock"
    job_lock = job_lock_path.open("a+") if expected_attempt_id else None
    try:
        if job_lock is not None:
            fcntl.flock(job_lock.fileno(), fcntl.LOCK_EX)
            job_path = directory / "jobs" / f"{job_key}.json"
            current = json.loads(job_path.read_text()) if job_path.is_file() else {}
            if current.get("attempt_id") != expected_attempt_id:
                raise AssetServiceError("Image job result was superseded and has been fenced off.")
            call_cap = int(current.get("active_call_cost_microusd", 0))
            actual = call_cap if actual_cost_microusd is None else actual_cost_microusd
            if actual < 0 or actual > call_cap:
                raise AssetServiceError("Provider cost exceeded the frozen per-call image cap.")
        _atomic_write(data_path, content)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                catalog = _read_catalog(catalog_path)
                catalog.entries[record.artifact_id] = record
                _atomic_write(catalog_path, catalog.model_dump_json().encode())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        if job_lock is not None:
            job_path = directory / "jobs" / f"{job_key}.json"
            current = json.loads(job_path.read_text())
            current["artifact_id"] = record.artifact_id
            current["state"] = "completed"
            current["calls_succeeded"] = int(current.get("calls_succeeded", 0)) + 1
            current["cost_succeeded_microusd"] = int(
                current.get("cost_succeeded_microusd", 0)
            ) + actual
            current["cost_refunded_microusd"] = int(
                current.get("cost_refunded_microusd", 0)
            ) + call_cap - actual
            budget_path = _budget_file(
                collection, manifest.source_sha256, manifest_hash, current["prepared_sha256"]
            )
            current.pop("attempt_id", None)
            current.pop("active_call_cost_microusd", None)
            _atomic_write(job_path, json.dumps(current, sort_keys=True).encode())
            budget = json.loads(budget_path.read_text())
            budget["reserved_cost_microusd"] = int(
                budget.get("reserved_cost_microusd", 0)
            ) - (call_cap - actual)
            _atomic_write(budget_path, json.dumps(budget, sort_keys=True).encode())
    finally:
        if job_lock is not None:
            fcntl.flock(job_lock.fileno(), fcntl.LOCK_UN)
            job_lock.close()
    return record


def _extension(media_type: str) -> str:
    return {"image/svg+xml": "svg", "text/plain; charset=utf-8": "txt", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(media_type, "bin")


def _read_artifact(record: AssetArtifactRecord) -> bytes:
    directory = _version_dir(record.collection, record.source_sha256, record.manifest_sha256)
    path = (directory / record.relative_file).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise AssetServiceError("Retained asset bytes are unavailable.")
    content = path.read_bytes()
    if _sha256(content) != record.sha256:
        raise AssetServiceError("Retained asset failed its integrity check.")
    if record.media_type in {"image/png", "image/jpeg", "image/webp"}:
        if record.media_type != "image/png" or _sanitize_raster(content, record.media_type) != content:
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


def enumerate_private_visual_candidates(
    *, collection: str, source_sha256: str, manifest_sha256: str
) -> list[PrivateVisualCandidate]:
    """Return retained private visual bytes for review; this is not a player asset API."""
    catalog = _read_catalog(_catalog_path(collection, source_sha256, manifest_sha256))
    candidates: list[PrivateVisualCandidate] = []
    for record in catalog.entries.values():
        if record.kind not in {"source_map", "generated_illustration", "original_creature_art"}:
            continue
        if (
            record.collection != collection
            or record.source_sha256 != source_sha256
            or record.manifest_sha256 != manifest_sha256
            or not (record.scene_ids or record.passage_ids or record.entity_id)
        ):
            continue
        try:
            content = _read_artifact(record)
        except AssetServiceError:
            continue
        candidates.append(
            PrivateVisualCandidate(
                candidate_id=_visual_candidate_id(record),
                artifact_sha256=record.sha256,
                manifest_sha256=record.manifest_sha256,
                prepared_sha256=record.prepared_sha256,
                kind=record.kind,
                media_type=record.media_type,
                scene_ids=record.scene_ids,
                passage_ids=record.passage_ids,
                entity_id=record.entity_id,
                data=content,
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.candidate_id)


def _visual_record_by_candidate_id(
    *, collection: str, source_sha256: str, manifest_sha256: str, candidate_id: str
) -> AssetArtifactRecord | None:
    catalog = _read_catalog(_catalog_path(collection, source_sha256, manifest_sha256))
    return next(
        (
            record
            for record in catalog.entries.values()
            if record.collection == collection
            and record.source_sha256 == source_sha256
            and record.manifest_sha256 == manifest_sha256
            and record.kind in {"source_map", "generated_illustration", "original_creature_art"}
            and _visual_candidate_id(record) == candidate_id
        ),
        None,
    )


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


def release_player_visual(
    *,
    collection: str,
    source_sha256: str,
    manifest_sha256: str,
    candidate_id: str,
    review_artifact: IndependentVisualReviewArtifact | dict[str, Any],
) -> AssetArtifactRecord:
    """Release one exact visual only after its independent signed review validates."""
    catalog_path = _catalog_path(collection, source_sha256, manifest_sha256)
    directory = _version_dir(collection, source_sha256, manifest_sha256)
    lock_path = directory / "catalog.lock"
    candidate = IndependentVisualReviewArtifact.model_validate(review_artifact)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            catalog = _read_catalog(catalog_path)
            record = next(
                (
                    retained
                    for retained in catalog.entries.values()
                    if retained.kind in {
                        "source_map", "generated_illustration", "original_creature_art"
                    }
                    and _visual_candidate_id(retained) == candidate_id
                ),
                None,
            )
            if record is None:
                raise AssetServiceError("Visual artifact is unavailable for release.")
            if record.collection != collection or record.source_sha256 != source_sha256 or record.manifest_sha256 != manifest_sha256:
                raise AssetServiceError("Visual review does not match the artifact source version.")
            _read_artifact(record)
            if not _valid_visual_review(record, candidate):
                raise AssetServiceError("Independent visual review signature or scope did not validate.")
            released = record.model_copy(update={"visual_review": candidate})
            catalog.entries[record.artifact_id] = released
            _atomic_write(catalog_path, catalog.model_dump_json().encode())
            return released
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def sign_and_persist_visual_review(
    *,
    collection: str,
    source_sha256: str,
    manifest_sha256: str,
    decision: IndependentVisualReviewDecision | dict[str, Any],
) -> IndependentVisualReviewArtifact | None:
    """Validate exact critic output, then server-sign and persist only an approval.

    Critic-provided scope, reviewer identity, review ID and signature are deliberately absent.
    Rejected decisions return ``None`` and are never signed. The private signing key is read
    only from the server environment and never enters candidate or player payloads.
    """
    accepted = IndependentVisualReviewDecision.model_validate(decision)
    record = _visual_record_by_candidate_id(
        collection=collection,
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        candidate_id=accepted.candidate_id,
    )
    if record is None:
        raise AssetServiceError("Visual review candidate is unavailable or has changed.")
    if accepted.artifact_sha256 != record.sha256:
        raise AssetServiceError("Visual review digest does not match its exact candidate.")
    _read_artifact(record)
    if accepted.decision != "approved":
        return None

    encoded_private_key = os.environ.get("EDGAR_VISUAL_REVIEW_PRIVATE_KEY")
    encoded_public_key = os.environ.get("EDGAR_VISUAL_REVIEW_PUBLIC_KEY")
    if not encoded_private_key or not encoded_public_key:
        raise AssetServiceError("Trusted visual review signing keys are not configured.")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(encoded_private_key, validate=True)
        )
        configured_public_key = base64.b64decode(encoded_public_key, validate=True)
        derived_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        if derived_public_key != configured_public_key:
            raise AssetServiceError("Trusted visual review signing keys do not match.")
    except AssetServiceError:
        raise
    except Exception as exc:
        raise AssetServiceError("Trusted visual review signing key is invalid.") from exc

    unsigned = IndependentVisualReviewArtifact(
        review_id=f"visual_review_{uuid.uuid4().hex}",
        reviewer_id="p09_independent_critic",
        candidate_id=accepted.candidate_id,
        decision="approved",
        reasons=accepted.reasons,
        artifact_sha256=record.sha256,
        manifest_sha256=record.manifest_sha256,
        prepared_sha256=record.prepared_sha256,
        scene_ids=record.scene_ids,
        passage_ids=record.passage_ids,
        entity_id=record.entity_id,
        signature="A" * 40,
    )
    signed = unsigned.model_copy(
        update={
            "signature": base64.b64encode(
                private_key.sign(_visual_review_payload(unsigned))
            ).decode()
        }
    )
    released = release_player_visual(
        collection=collection,
        source_sha256=source_sha256,
        manifest_sha256=manifest_sha256,
        candidate_id=accepted.candidate_id,
        review_artifact=signed,
    )
    if released.visual_review is None:
        raise AssetServiceError("Signed visual review was not persisted.")
    return released.visual_review


def apply_visual_review_to_coverage(
    *,
    receipt: AssetCoverageReceipt,
    collection: str,
    source_sha256: str,
    review_artifact: IndependentVisualReviewArtifact | dict[str, Any],
) -> AssetCoverageReceipt:
    """Mark prepared player bytes reviewed after checking the persisted signed artifact."""
    review = IndependentVisualReviewArtifact.model_validate(review_artifact)
    record = _visual_record_by_candidate_id(
        collection=collection,
        source_sha256=source_sha256,
        manifest_sha256=receipt.manifest_sha256,
        candidate_id=review.candidate_id,
    )
    if (
        record is None
        or record.prepared_sha256 != receipt.prepared_sha256
        or record.visual_review != review
        or not _valid_visual_review(record, review)
    ):
        raise AssetServiceError("Persisted visual review does not match this coverage receipt.")
    matching = [
        index
        for index, item in enumerate(receipt.coverage)
        if item.asset_id == record.plan_asset_id
        and item.kind == record.kind
        and item.artifact_sha256 == record.sha256
        and item.status == "ready"
    ]
    if len(matching) != 1:
        raise AssetServiceError("Visual review does not match exactly one prepared asset receipt.")
    coverage = list(receipt.coverage)
    index = matching[0]
    coverage[index] = coverage[index].model_copy(
        update={"player_review_status": "approved"}
    )
    return receipt.model_copy(update={"coverage": coverage})


def apply_rejected_visual_review_to_coverage(
    *,
    receipt: AssetCoverageReceipt,
    collection: str,
    source_sha256: str,
    decision: IndependentVisualReviewDecision | dict[str, Any],
) -> AssetCoverageReceipt:
    """Record an exact critic rejection without signing or exposing the candidate."""
    rejected = IndependentVisualReviewDecision.model_validate(decision)
    if rejected.decision != "rejected":
        raise AssetServiceError("Only a rejected visual decision can mark coverage rejected.")
    record = _visual_record_by_candidate_id(
        collection=collection,
        source_sha256=source_sha256,
        manifest_sha256=receipt.manifest_sha256,
        candidate_id=rejected.candidate_id,
    )
    if (
        record is None
        or record.prepared_sha256 != receipt.prepared_sha256
        or rejected.artifact_sha256 != record.sha256
    ):
        raise AssetServiceError("Rejected visual decision does not match this coverage receipt.")
    matching = [
        index
        for index, item in enumerate(receipt.coverage)
        if item.asset_id == record.plan_asset_id
        and item.kind == record.kind
        and item.artifact_sha256 == record.sha256
        and item.status == "ready"
    ]
    if len(matching) != 1:
        raise AssetServiceError("Rejected visual decision does not match exactly one prepared asset receipt.")
    coverage = list(receipt.coverage)
    index = matching[0]
    coverage[index] = coverage[index].model_copy(
        update={"player_review_status": "rejected"}
    )
    return receipt.model_copy(update={"coverage": coverage})


def _record_for(
    collection: str, source_sha256: str, manifest_sha256: str, asset_id: str
) -> AssetArtifactRecord | None:
    catalog = _read_catalog(_catalog_path(collection, source_sha256, manifest_sha256))
    return catalog.entries.get(asset_id) or next(
        (record for record in catalog.entries.values() if record.plan_asset_id == asset_id),
        None,
    )


def _records_for_plan(
    collection: str, source_sha256: str, manifest_sha256: str, asset_id: str
) -> list[AssetArtifactRecord]:
    catalog = _read_catalog(_catalog_path(collection, source_sha256, manifest_sha256))
    return [record for record in catalog.entries.values() if record.plan_asset_id == asset_id]


def _render_act_map(manifest: AdventureManifest, act_id: str) -> bytes:
    prepared = manifest.prepared
    if prepared is None:
        raise AssetServiceError("Prepared topology is unavailable.")
    act = next((item for item in prepared.acts if item.act_id == act_id), None)
    if act is None:
        raise AssetServiceError("Canonical act is unavailable.")
    scoped = set(act.scene_ids)
    scene_by_id = {item.scene_id: item for item in manifest.scenes}
    projection = PlayerMapProjection(
        session_id=1,
        source_version=manifest.reviewed_sha256 or "0" * 64,
        topology="prepared",
        current_scene_id=None,
        places=[
            PlayerMapPlace(scene_id=sid, title=scene_by_id[sid].title, status="known")
            for sid in sorted(scoped)
            if sid in scene_by_id
        ],
        routes=[
            PlayerMapRoute(from_scene_id=route.from_scene_id, to_scene_id=route.to_scene_id)
            for route in prepared.routes
            if route.from_scene_id in scoped and route.to_scene_id in scoped
        ],
        note="Private canonical act map.",
    )
    return render_player_map_svg(projection)


def _crop_source_pdf(path: Path, crop_binding: Any) -> bytes:
    if not path.is_file():
        raise AssetServiceError("Authorized source PDF is unavailable.")
    if _sha256(path.read_bytes()) != crop_binding.source_sha256:
        raise AssetServiceError("Source PDF version no longer matches the retained crop.")
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - packaging error in production
        raise AssetServiceError("The local PDF renderer is unavailable.") from exc
    try:
        with pymupdf.open(path) as document:
            if crop_binding.page > len(document):
                raise AssetServiceError("Source page is unavailable.")
            page = document[crop_binding.page - 1]
            rect = page.rect
            crop = crop_binding.crop
            clip = pymupdf.Rect(
                rect.x0 + rect.width * crop.x,
                rect.y0 + rect.height * crop.y,
                rect.x0 + rect.width * (crop.x + crop.width),
                rect.y0 + rect.height * (crop.y + crop.height),
            )
            pixmap = page.get_pixmap(clip=clip, dpi=180, alpha=False)
            data = pixmap.tobytes("png")
    except AssetServiceError:
        raise
    except Exception as exc:
        raise AssetServiceError("Source PDF crop could not be rendered.") from exc
    if not data or len(data) > _MAX_ARTIFACT_BYTES:
        raise AssetServiceError("Rendered source crop exceeds the artifact size limit.")
    return data


def _extract_handout_text(
    manifest: AdventureManifest, passage_ids: list[str], page_texts: Mapping[int, str]
) -> bytes:
    passages = {passage.passage_id: passage for passage in manifest.passages}
    blocks: list[str] = []
    for passage_id in passage_ids:
        passage = passages.get(passage_id)
        if passage is None or passage.audience != "player":
            raise AssetServiceError("Handout text is not an exact reviewed player passage.")
        page = page_texts.get(passage.page)
        if page is None:
            raise AssetServiceError("The retained source page for this handout is unavailable.")
        text = page[passage.start : passage.end]
        if not text or text_digest(text) != passage.text_sha256:
            raise AssetServiceError("Handout source text no longer matches its reviewed digest.")
        blocks.append(text)
    return "\n\n".join(blocks).encode("utf-8")


def _scope_coverage(
    plan: AssetPlan, coverage: list[AssetCoverage]
) -> tuple[list[AssetScopeCoverage], list[AssetScopeCoverage]]:
    by_id = {item.asset_id: item for item in coverage}
    maps = [item for item in plan.items if item.kind == "schematic_map"]
    acts = [
        AssetScopeCoverage(
            scope="act",
            scope_id=item.act_id,
            required=item.required,
            status=by_id[item.asset_id].status if item.asset_id in by_id else "failed",
            asset_ids=[item.asset_id],
        )
        for item in maps
        if item.act_id is not None
    ]
    scenes: list[AssetScopeCoverage] = []
    for scene_id in plan.required_scene_ids:
        scoped = [item for item in maps if scene_id in item.scene_ids]
        ready = [item for item in scoped if by_id.get(item.asset_id) and by_id[item.asset_id].status == "ready"]
        scenes.append(
            AssetScopeCoverage(
                scope="scene",
                scope_id=scene_id,
                required=True,
                status="ready" if ready else "failed" if scoped else "unavailable",
                asset_ids=[item.asset_id for item in scoped],
            )
        )
    return acts, scenes


def _plan_scope_inventory_matches(manifest: AdventureManifest, plan: AssetPlan) -> bool:
    prepared = manifest.prepared
    if prepared is None:
        return False
    expected_acts = {act.act_id: set(act.scene_ids) for act in prepared.acts}
    expected_scenes = [scene.scene_id for scene in manifest.scenes]
    if set(plan.required_act_ids) != set(expected_acts) or len(plan.required_act_ids) != len(expected_acts):
        return False
    if plan.required_scene_ids != expected_scenes:
        return False
    maps = [item for item in plan.items if item.kind == "schematic_map"]
    if len(maps) != len(expected_acts) or {item.act_id for item in maps} != set(expected_acts):
        return False
    return all(
        item.required and item.act_id is not None and set(item.scene_ids) == expected_acts[item.act_id]
        for item in maps
    )


async def _run_image_job(
    *,
    collection: str,
    manifest: AdventureManifest,
    prepared: PreparedCampaign,
    item_id: str,
    prompt: str,
    scene_ids: list[str],
    provider: ImageGenerationProvider,
    profile: ImageProfile,
    profile_digest: str,
    max_image_calls: int,
    max_image_cost_microusd: int,
    max_image_call_cost_microusd: int,
    image_price_revision: str,
    image_price_model: str,
    image_price_size: str,
    image_price_quality: str,
    retry_failed: bool,
    on_reserved: Callable[[str], None],
) -> tuple[AssetArtifactRecord, int]:
    if (
        image_price_model != profile.model
        or image_price_size != profile.size
        or image_price_quality != profile.quality
        or not image_price_revision.strip()
    ):
        raise AssetServiceError("Frozen image price record does not match the effective image profile.")
    if isinstance(provider, OpenAIImageProvider):
        trusted_price = _trusted_image_price_record()
        if (
            trusted_price is None
            or trusted_price.revision != image_price_revision
            or trusted_price.model != profile.model
            or trusted_price.size != profile.size
            or trusted_price.quality != profile.quality
            or max_image_call_cost_microusd < trusted_price.unit_cost_microusd
        ):
            raise AssetServiceError(
                "Paid image generation requires a matching server-configured documented price record."
            )
    job_key = _job_key(
        collection=collection,
        manifest=manifest,
        prepared=prepared,
        item_id=item_id,
        prompt=prompt,
        profile=profile,
        profile_digest=profile_digest,
    )
    records = _records_for_plan(
        collection, manifest.source_sha256, manifest.reviewed_sha256 or "", item_id
    )
    record = next(
        (entry for entry in records if entry.job_key == job_key and entry.kind == "generated_illustration"),
        None,
    )
    if record is not None:
        _read_artifact(record)
        return record, 0
    version_dir = _version_dir(collection, manifest.source_sha256, manifest.reviewed_sha256 or "")
    job_path = version_dir / "jobs" / f"{job_key}.json"
    if job_path.is_file():
        job = json.loads(job_path.read_text())
        if job.get("state") == "failed" and not retry_failed:
            raise AssetServiceError("The previous image job failed; an explicit retry is required.")
        if job.get("state") == "completed":
            completed = next((entry for entry in records if entry.job_key == job_key), None)
            if completed is not None:
                _read_artifact(completed)
                return completed, 0
    attempt_id = _reserve_image_call(
        collection=collection,
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256 or "",
        prepared_sha256=prepared_digest(prepared),
        job_key=job_key,
        max_image_calls=max_image_calls,
        max_image_cost_microusd=max_image_cost_microusd,
        max_image_call_cost_microusd=max_image_call_cost_microusd,
        image_price_revision=image_price_revision,
        image_price_model=image_price_model,
        image_price_size=image_price_size,
        image_price_quality=image_price_quality,
        timeout_seconds=profile.timeout_seconds,
    )
    on_reserved(job_key)
    try:
        image = await provider.generate(prompt, profile)
        if image.cost_microusd is not None and image.cost_microusd > max_image_call_cost_microusd:
            raise ImageProviderUnavailable("Provider cost exceeded the frozen per-call image cap.")
        if image.media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ImageProviderUnavailable("The image provider returned an unsupported format.")
        record = _record_artifact(
            collection=collection,
            manifest=manifest,
            prepared=prepared,
            item_id=item_id,
            kind="generated_illustration",
            content=image.data,
            media_type=image.media_type,
            scene_ids=scene_ids,
            passage_ids=[],
            entity_id=None,
            profile=profile,
            profile_digest=profile_digest,
            provenance={"origin": "generated", "validation": "decorative_only"},
            job_key=job_key,
            expected_attempt_id=attempt_id,
            actual_cost_microusd=image.cost_microusd,
        )
        return record, (
            max_image_call_cost_microusd
            if image.cost_microusd is None
            else image.cost_microusd
        )
    except Exception:
        _finish_job(
            collection=collection,
            source_sha256=manifest.source_sha256,
            manifest_sha256=manifest.reviewed_sha256 or "",
            job_key=job_key,
            attempt_id=attempt_id,
            state="failed",
        )
        raise


async def prepare_assets(
    manifest: AdventureManifest | dict,
    plan: AssetPlan | dict,
    *,
    collection: str,
    source_pdf_path: Path | None = None,
    source_pdf_resolver: Callable[[str], Path | None] | None = None,
    page_texts: Mapping[int, str] | None = None,
    output_root: Path | None = None,
    image_profiles: ModelProfileSnapshot | None = None,
    image_budget: AssetImageBudget | dict | None = None,
    image_provider: ImageGenerationProvider | None = None,
    retry_failed: bool = False,
) -> AssetCoverageReceipt:
    """P08 production adapter; writes retained artifacts and a typed per-item gate receipt."""
    reviewed = require_published(manifest)
    candidate = AssetPlan.model_validate(plan)
    if not candidate.valid:
        return AssetCoverageReceipt(
            manifest_sha256=candidate.manifest_sha256,
            prepared_sha256=candidate.prepared_sha256,
            coverage=[],
            findings=["Asset plan failed deterministic validation."],
        )
    if reviewed.reviewed_sha256 != candidate.manifest_sha256 or reviewed.prepared is None:
        return AssetCoverageReceipt(
            manifest_sha256=candidate.manifest_sha256,
            prepared_sha256=candidate.prepared_sha256,
            coverage=[],
            findings=["Asset plan is stale for the reviewed prepared campaign."],
        )
    if prepared_digest(reviewed.prepared) != candidate.prepared_sha256:
        return AssetCoverageReceipt(
            manifest_sha256=candidate.manifest_sha256,
            prepared_sha256=candidate.prepared_sha256,
            coverage=[],
            findings=["Asset plan topology version is stale."],
        )
    if not _plan_scope_inventory_matches(reviewed, candidate):
        return AssetCoverageReceipt(
            manifest_sha256=candidate.manifest_sha256,
            prepared_sha256=candidate.prepared_sha256,
            coverage=[],
            findings=["Asset plan does not contain the exact required act and scene inventory."],
        )
    prepared = reviewed.prepared
    profiles = image_profiles or current_model_profiles()
    budget = AssetImageBudget.model_validate(image_budget or {"max_image_calls": 0})
    provider = image_provider or OpenAIImageProvider()
    coverage: list[AssetCoverage] = []
    findings = list(candidate.findings)
    image_calls_reserved = image_calls_succeeded = image_calls_failed = 0
    image_cost_reserved = image_cost_succeeded = image_cost_failed = 0
    image_cost_refunded = 0
    image_job_keys: list[str] = []
    root_token = _ASSET_ROOT_OVERRIDE.set(output_root) if output_root is not None else None
    try:
        for item in candidate.items:
            reserved_keys: list[str] = []
            image_cost_actual = 0
            try:
                record: AssetArtifactRecord | None = None
                if item.kind == "schematic_map":
                    content = _render_act_map(reviewed, item.act_id or "")
                    record = _record_artifact(
                        collection=collection, manifest=reviewed, prepared=prepared,
                        item_id=item.asset_id, kind="schematic_map", content=content,
                        media_type="image/svg+xml", scene_ids=item.scene_ids, passage_ids=[],
                        entity_id=None, provenance={"origin": "deterministic", "scale": None},
                    )
                elif item.kind == "source_map":
                    source = item.source_crop
                    path = source_pdf_path
                    if source and source.collection != collection and source_pdf_resolver:
                        path = source_pdf_resolver(source.collection)
                    if source is None or path is None:
                        raise AssetServiceError("Exact retained source map crop is unavailable.")
                    content = await asyncio.to_thread(_crop_source_pdf, path, source)
                    record = _record_artifact(
                        collection=collection, manifest=reviewed, prepared=prepared,
                        item_id=item.asset_id, kind="source_map", content=content,
                        media_type="image/png", scene_ids=item.scene_ids, passage_ids=item.passage_ids,
                        entity_id=None,
                        provenance={"origin": "source", **source.model_dump(mode="json")},
                        job_key=hashlib.sha256(
                            json.dumps(source.model_dump(mode="json"), sort_keys=True).encode()
                        ).hexdigest(),
                    )
                elif item.kind == "handout":
                    if page_texts is None:
                        raise AssetServiceError("Retained source pages for this handout are unavailable.")
                    content = _extract_handout_text(reviewed, item.passage_ids, page_texts)
                    record = _record_artifact(
                        collection=collection, manifest=reviewed, prepared=prepared,
                        item_id=item.asset_id, kind="handout", content=content,
                        media_type="text/plain; charset=utf-8", scene_ids=item.scene_ids,
                        passage_ids=item.passage_ids, entity_id=None,
                        provenance={"origin": "exact_reviewed_player_passages"},
                    )
                elif item.kind == "original_creature_art":
                    if item.artwork_status != "available" or item.creature_artwork is None:
                        coverage.append(
                            AssetCoverage(
                                asset_id=item.asset_id, kind=item.kind, required=item.required,
                                status="unavailable",
                                explanation=("Original artwork is ambiguous." if item.artwork_status == "ambiguous" else "No exact retained original artwork is available."),
                            )
                        )
                        continue
                    binding = item.creature_artwork
                    path = source_pdf_resolver(binding.source.collection) if source_pdf_resolver else None
                    if path is None and binding.source.collection == collection:
                        path = source_pdf_path
                    if path is None:
                        raise AssetServiceError("Exact original source book is unavailable.")
                    content = await asyncio.to_thread(_crop_source_pdf, path, binding.source)
                    record = _record_artifact(
                        collection=collection, manifest=reviewed, prepared=prepared,
                        item_id=item.asset_id, kind="original_creature_art", content=content,
                        media_type="image/png", scene_ids=item.scene_ids,
                        passage_ids=[], entity_id=item.entity_id,
                        provenance={"origin": "original_source", **binding.model_dump(mode="json")},
                        job_key=hashlib.sha256(
                            json.dumps(binding.model_dump(mode="json"), sort_keys=True).encode()
                        ).hexdigest(),
                    )
                elif item.kind == "generated_illustration":
                    if budget.max_image_calls == 0:
                        raise ImageBudgetExceeded("Image generation is disabled by the frozen budget.")
                    if item.prompt is None:
                        raise AssetServiceError("Generated illustration prompt is unavailable.")
                    profile = effective_image_profile("image.illustration", profiles)
                    record, image_cost_actual = await _run_image_job(
                        collection=collection, manifest=reviewed, prepared=prepared,
                        item_id=item.asset_id, prompt=item.prompt, scene_ids=item.scene_ids,
                        provider=provider, profile=profile, profile_digest=profiles.digest,
                        max_image_calls=budget.max_image_calls, retry_failed=retry_failed,
                        max_image_cost_microusd=budget.max_image_cost_microusd,
                        max_image_call_cost_microusd=budget.max_image_call_cost_microusd,
                        image_price_revision=budget.image_price_revision or "",
                        image_price_model=budget.image_price_model or "",
                        image_price_size=budget.image_price_size or "",
                        image_price_quality=budget.image_price_quality or "",
                        on_reserved=reserved_keys.append,
                    )
                    image_calls_reserved += len(reserved_keys)
                    image_calls_succeeded += len(reserved_keys)
                    image_cost_reserved += budget.max_image_call_cost_microusd * len(reserved_keys)
                    image_cost_succeeded += image_cost_actual
                    image_cost_refunded += (
                        budget.max_image_call_cost_microusd - image_cost_actual
                    ) * len(reserved_keys)
                    image_job_keys.extend(reserved_keys)
                if record is not None:
                    coverage.append(
                        AssetCoverage(
                            asset_id=item.asset_id, kind=item.kind, required=item.required,
                            status="ready", artifact_sha256=record.sha256,
                            player_review_status=(
                                "pending"
                                if item.kind in {
                                    "source_map", "generated_illustration", "original_creature_art"
                                }
                                else "not_required"
                            ),
                        )
                    )
                else:
                    raise AssetServiceError("No retained artifact was produced.")
            except (AssetJobBusy, ImageBudgetExceeded) as exc:
                coverage.append(
                    AssetCoverage(
                        asset_id=item.asset_id, kind=item.kind, required=item.required,
                        status="failed", explanation=str(exc),
                    )
                )
                if item.required:
                    findings.append(f"Required asset {item.asset_id} could not be prepared.")
            except Exception as exc:
                if item.kind == "generated_illustration" and reserved_keys:
                    image_calls_reserved += len(reserved_keys)
                    image_calls_failed += len(reserved_keys)
                    image_cost_reserved += budget.max_image_call_cost_microusd * len(reserved_keys)
                    image_cost_failed += budget.max_image_call_cost_microusd * len(reserved_keys)
                    image_job_keys.extend(reserved_keys)
                coverage.append(
                    AssetCoverage(
                        asset_id=item.asset_id, kind=item.kind, required=item.required,
                        status="failed", explanation=(str(exc)[:500] if isinstance(exc, AssetServiceError) else "Asset preparation failed."),
                    )
                )
                if item.required:
                    findings.append(f"Required asset {item.asset_id} could not be prepared.")
        acts, scenes = _scope_coverage(candidate, coverage)
        expected_asset_ids = sorted(item.asset_id for item in candidate.items)
        actual_asset_ids = sorted(item.asset_id for item in coverage)
        expected_act_ids = sorted(candidate.required_act_ids)
        actual_act_ids = sorted(item.scope_id for item in acts)
        expected_scene_ids = sorted(candidate.required_scene_ids)
        actual_scene_ids = sorted(item.scope_id for item in scenes)
        if (
            actual_asset_ids != expected_asset_ids
            or actual_act_ids != expected_act_ids
            or actual_scene_ids != expected_scene_ids
        ):
            findings.append("Asset coverage receipt does not match the exact planned scope inventory.")
        return AssetCoverageReceipt(
            manifest_sha256=candidate.manifest_sha256,
            prepared_sha256=candidate.prepared_sha256,
            profile_digest=profiles.digest,
            coverage=coverage,
            findings=findings,
            image_price_revision=budget.image_price_revision,
            image_cost_limit_microusd=budget.max_image_cost_microusd,
            image_call_cost_limit_microusd=budget.max_image_call_cost_microusd,
            image_calls_reserved=image_calls_reserved,
            image_calls_succeeded=image_calls_succeeded,
            image_calls_failed=image_calls_failed,
            image_cost_reserved_microusd=image_cost_reserved,
            image_cost_succeeded_microusd=image_cost_succeeded,
            image_cost_failed_microusd=image_cost_failed,
            image_cost_refunded_microusd=image_cost_refunded,
            image_job_keys=image_job_keys,
            act_coverage=acts,
            scene_coverage=scenes,
        )
    finally:
        if root_token is not None:
            _ASSET_ROOT_OVERRIDE.reset(root_token)


def validate_asset_coverage(
    collection: str,
    manifest: AdventureManifest | dict,
    plan: AssetPlan | dict,
    receipt: AssetCoverageReceipt | dict,
) -> bool:
    """Revalidate a claimed-ready receipt against today's plan, topology and retained bytes.

    This is the trust boundary for any caller that consumes ``required_ready``. Receipt flags
    alone are not evidence: every ready item must resolve to exactly one matching retained
    record, whose bytes are rehashed and whose visual review signature is rechecked.
    """
    try:
        canonical = require_published(manifest)
        candidate = AssetPlan.model_validate(plan)
        coverage_receipt = AssetCoverageReceipt.model_validate(receipt)
        if (
            not canonical.reviewed_sha256
            or canonical.prepared is None
            or not candidate.valid
            or candidate.source_mode != canonical.prepared.source_mode
            or candidate.manifest_sha256 != canonical.reviewed_sha256
            or candidate.prepared_sha256 != prepared_digest(canonical.prepared)
            or coverage_receipt.manifest_sha256 != canonical.reviewed_sha256
            or coverage_receipt.prepared_sha256 != candidate.prepared_sha256
            or coverage_receipt.findings
            or not _plan_scope_inventory_matches(canonical, candidate)
        ):
            return False

        plan_ids = [item.asset_id for item in candidate.items]
        coverage_ids = [item.asset_id for item in coverage_receipt.coverage]
        if (
            len(set(plan_ids)) != len(plan_ids)
            or len(coverage_ids) != len(set(coverage_ids))
            or set(plan_ids) != set(coverage_ids)
        ):
            return False
        plan_by_id = {item.asset_id: item for item in candidate.items}
        catalog = _read_catalog(
            _catalog_path(collection, canonical.source_sha256, canonical.reviewed_sha256)
        )
        for item_coverage in coverage_receipt.coverage:
            item = plan_by_id[item_coverage.asset_id]
            if (
                item_coverage.kind != item.kind
                or item_coverage.required != item.required
                or len(set(item.scene_ids)) != len(item.scene_ids)
                or len(set(item.passage_ids)) != len(item.passage_ids)
            ):
                return False
            is_visual = item.kind in {
                "source_map", "generated_illustration", "original_creature_art"
            }
            if not is_visual and item_coverage.player_review_status != "not_required":
                return False
            if item_coverage.status != "ready":
                if item.required:
                    return False
                if item_coverage.artifact_sha256 is not None:
                    return False
                continue
            if item_coverage.artifact_sha256 is None:
                return False

            matches = [
                record
                for record in catalog.entries.values()
                if record.plan_asset_id == item.asset_id
                and record.kind == item.kind
                and record.sha256 == item_coverage.artifact_sha256
                and record.collection == collection
                and record.source_sha256 == canonical.source_sha256
                and record.extraction_sha256 == canonical.extraction_sha256
                and record.manifest_sha256 == canonical.reviewed_sha256
                and record.prepared_sha256 == candidate.prepared_sha256
                and record.scene_ids == item.scene_ids
                and record.passage_ids == item.passage_ids
                and record.entity_id == item.entity_id
            ]
            if len(matches) != 1:
                return False
            record = matches[0]
            expected_media_type = (
                "image/svg+xml"
                if item.kind == "schematic_map"
                else "text/plain; charset=utf-8"
                if item.kind == "handout"
                else "image/png"
            )
            if record.media_type != expected_media_type:
                return False
            _read_artifact(record)
            if is_visual:
                if item_coverage.player_review_status == "approved":
                    if not _valid_visual_review(record):
                        return False
                elif item.required or item_coverage.player_review_status == "not_required":
                    return False

        expected_acts, expected_scenes = _scope_coverage(candidate, coverage_receipt.coverage)
        if (
            [entry.model_dump(mode="json") for entry in coverage_receipt.act_coverage]
            != [entry.model_dump(mode="json") for entry in expected_acts]
            or [entry.model_dump(mode="json") for entry in coverage_receipt.scene_coverage]
            != [entry.model_dump(mode="json") for entry in expected_scenes]
            or not coverage_receipt.required_ready
        ):
            return False
        return True
    except Exception:
        return False


def get_asset_bytes(
    *, collection: str, source_sha256: str, manifest_sha256: str, asset_id: str
) -> tuple[AssetArtifactRecord, bytes] | None:
    record = _record_for(collection, source_sha256, manifest_sha256, asset_id)
    if record is None:
        return None
    return record, _read_artifact(record)


def image_usage(
    *, collection: str, source_sha256: str, manifest_sha256: str, prepared_sha256: str
) -> dict[str, Any]:
    """Read durable reservation and outcome counters for supervisor reconciliation."""
    directory = _version_dir(collection, source_sha256, manifest_sha256)
    jobs_dir = directory / "jobs"
    jobs = []
    if jobs_dir.is_dir():
        for path in jobs_dir.glob("*.json"):
            if not path.is_file():
                continue
            job = json.loads(path.read_text())
            if job.get("prepared_sha256") == prepared_sha256:
                jobs.append(job)
    budget_path = _budget_file(collection, source_sha256, manifest_sha256, prepared_sha256)
    budget = (
        json.loads(budget_path.read_text())
        if budget_path.is_file()
        else {"reserved_calls": 0, "reserved_cost_microusd": 0}
    )
    known_jobs = sorted(
        path.stem
        for path in jobs_dir.glob("*.json")
        if path.is_file()
        and json.loads(path.read_text()).get("prepared_sha256") == prepared_sha256
    ) if jobs_dir.is_dir() else []
    return {
        "prepared_sha256": prepared_sha256,
        "reserved": int(budget.get("reserved_calls", 0)),
        "call_limit": int(budget.get("limit_calls", 0)),
        "cost_limit_microusd": int(budget.get("limit_cost_microusd", 0)),
        "call_cost_limit_microusd": int(budget.get("max_image_call_cost_microusd", 0)),
        "price_record": budget.get("price_record"),
        "reserved_cost_microusd": int(budget.get("reserved_cost_microusd", 0)),
        "gross_reserved_cost_microusd": sum(
            int(job.get("cost_reserved_microusd", 0)) for job in jobs
        ),
        "succeeded_cost_microusd": sum(
            int(job.get("cost_succeeded_microusd", 0)) for job in jobs
        ),
        "failed_cost_microusd": sum(
            int(job.get("cost_failed_microusd", 0)) for job in jobs
        ),
        "running_cost_microusd": sum(
            int(job.get("active_call_cost_microusd", 0))
            for job in jobs
            if job.get("state") == "running"
        ),
        "refunded_cost_microusd": sum(
            int(job.get("cost_refunded_microusd", 0)) for job in jobs
        ),
        "succeeded": sum(int(job.get("calls_succeeded", 0)) for job in jobs),
        "failed": sum(int(job.get("calls_failed", 0)) for job in jobs),
        "running": sum(1 for job in jobs if job.get("state") == "running"),
        "job_keys": known_jobs,
    }


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
    catalog = _read_catalog(_catalog_path(collection, manifest.source_sha256, manifest.reviewed_sha256))
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
            "handout", "source_map", "generated_illustration", "original_creature_art"
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
                passage.passage_id
                for passage in manifest.passages
                if passage.audience == "player"
            }
            if not record.passage_ids or not set(record.passage_ids) <= player_passages:
                continue
        elif not _valid_visual_review(record):
            continue
        try:
            _read_artifact(record)
        except AssetServiceError:
            continue
        approved.append({
            "asset_id": _public_asset_id(session_id, record.artifact_id),
            "kind": record.kind,
            "url": f"/api/sessions/{session_id}/assets/{_public_asset_id(session_id, record.artifact_id)}",
            "media_type": record.media_type,
            "sha256": record.sha256,
        })
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


__all__ = [
    "AssetImageBudget", "AssetServiceError", "GeneratedImage", "ImageGenerationProvider",
    "ImageProviderUnavailable", "OpenAIImageProvider", "PlayerMapRead", "authorized_player_assets",
    "apply_rejected_visual_review_to_coverage", "apply_visual_review_to_coverage",
    "enumerate_private_visual_candidates",
    "get_asset_bytes", "plan_assets", "prepare_assets", "release_player_visual",
    "sign_and_persist_visual_review", "validate_asset_coverage",
    "get_authorized_player_asset_bytes",
    "image_usage",
]
