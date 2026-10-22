"""Read-only, selected-session player journal endpoints."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from db.postgres.models import Campaign, EventLog
from edgar_core.manifests import load_published_manifest

from ..auth import Auth, member_session
from ..dependencies import get_session
from ..schemas.journal import JournalCategory, JournalEntry, JournalRead
from ..services import adventures_meta
from ..services.journal import (
    JournalPreparedLabels,
    filter_player_journal,
    project_player_journal,
)
from ..services.story_state import load_story_state

router = APIRouter(tags=["journal"])


async def _read_session_journal(session_id: int, db: AsyncSession, auth: Auth) -> JournalRead:
    # Player projection (EDG-046): members read the same allowlisted journal the owner's UI shows.
    session = await member_session(db, session_id, auth)
    state = await load_story_state(db, session_id, session.campaign_id)
    result = await db.execute(
        select(EventLog)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .order_by(EventLog.created_at.asc(), EventLog.id.asc())
    )
    events = result.scalars().all()
    adjudications = await db.execute(
        select(EventLog.id, EventLog.payload["turn_id"], EventLog.payload["accepted_observations"])
        .where(EventLog.session_id == session_id, EventLog.event_type == "adjudication")
        .order_by(EventLog.id.asc())
    )
    # Only the validated earned answers leave an adjudication row; its private basis,
    # proposals and diagnostics are never selected.
    observations = [
        (event_id, turn_id, [text for text in texts if isinstance(text, str)])
        for event_id, turn_id, texts in adjudications.all()
        if isinstance(turn_id, str) and isinstance(texts, list) and texts
    ]
    campaign = await db.get(Campaign, session.campaign_id)
    collections = (
        list(campaign.adventure_collections or [])
        if campaign is not None and campaign.source_mode != "homebrew"
        else []
    )
    prepared = await run_in_threadpool(prepared_journal_labels, collections)
    return project_player_journal(
        session_id,
        state,
        events,
        active_character_id=session.active_character_id,
        current_scene_id=session.current_scene_id,
        prepared=prepared,
        observations=observations,
    )


def prepared_journal_labels(collections: list[str]) -> JournalPreparedLabels:
    """Prepared names for recognition only, bound to each published manifest version.

    Unreadable metadata fails closed: no names to recognise and no page citations.
    """
    titles: dict[tuple[str, str, str], str] = {}
    actor_ids: set[tuple[str, str]] = set()
    paged: set[str] = set()
    scene_places: dict[tuple[str, str], dict] = {}
    for collection in collections:
        try:
            meta = adventures_meta.load_meta(collection)
            manifest = load_published_manifest(meta)
        except Exception:
            continue
        # Assembled (campaign-graph) adventures number synthetic pages; only a directly
        # extracted book has page numbers a player could look up.
        if (
            meta.get("preparation_workflow") is None
            and (meta.get("ingestion") or {}).get("source_kind") == "pdf"
        ):
            paged.add(collection)
        for scene in manifest.scenes:
            titles[(collection, "scene", scene.scene_id)] = scene.title
        from ..services.campaign_prep.runtime import published_scene_places

        scene_places.update({
            (collection, scene_id): place
            for scene_id, place in published_scene_places(collection, manifest).items()
        })
        for entity in manifest.entities:
            titles[(collection, "entity", entity.entity_id)] = entity.title
            if entity.kind == "actor":
                actor_ids.add((collection, entity.entity_id))
        for kind, definitions in (
            ("objective", manifest.objectives),
            ("milestone", manifest.milestones),
        ):
            for definition in definitions:
                titles[(collection, kind, definition.canonical_id)] = definition.title
    return JournalPreparedLabels(
        titles=titles, actor_ids=frozenset(actor_ids), paged_collections=frozenset(paged),
        scene_places=scene_places,
    )


def _no_store(response: Response) -> None:
    # Session knowledge must not survive navigation as a shared/proxy cache entry.
    response.headers["Cache-Control"] = "private, no-store"


@router.get("/sessions/{session_id}/journal", response_model=JournalRead)
async def get_session_journal(
    session_id: int,
    auth: Auth,
    response: Response,
    category: JournalCategory | None = None,
    q: str | None = Query(default=None, max_length=160),
    db: AsyncSession = Depends(get_session),
) -> JournalRead:
    """Project only this session's earned facts before applying player-facing filters."""
    _no_store(response)
    journal = await _read_session_journal(session_id, db, auth)
    return filter_player_journal(journal, category=category, query=q)


@router.get("/sessions/{session_id}/journal/export")
async def export_session_journal(
    session_id: int,
    auth: Auth,
    response: Response,
    category: JournalCategory | None = None,
    q: str | None = Query(default=None, max_length=160),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Download only the same safe entries available in the selected-session list."""
    _no_store(response)
    journal = filter_player_journal(
        await _read_session_journal(session_id, db, auth), category=category, query=q
    )
    data = journal.model_dump(mode="json")
    filename = f"journal-session-{session_id}.json"
    return JSONResponse(
        content=json.loads(json.dumps(data, ensure_ascii=False)),
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/sessions/{session_id}/journal/{entry_id}", response_model=JournalEntry)
async def get_session_journal_entry(
    session_id: int,
    entry_id: str,
    auth: Auth,
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> JournalEntry:
    """Resolve a detail only against the same safe session projection; unknown IDs disclose nothing."""
    _no_store(response)
    journal = await _read_session_journal(session_id, db, auth)
    entry = next((item for item in journal.entries if item.id == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return entry
