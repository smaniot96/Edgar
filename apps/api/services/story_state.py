"""Canonical session story ledger and explicit player-known projection.

Only accepted effects enter this service. Callers own the transaction and must revalidate
source evidence before writing. No model call, campaign flag write, or prepared definition
serialization occurs here. Rows are immutable; the full effect sequence survives reload.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.combatants import disposition_of
from db.postgres.models import NPC, EventLog, Session as SessionModel, StoryFact as StoryFactRow
from edgar_core.story import (
    AcceptedStoryEffect,
    StoryEffect,
    StoryFact,
    StoryRef,
    StoryState,
    accept_story_effect,
    known_story_name,
    player_known_story as player_known_story,
    story_target_id,
)

# EDG-146: engine-owned combat outcomes enter the ledger as `encounter` facts on a combat-owned
# target. The target has no reviewed manifest, so its digests name the encounter itself.
COMBAT_OUTCOME_COLLECTION = "combat"
COMBAT_OUTCOME_STATUS = {"captured": "captive", "dead": "dead", "defeated": "defeated"}
_CAPTIVE_TERMINAL = frozenset({"dead", "released"})


class StoryStateConflict(ValueError):
    """The planned state is stale or would rewrite committed history."""


async def load_story_state(
    db: AsyncSession, session_id: int, campaign_id: int | None = None
) -> dict:
    if campaign_id is not None:
        owner = await db.scalar(
            select(SessionModel.campaign_id).where(SessionModel.id == session_id)
        )
        if owner != campaign_id:
            raise StoryStateConflict("Story session does not belong to this campaign")
    rows = await db.scalars(
        select(StoryFactRow).where(StoryFactRow.session_id == session_id).order_by(StoryFactRow.id)
    )
    return StoryState(facts=[StoryFact.model_validate(row.payload) for row in rows]).model_dump()


async def persist_story_effects(
    db: AsyncSession,
    game_session: SessionModel,
    effects: list[AcceptedStoryEffect | dict],
    *,
    event_id: int,
    expected_state: dict | None = None,
) -> dict:
    """Append sealed effects atomically with the caller's narration event; never commit.

    The session lock serializes competing writers. Exact replays are no-ops, while reused
    effect identities with different content, stale snapshots and terminal regressions fail
    the entire caller-owned transaction. fact_id groups a canonical target; effect_id is
    the immutable occurrence/content identity and the database idempotency constraint.
    """
    locked = await db.scalar(
        select(SessionModel).where(SessionModel.id == game_session.id).with_for_update()
    )
    if locked is None or locked.campaign_id != game_session.campaign_id:
        raise StoryStateConflict("Story session ownership changed")
    event_owner = await db.scalar(select(EventLog.session_id).where(EventLog.id == event_id))
    if event_owner != game_session.id:
        raise StoryStateConflict("Story provenance event belongs to another session")
    current = await load_story_state(db, game_session.id, game_session.campaign_id)
    if (
        expected_state is not None
        and StoryState.model_validate(expected_state).model_dump() != current
    ):
        raise StoryStateConflict("Story state changed while the turn was being generated")
    facts = [StoryFact.model_validate(f) for f in current["facts"]]
    by_effect = {f.effect_id: f for f in facts}
    for raw in effects:
        effect = AcceptedStoryEffect.model_validate(raw)
        # Validate the sealed content identity again at the persistence boundary.
        sealed = accept_story_effect(
            StoryEffect.model_validate(effect.model_dump(include=set(StoryEffect.model_fields))),
            turn_id=effect.turn_id,
            source_refs=effect.source_refs,
            display_name=effect.display_name,
            knowledge_kind=effect.knowledge_kind,
            clock_disclosure=effect.clock_disclosure,
            related_object=effect.related_object,
        )
        if sealed.effect_id != effect.effect_id:
            raise StoryStateConflict("Story effect content does not match its identity")
        existing = by_effect.get(effect.effect_id)
        if existing:
            # The original turn/event remain authoritative on an exact cross-turn replay.
            fields = set(AcceptedStoryEffect.model_fields) - {"turn_id"}
            if existing.model_dump(include=fields) != effect.model_dump(include=fields):
                raise StoryStateConflict("An accepted story effect cannot be rewritten")
            continue
        target_id = story_target_id(effect.target)
        prior = [f for f in facts if f.fact_id == target_id and f.kind == effect.kind]
        _validate_progression(effect, prior)
        fact = StoryFact(**effect.model_dump(), fact_id=target_id, event_id=event_id)
        db.add(
            StoryFactRow(
                session_id=game_session.id,
                effect_id=fact.effect_id,
                fact_id=fact.fact_id,
                event_id=event_id,
                payload=fact.model_dump(),
            )
        )
        facts.append(fact)
        by_effect[fact.effect_id] = fact
        await _sync_npc(db, game_session, fact)
    await db.flush()
    return StoryState(facts=facts).model_dump()


def _validate_progression(effect: AcceptedStoryEffect, prior: list[StoryFact]) -> None:
    if not prior:
        return
    latest = prior[-1]
    if effect.kind == "discover" and any(
        f.player_text != effect.player_text or f.source_refs != effect.source_refs for f in prior
    ):
        raise StoryStateConflict("A discovered quotation and its origin are immutable")
    if effect.kind == "promise" and any(
        (f.player_text, f.promise_actor, f.evidence_kind, f.origin_turn_id)
        != (effect.player_text, effect.promise_actor, effect.evidence_kind, effect.origin_turn_id)
        for f in prior
    ):
        raise StoryStateConflict("A promise quotation and its origin are immutable")
    terminal = {"completed", "failed", "fulfilled", "broken"}
    if effect.kind in {"objective", "milestone", "promise"} and latest.status in terminal:
        if effect.status != latest.status:
            raise StoryStateConflict("A recorded terminal story outcome cannot regress")
    if effect.kind == "clock" and latest.quantity is not None:
        if effect.quantity is None or effect.quantity < latest.quantity:
            raise StoryStateConflict("A story clock cannot run backwards")


async def _sync_npc(db: AsyncSession, game_session: SessionModel, fact: StoryFact) -> None:
    if fact.kind not in {"encounter", "relationship"} or fact.target.kind != "entity":
        return
    npc = await db.scalar(
        select(NPC).where(NPC.session_id == game_session.id, NPC.story_identity == fact.fact_id)
    )
    if npc is None:
        npc = NPC(
            campaign_id=game_session.campaign_id,
            session_id=game_session.id,
            story_identity=fact.fact_id,
            name=known_story_name(fact) or "Encountered person",
            disposition="neutral",
            stat_block={},
        )
        db.add(npc)
    if fact.kind == "relationship":
        npc.disposition = fact.status
    await db.flush()


def session_story_flags(
    state: dict, manifests: dict, legacy_flags: dict[str, str]
) -> dict[str, str]:
    """Keep manual campaign flags; derive manifest discoveries only from this session."""
    known = StoryState.model_validate(state)
    discovery_flags = {
        e.get("discovery_flag")
        for m in manifests.values()
        for e in m.get("entities", [])
        if e.get("discovery_flag")
    }
    flags = {k: v for k, v in legacy_flags.items() if k not in discovery_flags}
    for fact in known.facts:
        if fact.kind not in {"discover", "encounter"}:
            continue
        manifest = manifests.get(fact.target.collection, {})
        if (
            manifest.get("source_sha256") != fact.target.source_sha256
            or manifest.get("reviewed_sha256") != fact.target.manifest_sha256
        ):
            continue
        for entity in manifest.get("entities", []):
            if entity["entity_id"] == fact.target.canonical_id and entity.get("discovery_flag"):
                flags[entity["discovery_flag"]] = "discovered"
    return flags


def combat_outcome_target(combat_id: int, combatant_id: str) -> StoryRef:
    """Stable identity of one foe in one encounter, independent of its status."""
    encounter = hashlib.sha256(f"combat-outcome:{combat_id}".encode()).hexdigest()
    manifest = hashlib.sha256(b"engine:combat-outcome:v1").hexdigest()
    who = hashlib.sha256(f"{combat_id}\n{combatant_id}".encode()).hexdigest()
    return StoryRef(
        collection=COMBAT_OUTCOME_COLLECTION,
        source_sha256=encounter,
        manifest_sha256=manifest,
        kind="entity",
        canonical_id=f"combatant_{who[:40]}",
    )


def _provenance(combat_id: int, combatant_id: str, action: str, round_: int | None) -> dict:
    reference = f"{combat_id}\n{combatant_id}\n{action}"
    return {
        "reference_id": hashlib.sha256(reference.encode()).hexdigest(),
        "audience": "engine",
        "combat_id": combat_id,
        "combat_round": round_,
        "combatant_id": combatant_id,
    }


async def persist_combat_outcome_facts(
    db: AsyncSession,
    game_session: SessionModel,
    combat_id: int,
    combat_state: dict,
    *,
    event_id: int,
    turn_id: str,
) -> None:
    """Project captured, dead and defeated foes of an ended encounter (caller commits).

    Only combatants with a stored disposition are recorded. Legacy `alive=false` rows have none,
    so they stay undecided and no intent is invented for them.
    """
    if not combat_state.get("ended"):
        return
    for c in combat_state.get("combatants") or []:
        if c.get("is_player") or "disposition" not in c:
            continue
        status = COMBAT_OUTCOME_STATUS.get(disposition_of(c))
        if status is None:
            continue
        combatant_id = str(c.get("name") or "")
        await _record_encounter_fact(
            db,
            game_session,
            combat_outcome_target(combat_id, combatant_id),
            status,
            display_name=str(c.get("display_name") or combatant_id),
            provenance=_provenance(combat_id, combatant_id, status, combat_state.get("round")),
            event_id=event_id,
            turn_id=turn_id,
        )


async def persist_captive_fact(
    db: AsyncSession,
    game_session: SessionModel,
    *,
    combat_id: int,
    combatant_id: str,
    status: str,
    display_name: str,
    combat_round: int | None,
    event_id: int,
    turn_id: str,
) -> None:
    """Record a player's release or kill of a captive; a terminal outcome is never reversed."""
    if status not in ("released", "dead"):
        raise StoryStateConflict("Unsupported captive outcome")
    await _record_encounter_fact(
        db,
        game_session,
        combat_outcome_target(combat_id, combatant_id),
        status,
        display_name=display_name,
        provenance=_provenance(combat_id, combatant_id, status, combat_round),
        event_id=event_id,
        turn_id=turn_id,
    )


async def _record_encounter_fact(
    db: AsyncSession,
    game_session: SessionModel,
    target: StoryRef,
    status: str,
    *,
    display_name: str,
    provenance: dict,
    event_id: int,
    turn_id: str,
) -> None:
    """Append one immutable engine fact once. Exact replays are no-ops; the session lock serializes."""
    await db.execute(
        select(SessionModel.id).where(SessionModel.id == game_session.id).with_for_update()
    )
    current = await load_story_state(db, game_session.id, game_session.campaign_id)
    facts = [StoryFact.model_validate(f) for f in current["facts"]]
    effect = accept_story_effect(
        StoryEffect(kind="encounter", target=target, status=status),
        turn_id=turn_id,
        source_refs=[provenance],
        display_name=display_name,
    )
    if any(f.effect_id == effect.effect_id for f in facts):
        return
    prior = [f for f in facts if f.fact_id == story_target_id(target)]
    if prior and prior[-1].status in _CAPTIVE_TERMINAL and prior[-1].status != status:
        raise StoryStateConflict("A recorded captive outcome cannot change")
    fact = StoryFact(**effect.model_dump(), fact_id=story_target_id(target), event_id=event_id)
    db.add(
        StoryFactRow(
            session_id=game_session.id,
            effect_id=fact.effect_id,
            fact_id=fact.fact_id,
            event_id=event_id,
            payload=fact.model_dump(),
        )
    )
    await db.flush()
