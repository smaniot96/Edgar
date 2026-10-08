"""Stage a telemetry row for the adjudication result.

Source-of-truth writes (HP delta, world flags, scene changes, inventory) live in
`apps.api.services.world_writes.apply_adjudication`. This node does not touch the database:
it returns the telemetry row as `telemetry_events`, and the API writes it in the same
transaction as the narration row and world writes, so a failed turn leaves no stray telemetry.
"""

from agent.state import AgentState


async def world_state_updater_node(state: AgentState) -> dict:
    adjudication = state.get("adjudication_result")
    if not state.get("session_id") or not adjudication:
        return {}
    events = list(state.get("telemetry_events") or [])
    events.append({"event_type": "adjudication", "payload": adjudication.model_dump()})
    return {"telemetry_events": events}
