"""NPC CRUD router — Phase 8.

All write endpoints are scoped to a campaign; reads can be done by NPC id directly.
"""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import NPC, Campaign, EventLog, Session as SessionModel

from ..dependencies import get_session
from ..schemas.npc import NPCCreate, NPCRead, NPCUpdate
from ..services.journal import project_player_journal
from ..services.journal_identity import journal_entry_id
from ..services.story_state import load_story_state
from ._common import Page, get_or_404

router = APIRouter(tags=["npcs"])


def _read_npc(npc: NPC) -> NPCRead:
    read = NPCRead.model_validate(npc)
    if npc.session_id is None or not npc.story_identity or len(npc.story_identity) != 64:
        return read
    return read.model_copy(
        update={"journal_entry_id": journal_entry_id(npc.session_id, "person", npc.story_identity)}
    )


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
        events = await db.scalars(
            select(EventLog)
            .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
            .order_by(EventLog.created_at.asc(), EventLog.id.asc())
        )
        tracking = project_player_journal(session_id, state, events.all()).tracking
        response.headers["X-NPC-Tracking"] = tracking
    result = await db.execute(
        select(NPC)
        .where(
            NPC.campaign_id == campaign_id,
            or_(NPC.session_id.is_(None), NPC.session_id == session_id),
        )
        # With a selected save, keep its learned rows ahead of campaign notes so pagination
        # cannot turn a populated save into a false empty state when manual notes are numerous.
        .order_by(NPC.session_id.is_(None).asc(), NPC.id.asc())
        .limit(page.limit)
        .offset(page.offset)
    )
    return [_read_npc(n) for n in result.scalars().all()]


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
