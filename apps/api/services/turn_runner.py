"""Turn orchestration shared by the sync and SSE turn endpoints.

Both endpoints run the same agent topology (`agent.graph`) and persist through `persist_turn`,
which writes everything a turn produces — combat state, adjudication world writes, telemetry
rows, the narration row and campaign completion — in ONE transaction on a short-lived DB
session, so a failure anywhere leaves no partial writes and no connection is held across LLM
calls.

The SSE runner drives `prepare_app` (the graph minus the linear-path narrator) so the input is
parsed exactly once, then streams the narrator with `llm.astream`. The combat subgraph narrates
the round itself, so on that path we synthesise word-level tokens from its narration to keep
the SSE protocol uniform.

Event types (see apps/api/README.md for the public contract):
  status: {"stage": "parsing" | "retrieving" | "adjudicating" | "narrating" | "saving"}
  token: {"text": "<chunk>"}                        # concatenated they form the narration
  adjudication: <AdjudicationResult dict>           # emitted once after adjudication
  debug: {...}                                      # only when the request asks AND EDGAR_DEBUG=1
  done: {"narration", "character", "current_scene_id", "combat_state", "campaign_complete"}
  error: {"code", "message", "detail"}              # stable code + generic text; never raw errors

Raw exception text is only ever logged server-side; clients get a stable `code` and a generic
`message` (`detail` repeats the message for older clients).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import structlog
from sqlalchemy import select

import db.postgres.session as db_session_module
from agent.graph import app, prepare_app, route_after_input_parser
from agent.nodes.narrator import build_narrator_prompt, make_narrator_model
from db.postgres.models import EventLog, Session as SessionModel
from edgar_core.config import LLM_MODEL

from .campaign_completion import maybe_complete_campaign
from .character_assignments import character_play_state
from .combat import persist_combat
from .world_writes import MissingCharacterAssignmentError, apply_adjudication

log = structlog.get_logger()


# code -> (HTTP status for the sync endpoint, message shown to the player)
PUBLIC_ERRORS: dict[str, tuple[int, str]] = {
    "llm_unavailable": (503, "The Dungeon Master is unavailable right now. Try again."),
    "character_not_assigned": (
        409,
        "The active character is not assigned to this campaign. Pick a character for the session.",
    ),
    "turn_save_failed": (500, "The turn could not be saved. Try again."),
    "turn_failed": (500, "The turn failed due to an internal error. Try again."),
}


class TurnError(Exception):
    """A turn failure with a stable public code. The underlying cause is logged, never shown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code, self.message = PUBLIC_ERRORS[code]

    def body(self) -> dict[str, str]:
        return error_body(self.code)


def error_body(code: str) -> dict[str, str]:
    """Public error payload. `detail` mirrors `message` (the frontend and FastAPI clients read it)."""
    _, message = PUBLIC_ERRORS[code]
    return {"code": code, "message": message, "detail": message}


def sse_event(event: str, data: Any) -> str:
    """Format one Server-Sent Events frame. Two newlines terminate a frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _chunk_text(chunk: Any) -> str:
    """Extract text from a langchain stream chunk.

    Some providers return list-of-blocks (`[{"type": "text", "text": "..."}]`) instead of a
    plain string; normalise to a single str.
    """
    raw = chunk.content if hasattr(chunk, "content") else str(chunk)
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return str(raw)


async def persist_turn(session_id: int, body_message: str, result: dict[str, Any]) -> dict[str, Any]:
    """Write the whole turn in one transaction and return the `done` payload.

    Opens its own short-lived session so the caller never holds a pooled connection across LLM
    calls. The character is read *before* campaign completion closes the assignments, so the
    final turn of a campaign still reports the hero's state. Raises TurnError (after rollback).
    """
    new_combat = result.get("combat_state")
    adjudication = result.get("adjudication_result")
    narration = result.get("narration", "") or ""
    async with db_session_module.async_session_factory() as db:
        try:
            game_session = (
                await db.execute(select(SessionModel).where(SessionModel.id == session_id))
            ).scalar_one()

            if new_combat:
                await persist_combat(db, session_id, new_combat)

            if adjudication:
                await apply_adjudication(db, game_session, adjudication)

            for event in result.get("telemetry_events") or []:
                db.add(
                    EventLog(
                        session_id=session_id,
                        event_type=event["event_type"],
                        payload=event["payload"],
                    )
                )
            db.add(
                EventLog(
                    session_id=session_id,
                    event_type="narration",
                    payload={"player_input": body_message, "narration": narration},
                )
            )

            character_out = await character_play_state(
                db, game_session.active_character_id, game_session.campaign_id
            )
            campaign_complete = await maybe_complete_campaign(
                db, game_session.campaign_id, adjudication
            )
            current_scene_id = game_session.current_scene_id
            await db.commit()
        except MissingCharacterAssignmentError as e:
            await db.rollback()
            log.warning("turn_persist_missing_assignment", session_id=session_id, error=str(e))
            raise TurnError("character_not_assigned") from e
        except Exception as e:
            await db.rollback()
            log.exception("turn_persist_failed", session_id=session_id)
            raise TurnError("turn_save_failed") from e

    return {
        "narration": narration,
        "character": character_out,
        "current_scene_id": current_scene_id,
        "combat_state": combat_state_public(new_combat),
        "campaign_complete": campaign_complete,
    }


async def run_turn(initial_state: dict[str, Any]) -> dict[str, Any]:
    """Run the full graph (sync endpoint). Node errors become TurnError("llm_unavailable")."""
    result = await app.ainvoke(dict(initial_state))
    if result.get("error"):
        log.error("turn_agent_error", error=result["error"])
        raise TurnError("llm_unavailable")
    return result


def combat_state_public(combat_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim the persisted combat dict to what the client HUD needs.

    Roster HP, round and outcome, plus the initiative view (order, AC, whose turn).
    """
    if not combat_state:
        return None
    return {
        "round": combat_state.get("round"),
        "ended": combat_state.get("ended", False),
        "outcome": combat_state.get("outcome"),
        "combatants": [
            {
                "name": c.get("display_name") or c.get("name"),
                "is_player": c.get("is_player", False),
                "hp_current": c.get("hp_current"),
                "hp_max": c.get("hp_max"),
                "ac": c.get("ac"),
                "alive": c.get("alive", True),
            }
            for c in (combat_state.get("combatants") or [])
        ],
        "initiative": combat_state.get("initiative") or [],
        "current_turn": combat_state.get("current_turn"),
        "current_turn_index": combat_state.get("current_turn_index"),
        "player_ac": combat_state.get("player_ac"),
        "second_wind_used": combat_state.get("second_wind_used", False),
    }


def _truncate_ctx(items: Any, n: int = 8, width: int = 320) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in (items or [])[:n]:
        out.append(
            {
                "text": (c.get("text", "") or "")[:width],
                "source": c.get("source"),
                "page": c.get("page"),
                "collection": c.get("collection"),
                "score": c.get("score"),
            }
        )
    return out


def _debug_payload(
    *,
    parsed: Any,
    rules_ctx: Any,
    adv_ctx: Any,
    world_flags_in: dict | None,
    combat_state: dict | None,
    timings: dict[str, int],
) -> dict[str, Any]:
    """Everything a developer needs to confirm a turn ran correctly: intent classification,
    the RAG chunks actually retrieved (the silently-dead-RAG class of bug), full combat
    internals (enemy AC/attack/damage/HP, initiative, second wind), stage timings, and model."""
    return {
        "parsed_input": parsed.model_dump() if parsed is not None else None,
        "rules_context": _truncate_ctx(rules_ctx),
        "adventure_context": _truncate_ctx(adv_ctx),
        "rules_context_count": len(rules_ctx or []),
        "adventure_context_count": len(adv_ctx or []),
        "world_flags_in": world_flags_in or {},
        "combat_state": combat_state,  # full, untrimmed (enemy stats included)
        "timings_ms": timings,
        "model": LLM_MODEL,
    }


def _yield_narration_tokens(narration: str) -> list[str]:
    """Split a complete narration into word-sized chunks (combat path's pseudo-stream)."""
    if not narration:
        return []
    parts = re.findall(r"\S+\s*", narration)
    return parts if parts else [narration]


# Pipeline node -> the timing bucket its duration is charged to.
_TIMING_BUCKET = {
    "input_parser": "parsing_ms",
    "world_retriever": "retrieving_ms",
    "rules_adjudicator": "adjudicating_ms",
    "world_state_updater": "adjudicating_ms",
    "combat": "combat_ms",
}


async def stream_session_turn(
    *,
    session_id: int,
    body_message: str,
    initial_state: dict[str, Any],
    debug: bool = False,
) -> AsyncIterator[str]:
    t0 = time.monotonic()
    lap_start = t0
    timings: dict[str, int] = {}

    def lap(bucket: str) -> None:
        """Charge the time since the previous lap to `bucket` (real wall-clock stage timings)."""
        nonlocal lap_start
        now = time.monotonic()
        timings[bucket] = timings.get(bucket, 0) + int((now - lap_start) * 1000)
        lap_start = now

    yield sse_event("status", {"stage": "parsing"})

    # Parse once, route, and run everything up to (not including) the linear-path narrator.
    state: dict[str, Any] = dict(initial_state)
    is_combat = False
    try:
        async for mode, chunk in prepare_app.astream(
            dict(initial_state), stream_mode=["updates", "values"]
        ):
            if mode == "values":
                state = dict(chunk)
                continue
            for node, update in chunk.items():
                lap(_TIMING_BUCKET.get(node, f"{node}_ms"))
                if (update or {}).get("error"):
                    continue
                if node == "input_parser":
                    route = route_after_input_parser({**state, **(update or {})})
                    is_combat = route == "combat"
                    yield sse_event("status", {"stage": "retrieving"})
                    if is_combat:
                        yield sse_event("status", {"stage": "adjudicating"})
                elif node == "world_retriever":
                    yield sse_event("status", {"stage": "adjudicating"})
    except Exception:
        log.exception("turn_agent_crashed", session_id=session_id)
        yield sse_event("error", error_body("turn_failed"))
        return

    if state.get("error"):
        log.error("turn_agent_error", session_id=session_id, error=state["error"])
        yield sse_event("error", error_body("llm_unavailable"))
        return

    adjudication = state.get("adjudication_result")
    if adjudication is not None:
        yield sse_event("adjudication", adjudication.model_dump())

    yield sse_event("status", {"stage": "narrating"})
    if is_combat:
        # The combat subgraph already narrated the round; pseudo-stream it word by word.
        narration = state.get("narration") or ""
        for piece in _yield_narration_tokens(narration):
            yield sse_event("token", {"text": piece})
    else:
        prompt = build_narrator_prompt(state)
        llm = make_narrator_model()
        narration_parts: list[str] = []
        try:
            async for chunk in llm.astream(prompt):
                tok = _chunk_text(chunk)
                if not tok:
                    continue
                narration_parts.append(tok)
                yield sse_event("token", {"text": tok})
        except Exception:
            log.exception("turn_narrator_failed", session_id=session_id)
            yield sse_event("error", error_body("llm_unavailable"))
            return
        narration = "".join(narration_parts)
        lap("narrating_ms")
    state["narration"] = narration

    yield sse_event("status", {"stage": "saving"})
    try:
        done_payload = await persist_turn(session_id, body_message, state)
    except TurnError as e:
        yield sse_event("error", e.body())
        return
    except Exception:
        # persist_turn maps its own failures to TurnError; this only catches e.g. a failed
        # rollback or connection checkout, which must still reach the client as an event.
        log.exception("turn_persist_failed", session_id=session_id)
        yield sse_event("error", error_body("turn_save_failed"))
        return
    lap("saving_ms")

    if debug:
        yield sse_event(
            "debug",
            _debug_payload(
                parsed=state.get("parsed_input"),
                rules_ctx=state.get("rules_context"),
                adv_ctx=state.get("adventure_context"),
                world_flags_in=initial_state.get("world_flags"),
                combat_state=state.get("combat_state"),
                timings={**timings, "total_ms": int((time.monotonic() - t0) * 1000)},
            ),
        )
    yield sse_event("done", done_payload)
