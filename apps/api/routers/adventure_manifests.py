"""Explicit JSON review interface; drafts are never implicitly approved by compilation."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from edgar_core.manifests import AdventureManifest

from ..auth import Auth, owned_campaign, require_admin
from ..dependencies import get_session
from ..services import adventure_manifests as service
from ..services.campaign_membership import is_solo_campaign
from ..services.campaign_sources import campaign_read
from ..services.solo_review import (
    SoloReview,
    build_solo_review,
    record_spoiler_review_opened,
    shared_review,
)

# Manifest review and publication change the shared adventure catalogue: operator (admin) only.
router = APIRouter(
    prefix="/adventures/{slug}/manifest",
    tags=["adventures"],
    dependencies=[Depends(require_admin)],
)


class ManifestReview(BaseModel):
    reviewed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1, max_length=200)


class SpoilerOpenedRequest(BaseModel):
    """Empty JSON body on purpose: a JSON body needs a CORS preflight, so no cross-site simple request reaches it."""

    model_config = ConfigDict(extra="forbid")


def _call(action, *args, **kwargs):
    from ..services.campaign_sources import validate_binding
    from ..services.preparation_privacy import require_public_source

    validate_binding(args[0])
    require_public_source(args[0])
    try:
        return action(*args, **kwargs)
    except service.ManifestSourceUnverified as exc:
        # Retrying review cannot succeed without a verified source record, so the host gets a
        # distinct code and a next step rather than the generic review-required gate.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "adventure_source_unverified",
                "message": "This adventure has no verified source record, so its review draft cannot be prepared. Start a new campaign with the current preparation pipeline, then remove this one.",
            },
        ) from exc
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


async def _campaign_adventure(db: AsyncSession, campaign_id: int, auth: Auth, slug: str):
    """The campaign must own this adventure; a foreign or unknown pair is the same 404."""
    campaign = await owned_campaign(db, campaign_id, auth)
    bound = set(campaign.adventure_collections or []) | {
        source.slug for source in (await campaign_read(campaign)).sources
    }
    if slug not in bound:
        raise HTTPException(status_code=404, detail="Adventure not found in this campaign")
    return campaign


@router.get("/solo-review", response_model=SoloReview)
async def solo_review(
    slug: str,
    campaign_id: int,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    """Spoiler-safe review for a solo host; shared campaigns get the mode only (EDG-176)."""
    campaign = await _campaign_adventure(db, campaign_id, auth, slug)
    if not await is_solo_campaign(db, campaign):
        return shared_review()
    return await run_in_threadpool(_call, build_solo_review, slug)


@router.post("/solo-review/spoiler-opened")
async def open_spoiler_review(
    slug: str,
    campaign_id: int,
    body: SpoilerOpenedRequest,
    auth: Auth,
    db: AsyncSession = Depends(get_session),
):
    """Records the opt-in spoiler review. It changes no audience, draft or publication state."""
    campaign = await _campaign_adventure(db, campaign_id, auth, slug)
    if not await is_solo_campaign(db, campaign):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "shared_review_required",
                "message": "Shared campaigns use the manual review.",
            },
        )
    user_id = auth.user_id if auth is not None else None
    await run_in_threadpool(record_spoiler_review_opened, slug, campaign.id, user_id)
    return {"recorded": True}
