"""The party seated at a session's table (EDG-046).

The party is the spotlight holder (``sessions.active_character_id``) followed by every other
character seated through ``session_participants``, in seat order. Each entry is the same play-state
projection the agent already receives for the acting character, so the agent reads one shape for
both. A character without an active assignment in the campaign is left out, like any other
unassigned character.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Session as SessionModel, SessionParticipant

from .character_assignments import character_play_state


async def seated_character_ids(db: AsyncSession, session_id: int) -> list[int]:
    result = await db.execute(
        select(SessionParticipant.character_id)
        .where(SessionParticipant.session_id == session_id)
        .order_by(SessionParticipant.id)
    )
    return list(result.scalars().all())


async def session_party(db: AsyncSession, session: SessionModel) -> list[dict[str, Any]]:
    """Play state for every character at the table, the acting character first."""
    ordered: list[int] = []
    if session.active_character_id is not None:
        ordered.append(session.active_character_id)
    for character_id in await seated_character_ids(db, session.id):
        if character_id not in ordered:
            ordered.append(character_id)
    party: list[dict[str, Any]] = []
    for character_id in ordered:
        state = await character_play_state(db, character_id, session.campaign_id)
        if state is not None:
            party.append(state)
    return party
