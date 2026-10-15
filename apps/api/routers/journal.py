"""Read-only, selected-session player journal endpoints."""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import EventLog, Session as SessionModel

from ..dependencies import get_session
from ..schemas.journal import JournalCategory, JournalEntry, JournalRead
from ..services.journal import filter_player_journal, project_player_journal
from ..services.story_state import load_story_state
from ._common import get_or_404

router = APIRouter(tags=["journal"])


async def _read_session_journal(session_id: int, db: AsyncSession) -> JournalRead:
    session = await get_or_404(db, SessionModel, session_id, "Session")
    state = await load_story_state(db, session_id, session.campaign_id)
    result = await db.execute(
        select(EventLog)
        .where(EventLog.session_id == session_id, EventLog.event_type == "narration")
        .order_by(EventLog.created_at.asc(), EventLog.id.asc())
    )
    events = result.scalars().all()
    return project_player_journal(session_id, state, events)


def _no_store(response: Response) -> None:
    # Session knowledge must not survive navigation as a shared/proxy cache entry.
    response.headers["Cache-Control"] = "private, no-store"


@router.get("/sessions/{session_id}/journal", response_model=JournalRead)
async def get_session_journal(
    session_id: int,
    response: Response,
    category: JournalCategory | None = None,
    q: str | None = Query(default=None, max_length=160),
    db: AsyncSession = Depends(get_session),
) -> JournalRead:
    """Project only this session's earned facts before applying player-facing filters."""
    _no_store(response)
    journal = await _read_session_journal(session_id, db)
    return filter_player_journal(journal, category=category, query=q)


@router.get("/sessions/{session_id}/journal/export")
async def export_session_journal(
    session_id: int,
    response: Response,
    category: JournalCategory | None = None,
    q: str | None = Query(default=None, max_length=160),
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Download only the same safe entries available in the selected-session list."""
    _no_store(response)
    journal = filter_player_journal(
        await _read_session_journal(session_id, db), category=category, query=q
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
    response: Response,
    db: AsyncSession = Depends(get_session),
) -> JournalEntry:
    """Resolve a detail only against the same safe session projection; unknown IDs disclose nothing."""
    _no_store(response)
    journal = await _read_session_journal(session_id, db)
    entry = next((item for item in journal.entries if item.id == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Not found")
    return entry
