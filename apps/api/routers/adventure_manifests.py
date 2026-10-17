"""Explicit JSON review interface; drafts are never implicitly approved by compilation."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from edgar_core.manifests import AdventureManifest

from ..auth import require_admin
from ..services import adventure_manifests as service

# Manifest review and publication change the shared adventure catalogue: operator (admin) only.
router = APIRouter(
    prefix="/adventures/{slug}/manifest",
    tags=["adventures"],
    dependencies=[Depends(require_admin)],
)


class ManifestReview(BaseModel):
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)


def _call(action, *args, **kwargs):
    from ..services.campaign_sources import validate_binding
    from ..services.preparation_privacy import require_public_source

    validate_binding(args[0])
    require_public_source(args[0])
    try:
        return action(*args, **kwargs)
    except (ValueError, OSError, KeyError) as exc:
        # Validation messages are written for the host. I/O and lookup errors can carry file
        # paths or internal keys, so those get a fixed safe message instead.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            else "The adventure review data could not be read. Reload the draft and try again."
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "adventure_manifest_review_required", "message": message},
        ) from exc


@router.post("/compile")
def compile_manifest(slug: str):
    return _call(service.compile_draft, slug)


@router.get("")
def inspect_manifest(slug: str):
    return _call(service.inspect_manifest, slug)


@router.put("")
def edit_manifest(slug: str, body: AdventureManifest):
    return _call(service.edit_draft, slug, body)


@router.post("/publish")
def publish_manifest(slug: str, body: ManifestReview):
    return _call(service.publish_manifest, slug, **body.model_dump())
