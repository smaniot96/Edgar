"""Developer reload of captured turn traces (EDG-138).

Reads the `turn_trace` rows of a session and composes each with the EDG-053 `turn_diagnostics` and
EDG-031 `combat_round` rows that share its `turn_id`. The companions are matched at read time, so
they are never copied into the trace. Callers gate access (`EDGAR_DEBUG`); this module only shapes.
A DM turn with no trace row was not captured and is never reported as an empty trace.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.combat_telemetry import COMBAT_ROUND_EVENT, combat_round_public
from agent.turn_diagnostics import TURN_DIAGNOSTICS_EVENT, turn_diagnostics_public
from agent.turn_trace import TURN_TRACE_EVENT, turn_trace_public
from db.postgres.models import EventLog


async def load_turn_traces(db: AsyncSession, session_id: int, limit: int) -> dict[str, Any]:
    """The newest `limit` traces of the session, oldest first, each with its composed companions."""
    rows = list(
        (
            await db.execute(
                select(EventLog)
                .where(EventLog.session_id == session_id, EventLog.event_type == TURN_TRACE_EVENT)
                .order_by(EventLog.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    turn_ids = {str(row.payload.get("turn_id")) for row in rows if row.payload.get("turn_id")}

    companions: dict[tuple[str, str], dict] = {}
    if turn_ids:
        found = (
            (
                await db.execute(
                    select(EventLog)
                    .where(
                        EventLog.session_id == session_id,
                        EventLog.event_type.in_([TURN_DIAGNOSTICS_EVENT, COMBAT_ROUND_EVENT]),
                        EventLog.payload["turn_id"].astext.in_(turn_ids),
                    )
                    .order_by(EventLog.id.asc())
                )
            )
            .scalars()
            .all()
        )
        for row in found:
            companions.setdefault((row.event_type, str(row.payload.get("turn_id"))), row.payload)

    turns = []
    for row in rows:
        turn_id = str(row.payload.get("turn_id"))
        turns.append(
            {
                "turn_id": turn_id,
                "created_at": row.created_at,
                "trace": turn_trace_public(row.payload),
                "turn_diagnostics": turn_diagnostics_public(
                    companions.get((TURN_DIAGNOSTICS_EVENT, turn_id))
                ),
                "combat_round": combat_round_public(companions.get((COMBAT_ROUND_EVENT, turn_id))),
            }
        )
    return {"scope": "dm_turns", "truncated": len(rows) == limit, "turns": turns}
