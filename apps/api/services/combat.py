"""Load and persist combat_state rows for a session."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import CombatState


async def load_active_combat(db: AsyncSession, session_id: int) -> dict | None:
    """Return the latest non-ended combat row as an agent-state dict, or None."""
    result = await db.execute(
        select(CombatState)
        .where(CombatState.session_id == session_id, CombatState.ended.is_(False))
        .order_by(CombatState.created_at.desc())
        .limit(1)
    )
    cs = result.scalar_one_or_none()
    if cs is None:
        return None
    return {
        "id": cs.id,
        "initiative_order": cs.initiative_order,
        "round": cs.round,
        "current_turn_index": cs.current_turn_index,
        "ended": cs.ended,
    }


async def persist_combat(
    db: AsyncSession, session_id: int, combat_state: dict | None
) -> None:
    """Insert or update combat_state from agent graph output."""
    if not combat_state:
        return
    if combat_state.get("id"):
        result = await db.execute(
            select(CombatState).where(
                CombatState.id == combat_state["id"],
                CombatState.session_id == session_id,
            )
        )
        cs = result.scalar_one()
        cs.initiative_order = combat_state.get("initiative_order", cs.initiative_order)
        cs.round = combat_state.get("round", cs.round)
        cs.current_turn_index = combat_state.get(
            "current_turn_index", cs.current_turn_index
        )
        cs.ended = combat_state.get("ended", cs.ended)
        return
    db.add(
        CombatState(
            session_id=session_id,
            initiative_order=combat_state["initiative_order"],
            round=combat_state["round"],
            current_turn_index=combat_state.get("current_turn_index", 0),
            ended=combat_state.get("ended", False),
        )
    )
