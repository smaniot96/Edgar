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
from agent.combatants import can_act, disposition_of
from agent.graph import app, prepare_app, route_after_input_parser
from agent.llm import message_text
from agent.nodes.narrator import (
    build_narrator_prompt,
    deterministic_narration,
    make_narrator_model,
    narrator_context,
)
from agent.supported_rules import (
    actions_for_state,
    apply_narration_boundary,
    engine_roll_mode,
    executed_spell_for_state,
)
from db.postgres.models import Campaign, CharacterAssignment, EventLog, Session as SessionModel
from edgar_core.config import LLM_MODEL
from edgar_core.sources import citations_for, public_citations

from .campaign_completion import maybe_complete_campaign
from .character_assignments import character_play_state
from .combat import persist_captive_outcome, persist_combat, persist_combat_npcs
from .world_writes import (
    MissingCharacterAssignmentError,
    apply_adjudication,
    apply_story_inventory,
)

log = structlog.get_logger()


# code -> (HTTP status for the sync endpoint, message shown to the player)
PUBLIC_ERRORS: dict[str, tuple[int, str]] = {
    "campaign_sources_unavailable": (
        409,
        "The campaign adventure source is unavailable. Recover it or choose homebrew play.",
    ),
    "adventure_manifest_review_required": (
        409,
        "Review and publish the adventure scene manifest before module play.",
    ),
    "creature_identity_unresolved": (
        409,
        "Use an exact verified creature name, or review the actor's statistics reference. If its source is unavailable, recover it before retrying.",
    ),
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

    def body(self) -> dict[str, Any]:
        return error_body(self.code)


def error_body(code: str) -> dict[str, Any]:
    """Public error payload. `detail` mirrors `message` (the frontend and FastAPI clients read it)."""
    _, message = PUBLIC_ERRORS[code]
    body: dict[str, Any] = {"code": code, "message": message, "detail": message}
    if code == "campaign_sources_unavailable":
        body["retrieval_degraded"] = True
    return body


def sse_event(event: str, data: Any) -> str:
    """Format one Server-Sent Events frame. Two newlines terminate a frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def public_adjudication(adjudication) -> dict | None:
    if adjudication is None:
        return None
    # Proposals and canonical/private evidence are engine inputs, never client DTOs.
    value = adjudication.model_dump(exclude={"story_effects", "accepted_story_effects"})
    value["citations"] = public_citations(value.get("citations", []))
    value["citation_ids"] = [r["reference_id"] for r in value["citations"]]
    return value


async def persist_turn(
    session_id: int, body_message: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Write the whole turn in one transaction and return the `done` payload.

    Opens its own short-lived session so the caller never holds a pooled connection across LLM
    calls. The character is read *before* campaign completion closes the assignments, so the
    final turn of a campaign still reports the hero's state. Raises TurnError (after rollback).
    """
    new_combat = result.get("combat_state")
    adjudication = result.get("adjudication_result")
    narration = result.get("narration", "") or ""
    edition = result.get("rules_edition", "2014")
    rules, adventure = narrator_context(result)
    ruling_refs = public_citations(adjudication.citations) if adjudication else []
    citations = list(
        {
            ref["reference_id"]: ref
            for ref in public_citations(citations_for(rules + adventure, edition)) + ruling_refs
        }.values()
    )
    ruling_citation_ids = [ref["reference_id"] for ref in ruling_refs]
    async with db_session_module.async_session_factory() as db:
        try:
            game_session = (
                await db.execute(
                    select(SessionModel).where(SessionModel.id == session_id).with_for_update()
                )
            ).scalar_one()

            # A retry of the same server turn must not repeat legacy HP, inventory, combat,
            # or completion writes. The receipt and narration commit in this same transaction.
            turn_id = result.get("turn_id")
            if turn_id:
                receipt = (
                    await db.execute(
                        select(EventLog).where(
                            EventLog.session_id == session_id,
                            EventLog.event_type == "narration",
                            EventLog.payload["turn_id"].astext == turn_id,
                        )
                    )
                ).scalar_one_or_none()
                if receipt is not None:
                    if (receipt.payload or {}).get("player_input") != body_message:
                        raise ValueError("Turn identity was reused for a different action")
                    saved = (receipt.payload or {}).get("done")
                    if saved is None:
                        raise RuntimeError("Incomplete turn receipt")
                    return saved

            from starlette.concurrency import run_in_threadpool

            from .adventure_manifests import campaign_manifests
            from .campaign_sources import require_campaign_sources
            from .story_state import session_story_flags
            from .world_flags import load_world_flags

            campaign = (
                await db.execute(
                    select(Campaign)
                    .where(Campaign.id == game_session.campaign_id)
                    .with_for_update()
                )
            ).scalar_one()
            if campaign.status == "ended":
                raise ValueError("Campaign ended during generation")
            if (
                campaign.source_mode != result.get("source_mode", "module")
                or campaign.rules_edition != result.get("rules_edition", "2014")
                or (
                    []
                    if campaign.source_mode == "homebrew"
                    else list(campaign.adventure_collections or [])
                )
                != result.get("adventure_collections", [])
            ):
                raise ValueError("Campaign source binding changed during generation")
            await require_campaign_sources(campaign)
            current_manifests = (
                {}
                if campaign.source_mode == "homebrew"
                else await run_in_threadpool(
                    campaign_manifests, list(campaign.adventure_collections or [])
                )
            )
            if current_manifests != (result.get("adventure_manifests") or {}):
                raise ValueError("Reviewed adventure changed during generation")

            # Other sessions may share an assignment. Serialize its inventory projection too.
            if game_session.active_character_id:
                await db.execute(
                    select(CharacterAssignment)
                    .where(
                        CharacterAssignment.character_id == game_session.active_character_id,
                        CharacterAssignment.campaign_id == game_session.campaign_id,
                        CharacterAssignment.ended_at.is_(None),
                    )
                    .with_for_update()
                )
            snapshot = result.get("turn_snapshot")
            current_character = await character_play_state(
                db, game_session.active_character_id, game_session.campaign_id
            )
            if snapshot and (
                snapshot.get("current_scene_id") != game_session.current_scene_id
                or snapshot.get("active_character_id") != game_session.active_character_id
                or snapshot.get("character") != current_character
            ):
                raise ValueError("Turn state changed during generation")

            from .story_state import (
                load_story_state,
                persist_combat_outcome_facts,
                persist_story_effects,
            )

            expected_story = result.get("story_state") or {"facts": []}
            current_story = await load_story_state(db, session_id, game_session.campaign_id)
            if current_story != expected_story:
                raise ValueError("Story state changed during generation")
            current_flags = session_story_flags(
                current_story,
                current_manifests,
                await load_world_flags(db, game_session.campaign_id),
            )
            if current_flags != (result.get("world_flags") or {}):
                raise ValueError("World state changed during generation")
            effects = adjudication.accepted_story_effects if adjudication else []
            if effects:
                from agent.models.adjudication import AdjudicationResult
                from agent.nodes.story_effects import validate_story_effects
                from edgar_core.story import StoryEffect

                checked = validate_story_effects(
                    AdjudicationResult(
                        success=adjudication.success,
                        story_effects=[
                            StoryEffect.model_validate(
                                effect.model_dump(include=set(StoryEffect.model_fields))
                            )
                            for effect in effects
                        ],
                    ),
                    {**result, "character": current_character, "story_state": current_story},
                )
                if checked.accepted_story_effects != effects:
                    raise ValueError("Story consequences no longer validate")
                # Validate against current committed ownership before any legacy writes.
                await apply_story_inventory(
                    db,
                    game_session,
                    effects,
                    initial_state={
                        **result,
                        "character": current_character,
                        "story_state": current_story,
                    },
                )

            combat_id = None
            if new_combat:
                combat_id = await persist_combat(db, session_id, new_combat)
                await persist_combat_npcs(
                    db, session_id, game_session.campaign_id, combat_id, new_combat
                )

            if adjudication:
                discovery_keys = {
                    entity["discovery_flag"]
                    for manifest in (result.get("adventure_manifests") or {}).values()
                    for entity in manifest.get("entities", [])
                    if entity.get("discovery_flag")
                }
                if discovery_keys:
                    await apply_adjudication(
                        db, game_session, adjudication, session_discovery_keys=discovery_keys
                    )
                else:
                    await apply_adjudication(db, game_session, adjudication)

            for event in result.get("telemetry_events") or []:
                db.add(
                    EventLog(
                        session_id=session_id,
                        event_type=event["event_type"],
                        payload=event["payload"],
                    )
                )
            narration_event = EventLog(
                session_id=session_id,
                event_type="narration",
                payload={
                    "player_input": body_message,
                    "narration": narration,
                    "citations": citations,
                    "ruling_citation_ids": ruling_citation_ids,
                    "rules_edition": edition,
                    "turn_id": turn_id,
                },
            )
            db.add(narration_event)
            await db.flush()
            if effects:
                await persist_story_effects(
                    db,
                    game_session,
                    effects,
                    event_id=narration_event.id,
                    expected_state=expected_story,
                )
            # EDG-146: engine outcomes join the same ledger and NPC rows in this transaction.
            ledger_turn = turn_id or f"engine-{narration_event.id}"
            if combat_id is not None and new_combat.get("ended"):
                await persist_combat_outcome_facts(
                    db,
                    game_session,
                    combat_id,
                    new_combat,
                    event_id=narration_event.id,
                    turn_id=ledger_turn,
                )
            if result.get("captive_outcome"):
                await persist_captive_outcome(
                    db,
                    game_session,
                    result["captive_outcome"],
                    event_id=narration_event.id,
                    turn_id=ledger_turn,
                )

            character_out = await character_play_state(
                db, game_session.active_character_id, game_session.campaign_id
            )
            campaign_complete = await maybe_complete_campaign(
                db, game_session.campaign_id, adjudication
            )
            current_scene_id = game_session.current_scene_id
            done = {
                "narration": narration,
                "citations": citations,
                "ruling_citation_ids": ruling_citation_ids,
                "rules_edition": edition,
                "character": character_out,
                "current_scene_id": current_scene_id,
                "combat_state": combat_state_public(new_combat),
                "campaign_complete": campaign_complete,
                "retrieval_degraded": bool(result.get("retrieval_degraded")),
                "source_status": result.get("source_status", []),
                "source_mode": result.get("source_mode", "module"),
            }
            narration_event.payload = {**narration_event.payload, "done": done}
            await db.commit()
        except MissingCharacterAssignmentError as e:
            await db.rollback()
            log.warning("turn_persist_missing_assignment", session_id=session_id, error=str(e))
            raise TurnError("character_not_assigned") from e
        except Exception as e:
            await db.rollback()
            log.exception("turn_persist_failed", session_id=session_id)
            raise TurnError("turn_save_failed") from e

    return done


async def run_turn(initial_state: dict[str, Any]) -> dict[str, Any]:
    """Run the full graph (sync endpoint). Node errors become TurnError("llm_unavailable")."""
    result = await app.ainvoke(dict(initial_state))
    if result.get("error"):
        log.error("turn_agent_error", error=result["error"])
        raise TurnError(result.get("error_code") or "llm_unavailable")
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
                "id": c.get("name"),
                "alive": c.get("alive", True),
                "disposition": disposition_of(c),
                "can_act": can_act(c),
            }
            for c in (combat_state.get("combatants") or [])
        ],
        "initiative": combat_state.get("initiative") or [],
        "current_turn": combat_state.get("current_turn"),
        "current_turn_index": combat_state.get("current_turn_index"),
        "player_ac": combat_state.get("player_ac"),
        "second_wind_used": combat_state.get("second_wind_used", False),
        # Additive (EDG-148): whether the engine fitted this roster to the solo budget, and why.
        "solo_budget": combat_state.get("solo_budget"),
        "second_wind_uses_remaining": combat_state.get("second_wind_uses_remaining"),
        # Additive (EDG-044): what the hero's last turn spent (action, bonus action, Dodge).
        "economy": combat_state.get("economy"),
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
                "citation": citations_for([c])[0],
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
        # Policy selectors and earned-clue IDs can encode reviewed internal names. They are
        # model-only routing data, so omit them from the optional client developer stream.
        "parsed_input": parsed.model_dump(exclude={"resolution_policy_id", "recall_fact_id"})
        if parsed is not None
        else None,
        "rules_context": _truncate_ctx(rules_ctx),
        "adventure_context": _truncate_ctx(adv_ctx),
        "rules_context_count": len(rules_ctx or []),
        "adventure_context_count": len(adv_ctx or []),
        "world_flags_in": world_flags_in or {},
        # Source references can identify planner-only module statistics. Keep those private
        # even in the optional client debug stream; numeric combat diagnostics remain.
        "combat_state": {
            **combat_state,
            "combatants": [
                {k: v for k, v in c.items() if k not in {"source_reference", "creature_id"}}
                for c in combat_state.get("combatants", [])
            ],
        }
        if combat_state
        else combat_state,
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
        yield sse_event("error", error_body(state.get("error_code") or "llm_unavailable"))
        return

    adjudication = state.get("adjudication_result")
    if adjudication is not None:
        yield sse_event("adjudication", public_adjudication(adjudication))

    yield sse_event("status", {"stage": "narrating"})
    if is_combat:
        # The combat subgraph already narrated the round; pseudo-stream it word by word.
        narration = state.get("narration") or ""
        for piece in _yield_narration_tokens(narration):
            yield sse_event("token", {"text": piece})
    elif (narration := deterministic_narration(state)) is not None:
        for piece in _yield_narration_tokens(narration):
            yield sse_event("token", {"text": piece})
    elif actions := actions_for_state(state):
        # A declared unsupported mechanic must not reach the player unchecked: wait for the
        # complete narration, drop claimed effects, and only then stream the bounded text.
        prompt = build_narrator_prompt(state)
        llm = make_narrator_model()
        try:
            response = await llm.ainvoke(prompt)
        except Exception:
            log.exception("turn_narrator_failed", session_id=session_id)
            yield sse_event("error", error_body("llm_unavailable"))
            return
        narration = apply_narration_boundary(
            message_text(response),
            actions,
            engine_roll_mode=engine_roll_mode(state),
            executed_spell=executed_spell_for_state(state),
        )
        for piece in _yield_narration_tokens(narration):
            yield sse_event("token", {"text": piece})
        lap("narrating_ms")
    else:
        prompt = build_narrator_prompt(state)
        llm = make_narrator_model()
        narration_parts: list[str] = []
        try:
            async for chunk in llm.astream(prompt):
                tok = message_text(chunk)
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
