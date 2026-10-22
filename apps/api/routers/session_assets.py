"""Read-only player map and retained artwork endpoints scoped to one session."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import Response as BinaryResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from db.postgres.models import Campaign, EventLog
from edgar_core.assets import PlayerMapProjection, project_player_map, render_player_map_svg
from edgar_core.manifests import AdventureManifest
from edgar_core.story import StoryFact
from edgar_core.world_map import PlayerWorldMap, project_world_map, render_world_map_svg

from ..auth import Auth, member_session
from ..dependencies import get_session
from ..services import adventure_assets
from ..services.adventure_manifests import campaign_manifests
from ..services.journal import project_player_journal
from ..services.journal_identity import journal_entry_id
from ..services.story_state import load_story_state, session_story_flags
from ..services.world_flags import load_world_flags
from ._common import get_or_404

router = APIRouter(tags=["session-assets"])


async def _session_map_context(session_id: int, db: AsyncSession, auth: Auth) -> dict[str, Any]:
    # Player projection only (EDG-046): the map, map.svg and assets are built from the journal projection.
    session = await member_session(db, session_id, auth)
    campaign = await get_or_404(db, Campaign, session.campaign_id, "Campaign")
    state = await load_story_state(db, session_id, session.campaign_id)
    facts = [StoryFact.model_validate(raw) for raw in state.get("facts", [])]
    event_result = await db.execute(
        select(EventLog)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .order_by(EventLog.created_at.asc(), EventLog.id.asc())
    )
    journal = project_player_journal(
        session_id,
        state,
        event_result.scalars().all(),
        active_character_id=session.active_character_id,
        current_scene_id=session.current_scene_id,
    )
    manifests: dict[str, AdventureManifest] = {}
    if campaign.source_mode != "homebrew" and campaign.adventure_collections:
        try:
            raw_manifests = await run_in_threadpool(
                campaign_manifests, list(campaign.adventure_collections)
            )
            manifests = {
                name: AdventureManifest.model_validate(raw) for name, raw in raw_manifests.items()
            }
        except Exception:
            # A broken or stale source cannot make the map endpoint disclose template data.
            manifests = {}
    return {
        "session": session,
        "campaign": campaign,
        "facts": facts,
        "journal": journal,
        "manifests": manifests,
        "state": state,
    }


def _select_manifest(context: dict[str, Any]) -> tuple[str, AdventureManifest] | None:
    session = context["session"]
    manifests: dict[str, AdventureManifest] = context["manifests"]
    facts: list[StoryFact] = context["facts"]
    matching: set[str] = set()
    for fact in facts:
        target = fact.target
        if target.kind != "scene" or fact.kind not in {"learn_place", "visit", "move"}:
            continue
        manifest = manifests.get(target.collection)
        if (
            manifest is not None
            and manifest.source_sha256 == target.source_sha256
            and manifest.reviewed_sha256 == target.manifest_sha256
        ):
            matching.add(target.collection)
    if len(matching) == 1:
        collection = next(iter(matching))
        return collection, manifests[collection]
    if len(matching) > 1:
        return None
    current_matches = [
        (collection, manifest)
        for collection, manifest in manifests.items()
        if session.current_scene_id
        and any(scene.scene_id == session.current_scene_id for scene in manifest.scenes)
    ]
    return current_matches[0] if len(current_matches) == 1 else None


async def _player_world_map(
    collection: str, projection: PlayerMapProjection
) -> PlayerWorldMap | None:
    """Project the prepared world map with the same earned scene knowledge as the scene map."""
    from ..services.campaign_prep.runtime import published_world_map

    prepared = await run_in_threadpool(published_world_map, collection)
    if prepared is None:
        return None
    return project_world_map(
        prepared,
        known_scene_ids={place.scene_id for place in projection.places},
        visited_scene_ids={
            place.scene_id for place in projection.places if place.status != "known"
        },
        current_scene_id=projection.current_scene_id,
    )


def _known_place_entries(journal: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": entry.id,
            "title": entry.title,
            "text": entry.text,
            "status": entry.status,
            "origin": entry.origin.model_dump(mode="json"),
        }
        for entry in journal.entries
        if entry.category == "place"
    ]


@router.get("/sessions/{session_id}/map")
async def get_session_map(
    session_id: int,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return a map built only from the selected session's committed place knowledge."""
    response.headers["Cache-Control"] = "private, no-store"
    context = await _session_map_context(session_id, db, auth)
    selected = _select_manifest(context)
    session = context["session"]
    journal = context["journal"]
    facts: list[StoryFact] = context["facts"]
    if selected is None:
        projection = PlayerMapProjection(
            session_id=session_id,
            source_version=hashlib.sha256(
                f"edgar-map-v1:{session_id}:{context['campaign'].id}".encode()
            ).hexdigest(),
            topology="unavailable",
            current_scene_id=None,
            places=[],
            routes=[],
            note="Reviewed map topology is unavailable for the known places in this session.",
        )
        return {
            **projection.model_dump(mode="json"),
            "journal_entry_ids": {},
            "known_place_entries": _known_place_entries(journal),
            "artwork": [],
            "tracking": journal.tracking,
            "world_map": None,
        }

    collection, manifest = selected
    manifests_dict = {
        name: item.model_dump(mode="json") for name, item in context["manifests"].items()
    }
    flags = session_story_flags(
        context["state"], manifests_dict, await load_world_flags(db, context["campaign"].id)
    )
    projection = project_player_map(
        session_id=session_id,
        collection=collection,
        manifest=manifest,
        facts=facts,
        world_flags=flags,
        current_scene_id=session.current_scene_id,
    )
    entries_by_id = {entry.id: entry for entry in journal.entries if entry.category == "place"}
    journal_ids: dict[str, str] = {}
    for fact in facts:
        if (
            fact.target.kind == "scene"
            and fact.target.collection == collection
            and fact.target.source_sha256 == manifest.source_sha256
            and fact.target.manifest_sha256 == manifest.reviewed_sha256
            and fact.kind in {"learn_place", "visit", "move"}
        ):
            entry_id = journal_entry_id(session_id, "place", fact.fact_id)
            if entry_id in entries_by_id:
                journal_ids[fact.target.canonical_id] = entry_id
    world = await _player_world_map(collection, projection)
    artwork = adventure_assets.authorized_player_assets(
        session_id=session_id,
        collection=collection,
        manifest=manifest,
        facts=facts,
    )
    return {
        **projection.model_dump(mode="json"),
        "journal_entry_ids": journal_ids,
        "known_place_entries": _known_place_entries(journal),
        "artwork": artwork,
        "tracking": journal.tracking,
        "world_map": world.model_dump(mode="json") if world else None,
    }


@router.get("/sessions/{session_id}/map.svg")
async def get_session_map_svg(
    session_id: int,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
    v: str | None = None,
) -> BinaryResponse:
    """Render the already-filtered map projection; this endpoint never loads private SVGs."""
    response.headers["Cache-Control"] = "private, no-store"
    context = await _session_map_context(session_id, db, auth)
    selected = _select_manifest(context)
    if selected is None:
        projection = PlayerMapProjection(
            session_id=session_id,
            source_version=hashlib.sha256(
                f"edgar-map-v1:{session_id}:{context['campaign'].id}".encode()
            ).hexdigest(),
            topology="unavailable",
            current_scene_id=None,
            places=[],
            routes=[],
            note="Reviewed map topology is unavailable for the known places in this session.",
        )
    else:
        collection, manifest = selected
        flags = session_story_flags(
            context["state"],
            {name: item.model_dump(mode="json") for name, item in context["manifests"].items()},
            await load_world_flags(db, context["campaign"].id),
        )
        projection = project_player_map(
            session_id=session_id,
            collection=collection,
            manifest=manifest,
            facts=context["facts"],
            world_flags=flags,
            current_scene_id=context["session"].current_scene_id,
        )
    if v is not None and v != projection.source_version:
        raise HTTPException(status_code=409, detail="Map changed. Refresh this session's map.")
    return BinaryResponse(
        content=render_player_map_svg(projection),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/sessions/{session_id}/world-map.svg")
async def get_session_world_map_svg(
    session_id: int,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> BinaryResponse:
    """Render the prepared regional map with only what this session may see."""
    response.headers["Cache-Control"] = "private, no-store"
    context = await _session_map_context(session_id, db, auth)
    selected = _select_manifest(context)
    if selected is None:
        raise HTTPException(status_code=404, detail="No world map for this session.")
    collection, manifest = selected
    flags = session_story_flags(
        context["state"],
        {name: item.model_dump(mode="json") for name, item in context["manifests"].items()},
        await load_world_flags(db, context["campaign"].id),
    )
    projection = project_player_map(
        session_id=session_id,
        collection=collection,
        manifest=manifest,
        facts=context["facts"],
        world_flags=flags,
        current_scene_id=context["session"].current_scene_id,
    )
    world = await _player_world_map(collection, projection)
    if world is None:
        raise HTTPException(status_code=404, detail="No world map for this session.")
    return BinaryResponse(
        content=render_world_map_svg(world),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/sessions/{session_id}/assets/{asset_id}")
async def get_session_asset(
    session_id: int,
    asset_id: str,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> BinaryResponse:
    """Serve an asset only after this session has committed its scene/passage/encounter."""
    response.headers["Cache-Control"] = "private, no-store"
    context = await _session_map_context(session_id, db, auth)
    for collection, manifest in context["manifests"].items():
        artifact = adventure_assets.get_authorized_player_asset_bytes(
            session_id=session_id,
            collection=collection,
            manifest=manifest,
            facts=context["facts"],
            asset_id=asset_id,
        )
        if artifact is not None:
            record, content = artifact
            return BinaryResponse(
                content=content,
                media_type=record.media_type,
                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
            )
    raise HTTPException(status_code=404, detail="Not found")


__all__ = ["router"]
