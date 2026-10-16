"""NPC CRUD router — Phase 8.

All write endpoints are scoped to a campaign; reads can be done by NPC id directly.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import NPC, Campaign, EventLog, Session as SessionModel
from edgar_core.story import StoryFact

from ..dependencies import get_session
from ..schemas.journal import JournalRead
from ..schemas.npc import NPCCreate, NPCRead, NPCUpdate
from ..services.journal import project_player_journal
from ..services.journal_identity import actor_entry_id
from ..services.story_state import load_story_state
from ._common import Page, get_or_404

router = APIRouter(tags=["npcs"])


def _read_npc(
    npc: NPC,
    actor_entries: dict[str, tuple[str, str]] | None = None,
) -> NPCRead:
    read = NPCRead.model_validate(npc)
    if npc.session_id is None or not npc.story_identity or not actor_entries:
        return read
    actor = actor_entries.get(npc.story_identity)
    if actor is None:
        return read
    entry_id, earned_title = actor
    return read.model_copy(update={"name": earned_title, "journal_entry_id": entry_id})


def _session_actor_entries(
    session_id: int, state: dict, journal: JournalRead
) -> dict[str, tuple[str, str]]:
    """Link stored NPC rows to the already-safe journal projection for this session."""
    facts = [StoryFact.model_validate(raw) for raw in state.get("facts", [])]
    people = {entry.id: entry.title for entry in journal.entries if entry.category == "person"}
    entries: dict[str, tuple[str, str]] = {}
    for fact in facts:
        if fact.kind not in {"encounter", "relationship"} or fact.target.kind != "entity":
            continue
        entry_id = actor_entry_id(session_id, fact.target)
        if entry_id in people:
            entries[fact.fact_id] = (entry_id, people[entry_id])
    return entries


def _project_session_npcs(
    rows: list[NPC], actor_entries: dict[str, tuple[str, str]]
) -> list[NPCRead]:
    latest_actor_rows: dict[str, NPC] = {}
    for npc in rows:
        if npc.story_identity and npc.story_identity in actor_entries:
            # Several stored fact revisions can resolve to the same canonical actor. Keep
            # the latest row so a later relationship disposition is not lost on dedup.
            latest_actor_rows[actor_entries[npc.story_identity][0]] = npc
    projected: list[NPCRead] = []
    for npc in rows:
        identity = npc.story_identity or ""
        actor = actor_entries.get(identity)
        if actor is not None:
            if latest_actor_rows.get(actor[0]) is not npc:
                continue
        elif identity and not identity.startswith("combat:"):
            # A stored story-person label is not a public identifier. Only reveal it when
            # its exact actor has a matching player-safe journal entry.
            continue
        projected.append(_read_npc(npc, actor_entries))
    return projected


@router.get("/campaigns/{campaign_id}/npcs", response_model=list[NPCRead])
async def list_npcs(
    campaign_id: int,
    response: Response,
    db: AsyncSession = Depends(get_session),
    page: Page = Depends(),
    session_id: int | None = None,
):
    await get_or_404(db, Campaign, campaign_id, "Campaign")
    if session_id is not None:
        session = await get_or_404(db, SessionModel, session_id, "Session")
        if session.campaign_id != campaign_id:
            raise HTTPException(status_code=404, detail="Session not found for campaign")
        # Use the same player-safe projection as the journal: a private or malformed ledger
        # row must not change whether the UI believes this save has tracked people.
        state = await load_story_state(db, session_id, campaign_id)
        narration_events = await db.scalars(
            select(EventLog)
            .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
            .order_by(EventLog.created_at.asc(), EventLog.id.asc())
        )
        journal = project_player_journal(
            session_id,
            state,
            narration_events,
        )
        response.headers["X-NPC-Tracking"] = journal.tracking
        actor_entries = _session_actor_entries(session_id, state, journal)
    if session_id is None:
        result = await db.execute(
            select(NPC)
            .where(NPC.campaign_id == campaign_id, NPC.session_id.is_(None))
            .order_by(NPC.id.asc())
            .limit(page.limit)
            .offset(page.offset)
        )
        return [_read_npc(npc) for npc in result.scalars().all()]

    # Deduplicate all selected-session facts before pagination. Otherwise many historical
    # source revisions of one actor can crowd the current row or later people off the page.
    session_result = await db.scalars(
        select(NPC)
        .where(NPC.campaign_id == campaign_id, NPC.session_id == session_id)
        .order_by(NPC.id.asc())
    )
    projected = _project_session_npcs(list(session_result.all()), actor_entries)
    start = page.offset
    stop = start + page.limit
    page_rows = projected[start:stop]
    if len(page_rows) < page.limit:
        manual_offset = max(0, start - len(projected))
        manual_result = await db.scalars(
            select(NPC)
            .where(NPC.campaign_id == campaign_id, NPC.session_id.is_(None))
            .order_by(NPC.id.asc())
            .limit(page.limit - len(page_rows))
            .offset(manual_offset)
        )
        page_rows.extend(_read_npc(npc) for npc in manual_result.all())
    return page_rows


@router.post("/campaigns/{campaign_id}/npcs", response_model=NPCRead, status_code=201)
async def create_npc(campaign_id: int, body: NPCCreate, db: AsyncSession = Depends(get_session)):
    await get_or_404(db, Campaign, campaign_id, "Campaign")
    npc = NPC(
        campaign_id=campaign_id,
        name=body.name,
        disposition=body.disposition,
        stat_block=body.stat_block,
    )
    db.add(npc)
    await db.commit()
    await db.refresh(npc)
    return _read_npc(npc)


@router.get("/npcs/{npc_id}", response_model=NPCRead)
async def get_npc(
    npc_id: int, db: AsyncSession = Depends(get_session), session_id: int | None = None
):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    if npc.session_id is not None and npc.session_id != session_id:
        raise HTTPException(status_code=404, detail="NPC not found for session")
    if npc.session_id is not None and session_id is not None:
        session = await get_or_404(db, SessionModel, session_id, "Session")
        if session.campaign_id != npc.campaign_id:
            raise HTTPException(status_code=404, detail="NPC not found for session")
        state = await load_story_state(db, session_id, session.campaign_id)
        narration_events = await db.scalars(
            select(EventLog)
            .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
            .order_by(EventLog.created_at.asc(), EventLog.id.asc())
        )
        journal = project_player_journal(session_id, state, narration_events)
        actor_entries = _session_actor_entries(session_id, state, journal)
        identity = npc.story_identity or ""
        if identity and identity not in actor_entries and not identity.startswith("combat:"):
            raise HTTPException(status_code=404, detail="NPC not found for session")
        return _read_npc(npc, actor_entries)
    return _read_npc(npc)


@router.patch("/npcs/{npc_id}", response_model=NPCRead)
async def update_npc(npc_id: int, body: NPCUpdate, db: AsyncSession = Depends(get_session)):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    if npc.session_id is not None:
        raise HTTPException(
            status_code=409, detail="Encountered NPC state is managed by story effects"
        )
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(npc, key, value)
    await db.commit()
    await db.refresh(npc)
    return NPCRead.model_validate(npc)


@router.delete("/npcs/{npc_id}", status_code=204)
async def delete_npc(npc_id: int, db: AsyncSession = Depends(get_session)):
    npc = await get_or_404(db, NPC, npc_id, "NPC")
    if npc.session_id is not None:
        raise HTTPException(
            status_code=409, detail="Encountered NPC state is managed by story effects"
        )
    await db.delete(npc)
    await db.commit()
