"""Load the active combat row for a session and persist updates after a turn.

Convention: agent-state `combat_state` carries the DB row id when it has been persisted.
`persist_combat` uses that id to UPDATE the existing row; missing id means a new encounter
and we INSERT. The agent's combat subgraph spreads the existing dict on every update so the
id flows through unchanged.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import CombatState


async def load_active_combat(db: AsyncSession, session_id: int) -> dict | None:
    """Latest non-ended row for this session, returned as the dict the agent expects."""
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
        "combatants": cs.combatants or [],
        "outcome": cs.outcome,
    }


async def persist_combat(
    db: AsyncSession, session_id: int, combat_state: dict | None
) -> None:
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
        cs.current_turn_index = combat_state.get("current_turn_index", cs.current_turn_index)
        cs.ended = combat_state.get("ended", cs.ended)
        cs.combatants = combat_state.get("combatants", cs.combatants)
        cs.outcome = combat_state.get("outcome", cs.outcome)
        return
    db.add(
        CombatState(
            session_id=session_id,
            initiative_order=combat_state["initiative_order"],
            round=combat_state["round"],
            current_turn_index=combat_state.get("current_turn_index", 0),
            ended=combat_state.get("ended", False),
            combatants=combat_state.get("combatants", []),
            outcome=combat_state.get("outcome"),
        )
    )
