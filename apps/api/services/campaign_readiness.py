"""One read-only projection for the gates that admit a turn.

The projection intentionally invokes the same source, preparation and published-manifest
checks used by turn setup. It is an observation only: the turn path repeats these checks and
persist_turn still checks source/version/assignment state at commit time.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from db.postgres.models import Campaign
from edgar_core.manifests import ManifestError

from ..schemas.readiness import ReadinessBlocker, ReadinessRead
from .campaign_sources import is_private_preparation_source, source_reads

_COPY = {
    "source_missing": (
        "The campaign source is missing or has no usable text. Recover the source to continue.",
        "recover_source",
    ),
    "source_unavailable": (
        "The campaign source cannot be checked right now. Try again when it is available.",
        "retry_readiness",
    ),
    "source_review_needed": (
        "The source extraction needs review or recovery before play can continue.",
        "review_source",
    ),
    "adventure_review_needed": (
        "Review and publish the adventure before starting or continuing play.",
        "review_adventure",
    ),
    "saved_version_conflict": (
        "This saved session refers to an adventure version that needs review before play can continue.",
        "review_adventure",
    ),
    "preparation_in_progress": (
        "Campaign preparation is in progress. Check back when it finishes.",
        "wait_for_preparation",
    ),
    "preparation_failed": (
        "Campaign preparation needs attention before play can continue.",
        "repair_preparation",
    ),
    "rules_edition_mismatch": (
        "The reviewed adventure does not match this campaign's rules edition.",
        "review_adventure",
    ),
    "choose_character": (
        "Choose a character assigned to this campaign before playing.",
        "choose_character",
    ),
    "character_assignment_conflict": (
        "This character is assigned to another campaign. Choose an available character.",
        "choose_character",
    ),
    "completed": (
        "This campaign is complete. Read the saved story or reopen the campaign to continue.",
        "read_history",
    ),
    "readiness_unavailable": (
        "Campaign readiness could not be checked. Retry before starting a turn.",
        "retry_readiness",
    ),
}


def _blocker(code: str) -> ReadinessBlocker:
    message, action = _COPY[code]
    return ReadinessBlocker(code=code, message=message, action=action)


def _project(blockers: list[ReadinessBlocker]) -> ReadinessRead:
    unique: list[ReadinessBlocker] = []
    seen: set[str] = set()
    for blocker in blockers:
        if blocker.code not in seen:
            unique.append(blocker)
            seen.add(blocker.code)
    blockers = unique
    if not blockers:
        return ReadinessRead(
            play_allowed=True,
            history_allowed=True,
            state="ready",
            primary_action="play",
            blockers=[],
        )
    first = blockers[0]
    return ReadinessRead(
        play_allowed=False,
        history_allowed=True,
        state=first.code,
        primary_action=first.action,
        blockers=blockers,
    )


async def evaluate_play_readiness(
    db: AsyncSession | None,
    campaign: Campaign,
    *,
    character_id: int | None = None,
    require_character: bool = False,
    session_id: int | None = None,
    observed_sources=None,
) -> ReadinessRead:
    """Read all current turn gates and return only safe categories and player copy.

    Campaign surfaces call this without a character to expose campaign blockers. Session,
    intro and turn surfaces set ``require_character`` so a missing or conflicting assignment
    is visible before any provider work or session write. Session surfaces also provide
    ``session_id`` to validate accepted, session-owned amendments against current reviewed
    manifests without writing or invoking a provider.
    """
    try:
        if campaign.status == "ended":
            return _project([_blocker("completed")])

        blockers: list[ReadinessBlocker] = []
        sources = []
        private_preparation_slugs: set[str] = set()
        if campaign.source_mode != "homebrew":
            sources = (
                observed_sources
                if observed_sources is not None
                else await source_reads(list(campaign.adventure_collections or []))
            )
            if not sources:
                blockers.append(_blocker("source_missing"))
            for source in sources:
                slug = getattr(source, "slug", None)
                if isinstance(slug, str) and await run_in_threadpool(
                    is_private_preparation_source, slug
                ):
                    private_preparation_slugs.add(slug)
            for source in sources:
                status = getattr(source, "status", None)
                job = getattr(source, "job", None)
                private_preparation = getattr(source, "slug", None) in private_preparation_slugs
                if status == "ready":
                    continue
                if private_preparation and status == "processing":
                    code = "preparation_in_progress"
                elif private_preparation or (
                    job is not None and getattr(job, "state", None) == "failed"
                ):
                    code = "preparation_failed"
                elif status in ("missing", "empty"):
                    code = "source_missing"
                elif status == "unavailable":
                    code = "source_unavailable"
                elif status == "processing":
                    code = "preparation_in_progress"
                else:
                    code = "source_review_needed"
                blockers.append(_blocker(code))

        # This is the existing prepared-play predicate used by create/session/turn paths.
        # It validates a private run's immutable edition and selected hero binding.
        sources_ready = (
            campaign.source_mode == "homebrew"
            or bool(sources)
            and all(getattr(source, "status", None) == "ready" for source in sources)
        )
        if sources_ready:
            from .prepared_play import require_prepared_play

            character = None
            if require_character and character_id is not None and db is not None:
                from db.postgres.models import Character

                character = await db.get(Character, character_id)

            try:
                await run_in_threadpool(
                    require_prepared_play,
                    [] if campaign.source_mode == "homebrew" else campaign.adventure_collections,
                    campaign.rules_edition,
                    character_id=character_id,
                    character=character,
                    # Ordinary sessions may be started without a character. Prepared runs
                    # still require their commissioned hero even when this session has none.
                    require_hero=require_character
                    and (character_id is not None or bool(private_preparation_slugs)),
                )
            except HTTPException as exc:
                code = getattr(exc, "readiness_code", None)
                blockers.append(_blocker(code if code in _COPY else "preparation_failed"))

        # Loading published manifests proves current canonical extraction/source identity,
        # explicit review receipt, and exact version binding. Vector chunks alone are not enough.
        manifests: dict[str, dict] = {}
        if sources_ready and campaign.source_mode != "homebrew":
            from .adventure_manifests import campaign_manifests

            try:
                manifests = await run_in_threadpool(
                    campaign_manifests, list(campaign.adventure_collections or [])
                )
            except ManifestError:
                blockers.append(_blocker("adventure_review_needed"))
            except Exception:
                blockers.append(_blocker("readiness_unavailable"))

            if not any(b.code == "adventure_review_needed" for b in blockers):
                for manifest in manifests.values():
                    edition = manifest.get("rules_edition")
                    if edition not in ("2014", "2024") or edition != campaign.rules_edition:
                        blockers.append(_blocker("rules_edition_mismatch"))
                        break

        if session_id is not None:
            if db is None:
                blockers.append(_blocker("readiness_unavailable"))
            else:
                from .prepared_amendments import (
                    PreparedAmendmentConflict,
                    load_session_prepared_amendments,
                )

                try:
                    await load_session_prepared_amendments(db, session_id, campaign.id, manifests)
                except PreparedAmendmentConflict:
                    blockers.append(_blocker("saved_version_conflict"))
                except Exception:
                    # Corrupt replay data is never treated as a playable save. Do not expose
                    # private payload details or try to repair it during a read.
                    blockers.append(_blocker("readiness_unavailable"))

        if require_character and character_id is not None:
            if db is None:
                blockers.append(_blocker("readiness_unavailable"))
            else:
                from .character_assignments import (
                    load_active_assignment,
                    load_active_assignment_any_campaign,
                )

                assignment = await load_active_assignment(db, character_id, campaign.id)
                if assignment is None:
                    elsewhere = await load_active_assignment_any_campaign(db, character_id)
                    blockers.append(
                        _blocker(
                            "character_assignment_conflict"
                            if elsewhere is not None
                            else "choose_character"
                        )
                    )

        # Return all independent blockers when safe to check them, while retaining stable order.
        return _project(blockers)
    except Exception:
        # A failed read is never interpreted as readiness. Do not put exception text in the DTO.
        return _project([_blocker("readiness_unavailable")])


def require_play_readiness(readiness: ReadinessRead) -> None:
    """Translate the shared projection into stable turn-preflight errors."""
    if readiness.play_allowed:
        return
    blocker = readiness.blockers[0] if readiness.blockers else _blocker("readiness_unavailable")
    if blocker.code in ("source_missing", "source_unavailable", "source_review_needed"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "campaign_sources_unavailable",
                "message": blocker.message,
                "retrieval_degraded": True,
            },
        )
    if blocker.code == "adventure_review_needed":
        raise HTTPException(
            status_code=409,
            detail={"code": "adventure_manifest_review_required", "message": blocker.message},
        )
    if blocker.code == "saved_version_conflict":
        raise HTTPException(
            status_code=409,
            detail={"code": blocker.code, "message": blocker.message},
        )
    if blocker.code == "choose_character":
        raise HTTPException(
            status_code=409,
            detail={"code": "character_not_assigned", "message": blocker.message},
        )
    status = 503 if blocker.code == "readiness_unavailable" else 409
    raise HTTPException(
        status_code=status,
        detail={"code": blocker.code, "message": blocker.message},
    )
