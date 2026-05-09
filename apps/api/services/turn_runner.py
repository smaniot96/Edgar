"""SSE streaming for session turns (Option A: stream narrator only on the non-combat path)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from agent.graph import app
from agent.llm import make_chat_model
from agent.nodes.input_parser import input_parser_node
from agent.nodes.memory_summarizer import memory_summarizer_node
from agent.nodes.narrator import build_narrator_prompt
from agent.nodes.rules_adjudicator import rules_adjudicator_node
from agent.nodes.world_retriever import world_retriever_node
from agent.nodes.world_state_updater import world_state_updater_node
from db.postgres.models import Character, EventLog, Session as SessionModel
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .combat import persist_combat
from .world_writes import apply_adjudication


def sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _chunk_text(chunk: Any) -> str:
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


async def _character_dict(db: AsyncSession, character_id: int | None) -> dict[str, Any] | None:
    if not character_id:
        return None
    crefresh = await db.execute(select(Character).where(Character.id == character_id))
    char_row = crefresh.scalar_one_or_none()
    if char_row is None:
        return None
    return {
        "id": char_row.id,
        "name": char_row.name,
        "class": char_row.character_class,
        "level": char_row.level,
        "hp_current": char_row.hp_current,
        "hp_max": char_row.hp_max,
        "stats": char_row.stats,
        "inventory": char_row.inventory,
    }


async def _persist_turn(
    db: AsyncSession,
    session_id: int,
    game_session: SessionModel,
    body_message: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Commit narration, adjudication side effects, combat. Returns payload for `done` event."""
    new_combat = result.get("combat_state")
    if new_combat:
        await persist_combat(db, session_id, new_combat)

    adjudication = result.get("adjudication_result")
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
    await db.refresh(game_session)
    character_out = await _character_dict(db, game_session.active_character_id)
    return {
        "narration": result.get("narration", ""),
        "character": character_out,
        "current_scene_id": game_session.current_scene_id,
    }


def _yield_narration_tokens(narration: str) -> list[str]:
    """Split narration into SSE token chunks (words + trailing whitespace)."""
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
) -> AsyncIterator[str]:
    yield sse_event("status", {"stage": "parsing"})
    state: dict[str, Any] = dict(initial_state)

    parsed_updates = await input_parser_node(state)
    state.update(parsed_updates)
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    parsed = state.get("parsed_input")
    is_combat = bool(parsed and parsed.intent == "combat")

    if is_combat:
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
        narration = result.get("narration") or ""
        for piece in _yield_narration_tokens(narration):
            yield sse_event("token", {"text": piece})
        yield sse_event("status", {"stage": "saving"})
        done_payload = await _persist_turn(db, session_id, game_session, body_message, result)
        yield sse_event("done", done_payload)
        return

    yield sse_event("status", {"stage": "retrieving"})
    retrieved = await world_retriever_node(state)
    state.update(retrieved)
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    yield sse_event("status", {"stage": "adjudicating"})
    adj_updates = await rules_adjudicator_node(state)
    state.update(adj_updates)
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    ws_updates = await world_state_updater_node(state)
    state.update(ws_updates)
    if state.get("error"):
        yield sse_event("error", {"detail": state["error"]})
        return

    adjudication = state.get("adjudication_result")
    if adjudication is not None:
        yield sse_event("adjudication", adjudication.model_dump())

    yield sse_event("status", {"stage": "narrating"})
    prompt = build_narrator_prompt(state)
    llm = make_chat_model(temperature=0.7)
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

    narration = "".join(narration_parts)
    messages = list(state.get("messages", []))
    messages.append(AIMessage(content=narration))
    state["narration"] = narration
    state["messages"] = messages
    sum_updates = await memory_summarizer_node(state)
    state.update(sum_updates)

    yield sse_event("status", {"stage": "saving"})
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
    yield sse_event("done", done_payload)
