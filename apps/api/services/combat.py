"""Load the active combat row for a session and persist updates after a turn.

Convention: agent-state `combat_state` carries the DB row id when it has been persisted.
Derived/HUD keys (`initiative`, `current_turn`, `player_ac`, `second_wind_used`) are rebuilt
from `combatants` on load (each combatant carries its `init`; the player carries `ac` and
`second_wind_used`), so no extra columns are needed.
`persist_combat` uses that id to UPDATE the existing row; missing id means a new encounter
and we INSERT. The agent's combat subgraph spreads the existing dict on every update so the
id flows through unchanged.
"""

import copy

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
    combatants = cs.combatants or []
    player = next((c for c in combatants if c.get("is_player")), None) or {}
    out = {
        "id": cs.id,
        "initiative_order": cs.initiative_order,
        "round": cs.round,
        "current_turn_index": cs.current_turn_index,
        "ended": cs.ended,
        "combatants": combatants,
        "outcome": cs.outcome,
        # Per-encounter facts live on the player combatant (the table has no columns for them).
        "player_ac": player.get("ac"),
        "second_wind_used": bool(player.get("second_wind_used", False)),
    }
    if all("init" in c for c in combatants) and combatants:
        from agent.graph_combat import initiative_view

        out["initiative"] = initiative_view(combatants)
        out["current_turn"] = (
            None if cs.ended else (player.get("display_name") or player.get("name"))
        )
    return out


async def load_resolved_combat_outcomes(db: AsyncSession, session_id: int) -> list[dict]:
    """Return this session's durable engine outcomes, including actual encounter identities.

    Ended CombatState rows remain authoritative; no synthetic StoryRef or duplicate outcome
    ledger is created. The planner may use retained private source_reference identities;
    player serialization must use resolved_combat_outcomes_public instead.
    """
    rows = await db.scalars(
        select(CombatState)
        .where(CombatState.session_id == session_id, CombatState.ended.is_(True))
        .order_by(CombatState.id)
    )
    fields = {"name", "display_name", "creature_id", "is_player", "alive", "source_reference"}
    return [
        {
            "id": row.id,
            "outcome": row.outcome,
            "round": row.round,
            "combatants": [
                {key: copy.deepcopy(value) for key, value in combatant.items() if key in fields}
                for combatant in row.combatants or []
            ],
        }
        for row in rows
    ]


def resolved_combat_outcomes_public(outcomes: list[dict]) -> list[dict]:
    """Known engine results and encountered roster labels; exclude private source bindings."""
    return [
        {
            "id": outcome["id"],
            "outcome": outcome["outcome"],
            "round": outcome["round"],
            "combatants": [
                {
                    key: copy.deepcopy(combatant[key])
                    for key in ("name", "display_name", "is_player", "alive")
                    if key in combatant
                }
                for combatant in outcome.get("combatants", [])
            ],
        }
        for outcome in outcomes
    ]


async def persist_combat(db: AsyncSession, session_id: int, combat_state: dict | None) -> None:
    if not combat_state:
        return
    if combat_state.get("id"):
        result = await db.execute(
            select(CombatState).where(
                CombatState.id == combat_state["id"],
                CombatState.session_id == session_id,
            ).with_for_update()
        )
        cs = result.scalar_one()
        if cs.ended:
            fields = ("initiative_order", "round", "current_turn_index", "ended", "combatants", "outcome")
            if any(combat_state.get(key, getattr(cs, key)) != getattr(cs, key) for key in fields):
                raise ValueError("A resolved combat outcome cannot be rewritten")
            return
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
