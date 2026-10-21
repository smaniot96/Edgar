"""Turn orchestration shared by the sync and SSE turn endpoints.

Both endpoints run the same agent topology (`agent.graph`) and persist through `persist_turn`,
which writes everything a turn produces — combat state, adjudication world writes, telemetry
rows, the narration row and campaign completion — in ONE transaction on a short-lived DB
session, so a failure anywhere leaves no partial writes and no connection is held across LLM
calls.

The SSE runner drives `prepare_app` (the graph minus the linear-path narrator) so the input is
parsed exactly once, then streams the narrator with `llm.astream`.

Combat is streamed from FINALISED mechanics (EDG-079). The combat round resolves dice, HP,
dispositions, the adjudication, completion and the round telemetry first, then returns a pending
narration (`defer_narration`) instead of calling the narrator. The runner streams that single
narrator call with the same prompt builder and the `play.combat_narrator` profile, then calls
`finalize_round_narration`, the same finalisation the sync endpoint uses. Streamed combat tokens
are PROVISIONAL: the boundary, roster fallback and outcome suffix are applied to the complete text
after the stream, so `done.narration` is authoritative and may differ from the concatenated tokens.
Engine-only combat rulings (no model narration) carry their final text as word-sized tokens
without a provider call.

Event types (see apps/api/README.md for the public contract):
  status: {"stage": "parsing" | "retrieving" | "adjudicating" | "narrating" | "saving"}
  token: {"text": "<chunk>"}                        # provisional; done.narration is authoritative
  adjudication: <AdjudicationResult dict>           # emitted once after adjudication
  debug: {...}                                      # only when the request asks AND EDGAR_DEBUG=1
  done: {"narration", "character", "current_scene_id", "combat_state", "campaign_complete"}
  error: {"code", "message", "detail"}              # stable code + generic text; never raw errors

Responses and covered failure logs use fixed categories and opaque support references;
exception messages and provider bodies are never copied to either boundary.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

import db.postgres.session as db_session_module
from agent.combat_confirmation import public_pending, public_standing_orders
from agent.combat_telemetry import COMBAT_ROUND_EVENT, combat_round_public
from agent.combatants import can_act, concentration_of, disposition_of, stable_id, view_fields
from agent.graph import app, prepare_app, route_after_input_parser
from agent.graph_combat import PENDING_ROUND_NARRATION_KEY, finalize_round_narration
from agent.llm import message_text
from agent.nodes.narrator import (
    build_narrator_prompt,
    deterministic_narration,
    make_narrator_model,
    narrator_context,
    narrator_request,
)
from agent.party_combat import hero_rows, holder_character_id, is_party
from agent.positioning import band_of
from agent.provider_trace import traced_call, traced_stream
from agent.supported_rules import (
    actions_for_state,
    apply_narration_boundary,
    engine_applied_for_state,
)
from agent.turn_diagnostics import (
    TURN_DIAGNOSTICS_EVENT,
    TurnDiagnostics,
    bind_collector,
    bound_steps,
    finalize_turn_diagnostics,
    new_turn_diagnostics,
    turn_diagnostics_public,
)
from agent.turn_trace import TRACE_MAX_CHUNKS, TRACE_SNIPPET_WIDTH, turn_trace_event
from apps.api.schemas.stream import frame_violations
from db.postgres.models import Campaign, CharacterAssignment, EventLog, Session as SessionModel
from edgar_core.model_profiles import (
    current_model_profiles,
    effective_profile,
    model_profile_snapshot,
    profile_diagnostics,
)
from edgar_core.safe_errors import OutcomeState, SafeFailure
from edgar_core.sources import citations_for, public_citations

from .advancement import grant_victory_milestone, victory_just_ended
from .campaign_completion import maybe_complete_campaign
from .character_assignments import character_play_state
from .combat import persist_captive_outcome, persist_combat, persist_combat_npcs
from .journal import journal_tracking_marker
from .operational_metrics import observe_committed_turn
from .roll_record import build_roll_record, roll_status_for
from .safe_errors import classify_error, log_failure
from .spotlight import SpotlightRefused, SpotlightTurn, apply_turn_commit
from .story_state import record_hero_death_if_dead
from .world_writes import (
    MissingCharacterAssignmentError,
    apply_adjudication,
    apply_story_inventory,
)

log = structlog.get_logger()


class TurnError(Exception):
    """One safe projection retained across response and log correlation."""

    def __init__(
        self,
        error: BaseException | str | None,
        *,
        outcome: OutcomeState | str = OutcomeState.UNKNOWN,
    ) -> None:
        self.failure = classify_error(error, outcome=outcome)
        super().__init__(self.failure.code)
        self.code = self.failure.code
        self.status_code = self.failure.status_code
        self.message = self.failure.message

    def body(self) -> dict[str, Any]:
        return _failure_body(self.failure)


def _failure_body(failure: SafeFailure) -> dict[str, Any]:
    body = failure.to_payload()
    if failure.code == "campaign_sources_unavailable":
        body["retrieval_degraded"] = True
    return body


def error_body(
    error: BaseException | str | None,
    *,
    outcome: OutcomeState | str = OutcomeState.NOT_COMMITTED,
    event: str | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    failure = classify_error(error, outcome=outcome)
    if event:
        log_failure(event, failure, session_id=session_id)
    return _failure_body(failure)


def sse_event(event: str, data: Any) -> str:
    """Format one Server-Sent Events frame. Two newlines terminate a frame.

    The frame is checked against the stream contract (`apps.api.schemas.stream`) but always sent
    unchanged: a violation is logged by field path only, never by value, so a protocol drift
    shows in logs and tests without ever cutting a committed turn's `done` frame short.
    """
    violations = frame_violations(event, data)
    if violations:
        log.error("sse_frame_contract_violation", frame=event, fields=violations)
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def public_adjudication(adjudication) -> dict | None:
    if adjudication is None:
        return None
    # Proposals and canonical/private evidence are engine inputs, never client DTOs.
    value = adjudication.model_dump(
        exclude={
            "story_effects",
            "accepted_story_effects",
            "accepted_observations",
            "proposed_prepared_amendment",
            "accepted_prepared_amendment",
        }
    )
    value["citations"] = public_citations(value.get("citations", []))
    value["citation_ids"] = [r["reference_id"] for r in value["citations"]]
    return value


async def apply_party_updates(
    db: Any, game_session: SessionModel, updates: list[dict[str, Any]]
) -> list[int]:
    """Write the sheet changes a party fight round made to heroes other than the acting one (EDG-049).

    Each entry is the engine's `{character_id, hp_delta, death_saves}`. The assignments are locked in
    id order first, so two sessions of one campaign cannot interleave. Returns the changed character ids.
    """
    from agent.models.adjudication import CharacterUpdate

    from .world_writes import _apply_character_update

    ids = sorted({int(u["character_id"]) for u in updates})
    if ids:
        await db.execute(
            select(CharacterAssignment)
            .where(
                CharacterAssignment.character_id.in_(ids),
                CharacterAssignment.campaign_id == game_session.campaign_id,
                CharacterAssignment.ended_at.is_(None),
            )
            .order_by(CharacterAssignment.id)
            .with_for_update()
        )
    for update in updates:
        await _apply_character_update(
            db,
            int(update["character_id"]),
            game_session.campaign_id,
            CharacterUpdate(
                hp_delta=int(update.get("hp_delta", 0)),
                death_saves=update.get("death_saves"),
            ),
        )
    return ids


async def _party_sheets(
    db: Any, game_session: SessionModel, character_ids: list[int]
) -> list[dict[str, Any]]:
    """Each changed hero's own play state with the account that controls it (private to that player)."""
    from db.postgres.models import SessionParticipant

    campaign = await db.get(Campaign, game_session.campaign_id)
    sheets = []
    for character_id in character_ids:
        controller = await db.scalar(
            select(SessionParticipant.user_id).where(
                SessionParticipant.session_id == game_session.id,
                SessionParticipant.character_id == character_id,
            )
        )
        sheets.append(
            {
                "user_id": controller or (campaign.created_by if campaign is not None else None),
                "character_id": character_id,
                "character": await character_play_state(db, character_id, game_session.campaign_id),
            }
        )
    return sheets


async def persist_turn(
    session_id: int,
    body_message: str,
    result: dict[str, Any],
    *,
    spotlight: SpotlightTurn | None = None,
    side: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the whole turn in one transaction and return the `done` payload.

    Opens its own short-lived session so the caller never holds a pooled connection across LLM
    calls. The character is read *before* campaign completion closes the assignments, so the
    final turn of a campaign still reports the hero's state. Raises TurnError (after rollback).

    `side` (EDG-049), when given, receives what the table channel needs besides `done`: the private
    sheets of the other heroes this turn changed (`party_sheets`) and the hero the fight passes to
    (`combat_holder`). It never reaches a response.

    `spotlight` (EDG-048) is the decision the turn was admitted under. It is checked and applied on
    the locked session row as the last step, so the turn commits only if the spotlight still stands.
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
    # The original dice, stored on the narration row so reload never re-rolls (EDG-030).
    turn_roll = build_roll_record(adjudication, result.get("turn_id"))
    roll_status = roll_status_for(turn_roll)
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

            from edgar_core.story import is_homebrew_effect

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
            from .prepared_amendments import (
                PreparedAmendmentConflict,
                append_session_prepared_amendment,
                load_session_prepared_amendments,
                revalidate_runtime_amendment,
            )

            current_overlays, current_amendment_events = await load_session_prepared_amendments(
                db, session_id, game_session.campaign_id, current_manifests
            )
            if current_overlays != (
                result.get("prepared_campaign_overlays") or {}
            ) or current_amendment_events != (result.get("prepared_amendment_events") or []):
                raise ValueError("Session prepared state changed during generation")
            accepted_amendment = None
            if adjudication and adjudication.accepted_prepared_amendment:
                accepted_amendment = revalidate_runtime_amendment(
                    adjudication.accepted_prepared_amendment,
                    state={
                        **result,
                        "story_state": current_story,
                        "world_flags": current_flags,
                        "prepared_campaign_overlays": current_overlays,
                    },
                    manifests=current_manifests,
                )
            elif result.get("accepted_prepared_amendment"):
                # LangGraph state is not an authority. Only the hidden engine-owned
                # adjudication receipt is eligible for persistence.
                raise PreparedAmendmentConflict("Untrusted prepared amendment state")
            effects = adjudication.accepted_story_effects if adjudication else []
            # Homebrew consequences are revalidated by persist_story_effects against the locked
            # ledger; reviewed-manifest revalidation below only covers module consequences.
            module_effects = [e for e in effects if not is_homebrew_effect(e)]
            if module_effects:
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
                            for effect in module_effects
                        ],
                    ),
                    {**result, "character": current_character, "story_state": current_story},
                )
                if checked.accepted_story_effects != module_effects:
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

            # EDG-045: a won encounter grants a milestone once, on its open-to-victory transition.
            won_now = await victory_just_ended(db, session_id, new_combat)
            combat_id = None
            if new_combat:
                combat_id = await persist_combat(db, session_id, new_combat)
                await persist_combat_npcs(
                    db, session_id, game_session.campaign_id, combat_id, new_combat
                )
            party_fight = bool(new_combat and is_party(new_combat.get("combatants")))
            if won_now:
                if party_fight:
                    # EDG-049: every hero who fought and lived shares the milestone, once.
                    for row in hero_rows(new_combat["combatants"]):
                        if row.get("character_id") is not None and disposition_of(row) != "dead":
                            await grant_victory_milestone(db, campaign, int(row["character_id"]))
                else:
                    await grant_victory_milestone(db, campaign, game_session.active_character_id)
            changed_heroes: list[int] = []
            if party_fight:
                changed_heroes = await apply_party_updates(
                    db, game_session, result.get("party_updates") or []
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
                    "roll_status": roll_status,
                    "roll": turn_roll,
                    **journal_tracking_marker(),
                },
            )
            db.add(narration_event)
            await db.flush()
            # EDG-045: a hero the engine killed this turn is recorded in the story ledger, once.
            await record_hero_death_if_dead(
                db, game_session, event_id=narration_event.id, turn_id=turn_id or ""
            )
            for hero_id in changed_heroes:
                await record_hero_death_if_dead(
                    db,
                    game_session,
                    event_id=narration_event.id,
                    turn_id=turn_id or "",
                    character_id=hero_id,
                )
            if accepted_amendment is not None:
                await append_session_prepared_amendment(
                    db,
                    session_id=session_id,
                    game_session=game_session,
                    receipt=accepted_amendment,
                    turn_id=turn_id,
                )
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
                "event_id": narration_event.id,
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
                "roll_status": roll_status,
                "roll": turn_roll,
            }
            narration_event.payload = {**narration_event.payload, "done": done}
            next_holder = (
                holder_character_id(new_combat)
                if party_fight and not new_combat.get("ended")
                else None
            )
            apply_turn_commit(
                game_session, spotlight, now=datetime.now(UTC), combat_holder=next_holder
            )
            if party_fight and not new_combat.get("ended") and spotlight is None:
                # Auth-less local mode has no spotlight plan, but the fight still names its holder.
                game_session.active_character_id = next_holder or game_session.active_character_id
            if side is not None:
                side["combat_holder"] = next_holder
                side["party_sheets"] = await _party_sheets(db, game_session, changed_heroes)
            await db.commit()
        except MissingCharacterAssignmentError as e:
            await db.rollback()
            failure = TurnError("character_not_assigned", outcome="not_committed")
            log_failure("turn_persist_missing_assignment", failure.failure, session_id=session_id)
            raise failure from e
        except SpotlightRefused as e:
            await db.rollback()
            failure = TurnError(e.code, outcome="not_committed")
            log_failure("turn_spotlight_refused", failure.failure, session_id=session_id)
            raise failure from e
        except Exception as e:
            await db.rollback()
            failure = TurnError("turn_save_failed")
            log_failure("turn_persist_failed", failure.failure, session_id=session_id)
            raise failure from e

    return done


async def run_turn(
    initial_state: dict[str, Any], *, diagnostics: TurnDiagnostics | None = None
) -> dict[str, Any]:
    """Run the full graph, retaining provider attribution only from recognized failures.

    The turn's `turn_diagnostics` row is appended to `telemetry_events` here, so `persist_turn`
    writes it in the same transaction as the turn (EDG-053).
    """
    diag = diagnostics or new_turn_diagnostics(
        turn_id=initial_state.get("turn_id"), delivery="sync"
    )
    with model_profile_snapshot(), bind_collector(diag):
        result = await app.ainvoke(dict(initial_state))
    if result.get("error"):
        failure = TurnError(result.get("error_code"), outcome="not_committed")
        log_failure("turn_agent_error", failure.failure)
        raise failure
    events = list(result.get("telemetry_events") or [])
    events.append(finalize_turn_diagnostics(diag, events))
    return {**result, "telemetry_events": events}


SKIP_TEXT = "I take the Dodge action."


async def run_combat_skip(
    initial_state: dict[str, Any], *, diagnostics: TurnDiagnostics | None = None
) -> dict[str, Any]:
    """Resolve an idle hero's skipped combat turn (EDG-049): the default Dodge, no parser call.

    The same round node a typed turn reaches, fed a pre-parsed Dodge, so a skip rolls and persists exactly
    like the hero acting. The model is only asked to narrate the round.
    """
    from agent.graph_combat import combat_round_node
    from agent.models.parsed_input import ParsedInput

    diag = diagnostics or new_turn_diagnostics(turn_id=initial_state.get("turn_id"), delivery="sync")
    state = {
        **initial_state,
        "player_input": SKIP_TEXT,
        "parsed_input": ParsedInput(intent="combat"),
        "combat_skip": True,
    }
    diag.set_path("combat")
    with model_profile_snapshot(), bind_collector(diag):
        result = await combat_round_node(state)
    if result.get("error"):
        failure = TurnError(result.get("error_code"), outcome="not_committed")
        log_failure("turn_agent_error", failure.failure)
        raise failure
    events = list(result.get("telemetry_events") or [])
    events.append(finalize_turn_diagnostics(diag, events))
    return {**state, **result, "telemetry_events": events}


def combat_state_public(combat_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim the persisted combat dict to what the client HUD needs.

    Roster HP, round and outcome, plus the initiative view (order, AC, whose turn).
    """
    if not combat_state:
        return None
    initiative = combat_state.get("initiative") or []
    current_index = combat_state.get("current_turn_index")
    current_turn_id = None
    if (
        not combat_state.get("ended")
        and type(current_index) is int
        and 0 <= current_index < len(initiative)
    ):
        current_turn_id = initiative[current_index].get("id")
    party = is_party(combat_state.get("combatants"))
    public: dict[str, Any] = {
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
                "id": stable_id(c),
                "alive": c.get("alive", True),
                "disposition": disposition_of(c),
                "can_act": can_act(c),
                # Additive (EDG-044): a foe's distance band and whether its reaction is spent.
                "band": None if c.get("is_player") else band_of(c),
                "reaction_used": bool(c.get("reaction_used", False)),
                **(_hero_fields(c) if party and c.get("is_player") else {}),
            }
            for c in (combat_state.get("combatants") or [])
        ],
        "initiative": initiative,
        "current_turn_id": current_turn_id,
        "current_turn": combat_state.get("current_turn"),
        "current_turn_index": combat_state.get("current_turn_index"),
        "player_ac": combat_state.get("player_ac"),
        "second_wind_used": combat_state.get("second_wind_used", False),
        # Additive (EDG-148): whether the engine fitted this roster to the solo budget, and why.
        "solo_budget": combat_state.get("solo_budget"),
        "second_wind_uses_remaining": combat_state.get("second_wind_uses_remaining"),
        # Additive (EDG-044): what the hero's last turn spent (action, bonus action, Dodge).
        "economy": combat_state.get("economy"),
        "raging": bool(combat_state.get("raging", False)),
        # Additive (EDG-043): the spell the hero is concentrating on, or None.
        "concentration": _concentration_spell(combat_state),
    }
    # Additive (EDG-182): a multi-part declaration waiting for the player's yes or no, as its summary.
    if pending := public_pending(combat_state.get("combatants") or []):
        public["pending_declaration"] = pending
    # Additive (EDG-182): the hero's standing reaction orders, each cancellable by a reply.
    if orders := public_standing_orders(combat_state.get("combatants") or []):
        public["standing_orders"] = orders
    if party:
        # Additive (EDG-049): the fight is shared, and the engine says whose turn it is.
        public["party"] = True
        holder = (
            None
            if combat_state.get("ended")
            else combat_state.get("current_turn_character_id", holder_character_id(combat_state))
        )
        public["current_turn_character_id"] = holder
    return public


def _hero_fields(c: dict[str, Any]) -> dict[str, Any]:
    """What the table sees of a hero in a party fight: its sheet id, whether it left, its death saves.

    Death saves are rolled in the open at a table, so the record is public; nothing else of the sheet is.
    """
    from agent.death import public_view as death_public, record_from
    from agent.resolution import DEATH_SAVES_KEY

    return {
        "character_id": c.get("character_id"),
        "fled": bool(c.get("fled", False)),
        "death_saves": death_public(record_from(c.get(DEATH_SAVES_KEY))),
    }


def table_combat_state(combat_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """The combat view the table channel carries: a party fight's pending declaration stays with its hero."""
    if combat_state and combat_state.get("party"):
        return {k: v for k, v in combat_state.items() if k != "pending_declaration"}
    return combat_state


def _concentration_spell(combat_state: dict[str, Any]) -> str | None:
    heroes = [c for c in combat_state.get("combatants") or [] if c.get("is_player")]
    # EDG-049: in a party fight the HUD facts are the hero whose turn is next (or just acted).
    player = next((c for c in heroes if c.get("acting")), heroes[0] if heroes else None)
    concentration = concentration_of(player) if player is not None else None
    return str(concentration["spell"]) if concentration else None


def _truncate_ctx(
    items: Any, n: int = TRACE_MAX_CHUNKS, width: int = TRACE_SNIPPET_WIDTH
) -> list[dict[str, Any]]:
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


def combat_round_from_events(events: list[dict] | None) -> dict[str, Any] | None:
    """The developer view of this turn's combat round, or None for a non-combat turn."""
    for event in events or []:
        if event.get("event_type") == COMBAT_ROUND_EVENT:
            return combat_round_public(event.get("payload"))
    return None


def turn_diagnostics_from_events(events: list[dict] | None) -> dict[str, Any] | None:
    """The developer view of this turn's measured latency, usage and cost row (EDG-053)."""
    for event in events or []:
        if event.get("event_type") == TURN_DIAGNOSTICS_EVENT:
            return turn_diagnostics_public(event.get("payload"))
    return None


def stage_turn_trace(result: dict[str, Any], *, debug: bool, delivery: str) -> dict[str, Any]:
    """Stage this turn's developer trace beside its other telemetry rows (EDG-138).

    Runs only when the request opted into debug. The request schema already forces `debug` off
    unless `EDGAR_DEBUG` is set, so no trace is written while developer debug is disabled. The row
    is appended to `telemetry_events` and commits (or rolls back) with the turn in `persist_turn`.
    """
    if not debug:
        return result
    events = list(result.get("telemetry_events") or [])
    path = (turn_diagnostics_from_events(events) or {}).get("path") or "normal"
    event = turn_trace_event(
        turn_id=result.get("turn_id"),
        delivery=delivery,
        path=path,
        parsed_input=result.get("parsed_input"),
        rules_context=result.get("rules_context"),
        adventure_context=result.get("adventure_context"),
        rules_edition=result.get("rules_edition", "2014"),
        source_mode=result.get("source_mode", "module"),
        narrator_profile="play.combat_narrator" if path == "combat" else "play.narrator",
        model_profiles=profile_diagnostics(current_model_profiles()),
    )
    if event is None:
        return result
    return {**result, "telemetry_events": [*events, event]}


def _debug_combat_state(combat_state: dict | None) -> dict | None:
    """Expose observed combat diagnostics without unpublished source or capability data.

    Combatant JSON also stores reviewed actor bindings, uncast spells and unused lair options.
    These remain private even when a client asks for debug output. Use an explicit projection
    so a new persisted engine field cannot silently become player-visible.
    """
    if not combat_state:
        return combat_state
    numeric_fields = {
        "name", "display_name", "is_player", "hp_current", "hp_max", "ac", "init", "init_bonus",
        "attack_bonus", "damage_dice", "is_boss", "stat_adaptation", "legendary_spent",
    }
    return {
        **(combat_state_public(combat_state) or {}),
        "id": combat_state.get("id"),
        "initiative_order": combat_state.get("initiative_order"),
        "combatants": [
            {**{k: v for k, v in c.items() if k in numeric_fields}, **view_fields(c)}
            for c in combat_state.get("combatants", [])
        ],
    }


def _debug_payload(
    *,
    parsed: Any,
    rules_ctx: Any,
    adv_ctx: Any,
    world_flags_in: dict | None,
    combat_state: dict | None,
    timings: dict[str, int],
    combat_diagnostics: dict | None = None,
    turn_diagnostics: dict | None = None,
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
        "combat_state": _debug_combat_state(combat_state),
        "timings_ms": timings,
        # Combat turns only: the round's actors, original dice and outcomes (EDG-031).
        "combat_diagnostics": combat_diagnostics,
        # This turn's measured latency, usage and cost (EDG-053). Null fields are unmeasured.
        "turn_diagnostics": turn_diagnostics,
        "model": effective_profile("play.narrator").model,
        "model_profiles": profile_diagnostics(current_model_profiles()),
    }


def _yield_narration_tokens(narration: str) -> list[str]:
    """Split engine-final text into word-sized tokens. Used only where no model narration exists

    (captive and clarification rulings, deterministic text). Model narration streams its own chunks.
    """
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
    diagnostics: TurnDiagnostics | None = None,
    on_committed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    spotlight: SpotlightTurn | None = None,
    side: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Stream a turn under one immutable settings snapshot, including the narrator tail.

    `on_committed` runs right after the turn's transaction commits, before its `done` frame is
    yielded, so a committed turn is never unknown to the session channel (EDG-047).
    """
    with model_profile_snapshot():
        # aclosing: a client disconnect closes the turn generator here, and it closes the
        # provider stream below, rather than leaving that to garbage collection.
        async with aclosing(
            _stream_session_turn(
                session_id=session_id,
                body_message=body_message,
                initial_state=initial_state,
                debug=debug,
                diagnostics=diagnostics,
                on_committed=on_committed,
                spotlight=spotlight,
                side=side,
            )
        ) as frames:
            async for frame in frames:
                yield frame


class _NarrationStream:
    """What one streamed narrator call produced. The caller reads it after the frames."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.failed = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


async def _stream_narrator(
    *,
    llm: Any,
    prompt: Any,
    profile: str,
    run: _NarrationStream,
    diagnostics: TurnDiagnostics,
    session_id: int,
) -> AsyncIterator[str]:
    """Yield `token` frames from one narrator call, traced on the turn's collector (EDG-053).

    Tokens are provisional: `run.text` is used only after the caller has finalised the turn, and
    nothing here commits. The provider stream is closed however this generator exits, including
    client disconnect (GeneratorExit) and task cancellation, so a stopped turn never leaves a
    provider call running. A provider failure becomes one redacted `error` frame and `run.failed`.
    """
    stream = None
    try:
        stream = llm.astream(prompt)
        # aclosing: the traced wrapper records the call (cancelled, failed or complete) when it ends.
        async with aclosing(
            traced_stream(stream, kind="narrate", profile=profile, collector=diagnostics)
        ) as chunks:
            async for chunk in chunks:
                text = message_text(chunk)
                if not text:
                    continue
                diagnostics.record_first_text()
                run.parts.append(text)
                yield sse_event("token", {"text": text})
    except Exception as e:
        run.failed = True
        yield sse_event("error", error_body(e, event="turn_narrator_failed", session_id=session_id))
    finally:
        # The provider stream itself, not only the wrapper, must be finalised on every exit path.
        if stream is not None and hasattr(stream, "aclose"):
            await stream.aclose()


async def _stream_session_turn(
    *,
    session_id: int,
    body_message: str,
    initial_state: dict[str, Any],
    debug: bool = False,
    diagnostics: TurnDiagnostics | None = None,
    on_committed: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    spotlight: SpotlightTurn | None = None,
    side: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    t0 = time.monotonic()
    lap_start = t0
    timings: dict[str, int] = {}
    diag = diagnostics or new_turn_diagnostics(
        turn_id=initial_state.get("turn_id"), delivery="stream"
    )

    def lap(bucket: str) -> None:
        """Charge the time since the previous lap to `bucket` (debug timings)."""
        nonlocal lap_start
        now = time.monotonic()
        timings[bucket] = timings.get(bucket, 0) + int((now - lap_start) * 1000)
        lap_start = now

    yield sse_event("status", {"stage": "parsing"})

    # Parse once, route, and run everything up to (not including) the linear-path narrator. The
    # combat round defers its single narrator call (EDG-079) so the runner can stream it. Graph
    # node wall times reach the collector through `timed_node`; `bound_steps` binds it per step.
    state: dict[str, Any] = dict(initial_state)
    is_combat = False
    try:
        async with aclosing(
            bound_steps(
                prepare_app.astream(
                    {**initial_state, "defer_narration": True}, stream_mode=["updates", "values"]
                ),
                diag,
            )
        ) as graph_updates:
            async for mode, chunk in graph_updates:
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
                        diag.set_path("combat" if is_combat else "normal")
                        yield sse_event("status", {"stage": "retrieving"})
                        if is_combat:
                            yield sse_event("status", {"stage": "adjudicating"})
                    elif node == "world_retriever":
                        yield sse_event("status", {"stage": "adjudicating"})
    except Exception as exc:
        yield sse_event("error", error_body(exc, event="turn_agent_crashed", session_id=session_id))
        return

    if state.get("error"):
        yield sse_event(
            "error",
            error_body(state.get("error_code"), event="turn_agent_error", session_id=session_id),
        )
        return

    adjudication = state.get("adjudication_result")
    if adjudication is not None:
        yield sse_event("adjudication", public_adjudication(adjudication))

    yield sse_event("status", {"stage": "narrating"})
    narrating_started = time.monotonic()
    pending = state.pop(PENDING_ROUND_NARRATION_KEY, None)
    # Combat with a model narration: the mechanics are final. Stream the one narrator call, then
    # finalise the round with the same function the sync node uses. Only `done` after the commit
    # is authoritative. Its narrating and mechanics stages come from the round's own row.
    if pending is not None and actions_for_state(pending.narr_state):
        llm, prompt = narrator_request(pending.narr_state, pending.profile)
        # A declared unsupported mechanic must not reach the player unchecked (as on the
        # normal path): wait for the complete text, finalise, then stream only the bounded text.
        try:
            response = await traced_call(
                lambda: llm.ainvoke(prompt), kind="narrate", profile=pending.profile
            )
        except Exception as e:
            yield sse_event(
                "error", error_body(e, event="turn_narrator_failed", session_id=session_id)
            )
            return
        text = message_text(response)
        narrating_ms = int((time.monotonic() - narrating_started) * 1000)
        state.update(
            finalize_round_narration(pending, text, bound=True, narrating_ms=narrating_ms)
        )
        for piece in _yield_narration_tokens(state["narration"]):
            diag.record_first_text()
            yield sse_event("token", {"text": piece})
        narration = state["narration"]
        timings["narrating_ms"] = timings.get("narrating_ms", 0) + narrating_ms
        lap_start = time.monotonic()
    elif pending is not None:
        llm, prompt = narrator_request(pending.narr_state, pending.profile)
        run = _NarrationStream()
        async with aclosing(
            _stream_narrator(
                llm=llm,
                prompt=prompt,
                profile=pending.profile,
                run=run,
                diagnostics=diag,
                session_id=session_id,
            )
        ) as frames:
            async for frame in frames:
                yield frame
        if run.failed:
            return
        narrating_ms = int((time.monotonic() - narrating_started) * 1000)
        state.update(
            finalize_round_narration(pending, run.text, bound=True, narrating_ms=narrating_ms)
        )
        narration = state["narration"]
        timings["narrating_ms"] = timings.get("narrating_ms", 0) + narrating_ms
        lap_start = time.monotonic()
    elif is_combat:
        # Engine-only combat rulings (captives, clarifications, deterministic text): already final.
        narration = state.get("narration") or ""
        for piece in _yield_narration_tokens(narration):
            diag.record_first_text()
            yield sse_event("token", {"text": piece})
    elif (narration := deterministic_narration(state)) is not None:
        for piece in _yield_narration_tokens(narration):
            diag.record_first_text()
            yield sse_event("token", {"text": piece})
    elif actions := actions_for_state(state):
        # A declared unsupported mechanic must not reach the player unchecked: wait for the
        # complete narration, drop claimed effects, and only then stream the bounded text.
        prompt = build_narrator_prompt(state)
        llm = make_narrator_model()
        try:
            response = await traced_call(
                lambda: llm.ainvoke(prompt), kind="narrate", profile="play.narrator"
            )
        except Exception as e:
            yield sse_event(
                "error", error_body(e, event="turn_narrator_failed", session_id=session_id)
            )
            return
        narration = apply_narration_boundary(
            message_text(response), actions, applied=engine_applied_for_state(state)
        )
        for piece in _yield_narration_tokens(narration):
            diag.record_first_text()
            yield sse_event("token", {"text": piece})
        lap("narrating_ms")
    else:
        prompt = build_narrator_prompt(state)
        llm = make_narrator_model()
        run = _NarrationStream()
        async with aclosing(
            _stream_narrator(
                llm=llm,
                prompt=prompt,
                profile="play.narrator",
                run=run,
                diagnostics=diag,
                session_id=session_id,
            )
        ) as frames:
            async for frame in frames:
                yield frame
        if run.failed:
            return
        narration = run.text
        lap("narrating_ms")
    if not is_combat:
        # Combat narration is measured by the round itself (adopted into the turn row below).
        diag.record_stage_ms("narrating_ms", int((time.monotonic() - narrating_started) * 1000))
    state["narration"] = narration

    yield sse_event("status", {"stage": "saving"})
    # Built before the commit so the row rides the same transaction. Its total ends just before it.
    telemetry = list(state.get("telemetry_events") or [])
    state["telemetry_events"] = [*telemetry, finalize_turn_diagnostics(diag, telemetry)]
    state = stage_turn_trace(state, debug=debug, delivery="stream")
    try:
        done_payload = await persist_turn(
            session_id, body_message, state, spotlight=spotlight, side=side
        )
    except TurnError as e:
        yield sse_event("error", e.body())
        return
    except Exception:
        # persist_turn maps its own failures to TurnError; this only catches e.g. a failed
        # rollback or connection checkout, which must still reach the client as an event.
        yield sse_event(
            "error",
            error_body(
                "turn_save_failed",
                outcome="unknown",
                event="turn_persist_failed",
                session_id=session_id,
            ),
        )
        return
    observe_committed_turn(state.get("telemetry_events"), diag)
    lap("saving_ms")
    if on_committed is not None:
        # Never raises by contract (see services.session_broadcast); the done frame follows.
        await on_committed(done_payload)

    if debug:
        combat_diagnostics = combat_round_from_events(state.get("telemetry_events"))
        # Stages the combat round measured itself. Unmeasured ones stay absent (UI: unavailable).
        combat_timings = (combat_diagnostics or {}).get("timings_ms") or {}
        yield sse_event(
            "debug",
            _debug_payload(
                parsed=state.get("parsed_input"),
                rules_ctx=state.get("rules_context"),
                adv_ctx=state.get("adventure_context"),
                world_flags_in=initial_state.get("world_flags"),
                combat_state=state.get("combat_state"),
                timings={
                    **timings,
                    **combat_timings,
                    "total_ms": int((time.monotonic() - t0) * 1000),
                },
                combat_diagnostics=combat_diagnostics,
                turn_diagnostics=turn_diagnostics_from_events(state.get("telemetry_events")),
            ),
        )
    yield sse_event("done", done_payload)
