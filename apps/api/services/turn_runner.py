"""SSE turn runner.

Streams the narration to the client token-by-token while the rest of the graph runs to
completion in the background. The non-combat path is unrolled here so we can call
`llm.astream` on the narrator step (the graph's compiled narrator uses `ainvoke`); the combat
path runs the full graph and then synthesises word-level tokens from the final narration so
the SSE protocol stays uniform.

Event types (see apps/api/README.md for the public contract):
  status: {"stage": "parsing" | "retrieving" | "adjudicating" | "narrating" | "saving"}
  token: {"text": "<chunk>"}                        # concatenated they form the narration
  adjudication: <AdjudicationResult dict>           # emitted once after adjudication
  done: {"narration", "character", "current_scene_id"}
  error: {"detail": "<message>"}                    # on any node returning an error
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession

from edgar_core.config import LLM_MODEL

from agent.graph import app
from agent.llm import make_chat_model
from agent.nodes.input_parser import input_parser_node
from agent.nodes.memory_summarizer import memory_summarizer_node
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.nodes.world_retriever import world_retriever_node
from agent.nodes.world_state_updater import world_state_updater_node
from db.postgres.models import EventLog, Session as SessionModel

from .campaign_completion import maybe_complete_campaign
from .character_assignments import character_play_state
from .combat import persist_combat
from .world_writes import MissingCharacterAssignmentError, apply_adjudication

_NARRATOR_TEMPERATURE = 0.7


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


async def _character_dict(
    db: AsyncSession, character_id: int | None, campaign_id: int
) -> dict[str, Any] | None:
    return await character_play_state(db, character_id, campaign_id)


async def _persist_turn(
    db: AsyncSession,
    session_id: int,
    game_session: SessionModel,
    body_message: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Apply combat + world writes + narration row in one transaction; return the `done` payload."""
    new_combat = result.get("combat_state")
    adjudication = result.get("adjudication_result")
    try:
        if new_combat:
            await persist_combat(db, session_id, new_combat)

        if adjudication:
            await apply_adjudication(db, game_session, adjudication)

        db.add(
            EventLog(
                session_id=session_id,
                event_type="narration",
                payload={
                    "player_input": body_message,
                    "narration": result.get("narration", ""),
                },
            )
        )
        await db.commit()
    except MissingCharacterAssignmentError:
        await db.rollback()
        raise
    campaign_complete = await maybe_complete_campaign(
        db, game_session.campaign_id, adjudication
    )
    await db.refresh(game_session)
    character_out = await _character_dict(db, game_session.active_character_id, game_session.campaign_id)
    return {
        "narration": result.get("narration", ""),
        "character": character_out,
        "current_scene_id": game_session.current_scene_id,
        "combat_state": _combat_state_public(result.get("combat_state")),
        "campaign_complete": campaign_complete,
    }


def _combat_state_public(combat_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim the persisted combat dict to what the client needs (roster HP, round, outcome)."""
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
                "alive": c.get("alive", True),
            }
            for c in (combat_state.get("combatants") or [])
        ],
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


async def stream_session_turn(
    *,
    session_id: int,
    game_session: SessionModel,
    body_message: str,
    initial_state: dict[str, Any],
    db: AsyncSession,
    debug: bool = False,
) -> AsyncIterator[str]:
    t0 = time.monotonic()
    marks: dict[str, float] = {"start": t0}

    def ms_since(key: str) -> int:
        return int((time.monotonic() - marks.get(key, t0)) * 1000)

    yield sse_event("status", {"stage": "parsing"})
    state: dict[str, Any] = dict(initial_state)

    parsed_updates = await input_parser_node(state)
    state.update(parsed_updates)
    marks["parsed"] = time.monotonic()
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    parsed = state.get("parsed_input")
    active_combat = initial_state.get("combat_state")
    is_combat = bool(
        (active_combat and not active_combat.get("ended"))
        or (parsed and parsed.intent == "combat")
    )

    if is_combat:
        # The combat subgraph drives multiple internal nodes; we cannot easily stream from it.
        # Run the whole graph, then fake a token stream from the completed narration.
        yield sse_event("status", {"stage": "retrieving"})
        yield sse_event("status", {"stage": "adjudicating"})
        result = await app.ainvoke(dict(initial_state))
        if result.get("error"):
            yield sse_event("error", {"detail": result["error"]})
            return
        adj = result.get("adjudication_result")
        if adj is not None:
            yield sse_event("adjudication", adj.model_dump())
        yield sse_event("status", {"stage": "narrating"})
        for piece in _yield_narration_tokens(result.get("narration") or ""):
            yield sse_event("token", {"text": piece})
        if debug:
            yield sse_event(
                "debug",
                _debug_payload(
                    parsed=result.get("parsed_input") or parsed,
                    rules_ctx=result.get("rules_context"),
                    adv_ctx=result.get("adventure_context"),
                    world_flags_in=initial_state.get("world_flags"),
                    combat_state=result.get("combat_state"),
                    timings={"parsing_ms": ms_since("start"), "total_ms": ms_since("start")},
                ),
            )
        yield sse_event("status", {"stage": "saving"})
        try:
            done_payload = await _persist_turn(db, session_id, game_session, body_message, result)
        except MissingCharacterAssignmentError as e:
            yield sse_event("error", {"detail": str(e)})
            return
        yield sse_event("done", done_payload)
        return

    # Non-combat path: drive nodes in order so we can stream the narrator alone.
    yield sse_event("status", {"stage": "retrieving"})
    state.update(await world_retriever_node(state))
    marks["retrieved"] = time.monotonic()
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    yield sse_event("status", {"stage": "adjudicating"})
    state.update(await rules_adjudicator_node(state))
    marks["adjudicated"] = time.monotonic()
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    state.update(await world_state_updater_node(state))
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    adjudication = state.get("adjudication_result")
    if adjudication is not None:
        yield sse_event("adjudication", adjudication.model_dump())

    yield sse_event("status", {"stage": "narrating"})
    prompt = build_narrator_prompt(state)
    llm = make_chat_model(temperature=_NARRATOR_TEMPERATURE)
    narration_parts: list[str] = []
    try:
        async for chunk in llm.astream(prompt):
            tok = _chunk_text(chunk)
            if not tok:
                continue
            narration_parts.append(tok)
            yield sse_event("token", {"text": tok})
    except Exception as e:
        yield sse_event("error", {"detail": str(e)})
        return
    marks["narrated"] = time.monotonic()

    narration = "".join(narration_parts)
    # Mirror what the graph's narrator_node would have written so memory_summarizer sees the
    # same `messages` shape it sees in the non-streamed path.
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=narration))
    state["narration"] = narration
    state["messages"] = messages
    state.update(await memory_summarizer_node(state))

    yield sse_event("status", {"stage": "saving"})
    try:
        done_payload = await _persist_turn(
            db,
            session_id,
            game_session,
            body_message,
            {
                "narration": narration,
                "adjudication_result": adjudication,
                "combat_state": state.get("combat_state"),
            },
        )
    except MissingCharacterAssignmentError as e:
        yield sse_event("error", {"detail": str(e)})
        return
    if debug:
        yield sse_event(
            "debug",
            _debug_payload(
                parsed=parsed,
                rules_ctx=state.get("rules_context"),
                adv_ctx=state.get("adventure_context"),
                world_flags_in=initial_state.get("world_flags"),
                combat_state=state.get("combat_state"),
                timings={
                    "parsing_ms": int((marks.get("parsed", t0) - t0) * 1000),
                    "retrieving_ms": int((marks.get("retrieved", t0) - marks.get("parsed", t0)) * 1000),
                    "adjudicating_ms": int((marks.get("adjudicated", t0) - marks.get("retrieved", t0)) * 1000),
                    "narrating_ms": int((marks.get("narrated", t0) - marks.get("adjudicated", t0)) * 1000),
                    "total_ms": ms_since("start"),
                },
            ),
        )
    yield sse_event("done", done_payload)
