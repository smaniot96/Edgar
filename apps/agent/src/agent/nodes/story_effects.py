"""Canonical proposal validation and bounded planning/player projections.

Only retrieved, currently eligible reviewed spans may establish a new fact. Private
planner prose can guide a decision but can never become a discovered player quotation.
"""

from __future__ import annotations

import re

from agent.models.adjudication import AdjudicationResult
from agent.state import AgentState
from edgar_core.artifacts import text_digest
from edgar_core.manifests import require_published, select_scene_context
from edgar_core.sources import eligible_context, source_citation
from edgar_core.story import (
    AcceptedStoryEffect,
    StoryEffect,
    StoryFact,
    StoryRef,
    StoryState,
    accept_story_effect,
    player_promise_ref,
    story_target_id,
)

_OBJECT_KINDS = {"acquire", "consume", "transfer", "use", "environment"}
_CATEGORIES = {
    "object": "objects",
    "objective": "objectives",
    "milestone": "milestones",
    "promise": "promises",
    "clock": "clocks",
}
_STATUSES = {
    "discover": {"discovered"},
    "encounter": {"encountered"},
    "visit": {"visited"},
    "move": {"visited"},
    "relationship": {"friendly", "neutral", "hostile"},
    "objective": {"active", "completed", "failed"},
    "milestone": {"completed"},
    "promise": {"active", "fulfilled", "broken"},
    "clock": {"advanced", "completed"},
}


def committed_facts(state: AgentState) -> list:
    return StoryState.model_validate(state.get("story_state") or {}).facts


def story_candidates(state: AgentState) -> list[dict]:
    """Canonical identities in the active scene, respecting persisted prerequisites."""
    candidates = []
    flags = state.get("world_flags") or {}
    for collection, value in (state.get("adventure_manifests") or {}).items():
        manifest = require_published(value)
        if state.get("current_scene_id") not in {s.scene_id for s in manifest.scenes}:
            continue
        selected = select_scene_context(manifest, state.get("current_scene_id"), flags)
        eligible_ids = set(selected["player_passage_ids"] + selected["planner_passage_ids"])

        def add(
            kind, identity, title, passage_ids, collection=collection, manifest=manifest, **extra
        ):
            candidates.append(
                {
                    "target": StoryRef(
                        collection=collection,
                        source_sha256=manifest.source_sha256,
                        manifest_sha256=manifest.reviewed_sha256,
                        kind=kind,
                        canonical_id=identity,
                    ).model_dump(),
                    "title": title,
                    "passage_ids": passage_ids,
                    **extra,
                }
            )

        for scene in manifest.scenes:
            if scene.scene_id in selected["allowed_scene_ids"]:
                add(
                    "scene",
                    scene.scene_id,
                    scene.title,
                    [
                        p.passage_id
                        for p in manifest.passages
                        if p.passage_id in eligible_ids
                        and state.get("current_scene_id") in p.scene_ids
                    ],
                )
        for entity in selected["entities"]:
            add(
                "entity",
                entity["entity_id"],
                entity["title"],
                [
                    p.passage_id
                    for p in manifest.passages
                    if p.passage_id in eligible_ids and entity["entity_id"] in p.entity_ids
                ],
                entity_kind=entity["kind"],
                reveal_on=entity["reveal_on"],
                discovery_flag=entity.get("discovery_flag"),
                reveal_passage_ids=[
                    p.passage_id
                    for p in manifest.passages
                    if p.audience == "player"
                    and entity["entity_id"] in p.entity_ids
                    and state.get("current_scene_id") in p.scene_ids
                    and entity.get("discovery_flag")
                    and p.requires.get(entity["discovery_flag"]) == "discovered"
                    and all(
                        k == entity["discovery_flag"] or flags.get(k) == v
                        for k, v in p.requires.items()
                    )
                ],
            )
        for kind, category in _CATEGORIES.items():
            for definition in getattr(manifest, category):
                if (
                    state.get("current_scene_id") in definition.scene_ids
                    and all(flags.get(k) == v for k, v in definition.requires.items())
                    and eligible_ids.intersection(definition.passage_ids)
                ):
                    add(
                        kind,
                        definition.canonical_id,
                        definition.title,
                        list(eligible_ids.intersection(definition.passage_ids)),
                        statuses=definition.statuses,
                        reveal_on=definition.reveal_on,
                        clock_max=definition.clock_max,
                        portable=definition.portable,
                        consumable=definition.consumable,
                        initial_owner=definition.initial_owner,
                        operations=definition.operations,
                    )
    return candidates + _player_promise_candidates(state)


def _explicit_player_promise(text: str) -> bool:
    # Bounded first-person utterance, including "Mara, I promise..."; quoted NPC speech
    # and negated promises cannot establish an actual promise by the player.
    return len(text) <= 12000 and bool(
        re.search(
            r"(?:^|[,;:\n]\s*)(?:I\s+(?:promise|swear)|(?:io\s+)?(?:prometto|giuro))\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _player_promise_candidates(state: AgentState) -> list[dict]:
    facts = committed_facts(state)
    text = state.get("player_input", "")
    candidates = {}
    for fact in facts:
        if (
            fact.kind == "encounter"
            and fact.status == "encountered"
            and _explicit_player_promise(text)
        ):
            target = player_promise_ref(fact.target, text)
            candidate = {
                "target": target.model_dump(),
                "promise_actor": fact.target.model_dump(),
                "title": "Player promise",
                "statuses": ["active"],
                "knowledge_fact_ids": [fact.fact_id],
                "source_reference_ids": [r["reference_id"] for r in fact.source_refs],
            }
        elif fact.kind == "promise" and fact.promise_actor is not None:
            candidate = {
                "target": fact.target.model_dump(),
                "promise_actor": fact.promise_actor.model_dump(),
                "title": "Player promise",
                "statuses": ["fulfilled", "broken"] if fact.status == "active" else [],
                "knowledge_fact_ids": [fact.fact_id],
                "source_reference_ids": fact.source_reference_ids,
                "original_quotation": fact.player_text,
            }
        else:
            continue
        candidates[
            target.model_dump_json() if fact.kind == "encounter" else fact.target.model_dump_json()
        ] = candidate
    return list(candidates.values())


def _validate_player_promise(proposal: StoryEffect, state: AgentState, facts: list, success: bool):
    """An exact player utterance is event evidence, never a newly discovered book fact."""
    if (
        proposal.kind != "promise"
        or proposal.target.kind != "promise"
        or proposal.promise_actor is None
        or proposal.promise_actor.kind != "entity"
        or proposal.quantity is not None
        or proposal.destination
        or proposal.operation
    ):
        return None
    previous = [fact for fact in facts if fact.kind == "promise" and fact.target == proposal.target]
    if previous:
        original = previous[0]
        if (
            previous[-1].status != "active"
            or proposal.status not in {"fulfilled", "broken"}
            or original.promise_actor != proposal.promise_actor
            or not success
        ):
            return None
        anchor = original
        quotation = original.player_text
        origin_turn_id = original.origin_turn_id or original.turn_id
    else:
        quotation = state.get("player_input", "")
        if (
            proposal.status != "active"
            or not _explicit_player_promise(quotation)
            or proposal.target != player_promise_ref(proposal.promise_actor, quotation)
        ):
            return None
        anchor = next(
            (
                fact
                for fact in committed_facts(state)
                if fact.kind == "encounter"
                and fact.status == "encountered"
                and fact.target == proposal.promise_actor
            ),
            None,
        )
        if anchor is None:
            return None
        origin_turn_id = state["turn_id"]
    if (
        not {anchor.fact_id, anchor.effect_id}.intersection(proposal.knowledge_fact_ids)
        or not proposal.source_reference_ids
        or not set(proposal.source_reference_ids) <= {r["reference_id"] for r in anchor.source_refs}
        or not any(r.get("audience") == "player" for r in anchor.source_refs)
    ):
        return None
    effect = proposal.model_copy(
        update={
            "player_text": quotation,
            "source_reference_ids": anchor.source_reference_ids,
            "knowledge_fact_ids": [anchor.fact_id],
        }
    )
    return accept_story_effect(
        effect,
        turn_id=state["turn_id"],
        source_refs=anchor.source_refs,
        display_name="Player promise",
        evidence_kind="player_utterance",
        origin_turn_id=origin_turn_id,
    )


def _kind_matches(effect: StoryEffect, candidate: dict) -> bool:
    target_kind = effect.target.kind
    if effect.kind in _OBJECT_KINDS:
        return target_kind == "object"
    if effect.kind in {"visit", "move"}:
        return target_kind == "scene"
    if effect.kind in {"discover", "encounter", "relationship"}:
        return target_kind == "entity" and (
            effect.kind == "discover" or candidate.get("entity_kind") == "actor"
        )
    return target_kind == effect.kind


def _reviewed_chunk(chunk: dict, state: AgentState) -> bool:
    value = (state.get("adventure_manifests") or {}).get(chunk.get("collection"))
    if value is None:
        return False
    manifest = require_published(value)
    passage = next((p for p in manifest.passages if p.passage_id == chunk.get("passage_id")), None)
    return bool(
        passage
        and chunk.get("source_sha256") == manifest.source_sha256
        and chunk.get("extraction_sha256") == manifest.extraction_sha256
        and chunk.get("manifest_sha256") == manifest.reviewed_sha256
        and chunk.get("audience") == passage.audience
        and text_digest(chunk.get("text", "")) == passage.text_sha256
    )


def validate_story_effects(adj: AdjudicationResult, state: AgentState) -> AdjudicationResult:
    """Reject ungrounded proposals and stamp only server-owned accepted context.

    Persistence revalidates these same proposals against the latest locked snapshot.
    The object hook is supplied by the object consequence module; absent it fails closed.
    """
    adj.accepted_story_effects = []
    if state.get("manifest_status") != "reviewed" or not state.get("turn_id"):
        return adj
    candidates = {
        StoryRef.model_validate(c["target"]).model_dump_json(): c for c in story_candidates(state)
    }
    edition = state.get("rules_edition", "2014")
    chunks = eligible_context(
        state.get("adventure_context", [])
        + state.get("planner_context", [])
        + state.get("story_reveal_context", []),
        edition,
    )
    evidence = {source_citation(c, edition)["reference_id"]: c for c in chunks}
    facts = committed_facts(state)
    known_ids = {identity for f in facts for identity in (f.fact_id, f.effect_id)}
    accepted: list[AcceptedStoryEffect] = []
    seen_effects = {f.effect_id for f in facts}
    for proposal in adj.story_effects[:20]:
        if proposal.promise_actor is not None:
            sealed = _validate_player_promise(proposal, state, facts, adj.success)
            if sealed is not None and sealed.effect_id not in seen_effects:
                seen_effects.add(sealed.effect_id)
                accepted.append(sealed)
                # Full fact identity is needed for later proposals in this same turn.
                facts.append(
                    StoryFact(**sealed.model_dump(), fact_id=story_target_id(sealed.target))
                )
            continue
        candidate = candidates.get(proposal.target.model_dump_json())
        if candidate is None or not _kind_matches(proposal, candidate):
            continue
        if proposal.kind == "visit" and proposal.target.canonical_id != state.get(
            "current_scene_id"
        ):
            continue
        if proposal.kind == "move" and any(effect.kind == "move" for effect in accepted):
            continue
        if proposal.kind not in _OBJECT_KINDS and proposal.status not in _STATUSES.get(
            proposal.kind, set()
        ):
            continue  # outcome is engine-only, never a model proposal
        if proposal.kind not in _OBJECT_KINDS and (proposal.operation or proposal.destination):
            continue
        if (
            proposal.kind != "clock"
            and proposal.kind not in _OBJECT_KINDS
            and proposal.quantity is not None
        ):
            continue
        if not set(proposal.knowledge_fact_ids) <= known_ids:
            continue
        if (
            not proposal.source_reference_ids
            or not set(proposal.source_reference_ids) <= evidence.keys()
        ):
            continue
        supporting = [evidence[rid] for rid in proposal.source_reference_ids]
        if any(
            c.get("collection") != proposal.target.collection
            or c.get("manifest_sha256") != proposal.target.manifest_sha256
            or c.get("source_sha256") != proposal.target.source_sha256
            or c.get("passage_id") not in candidate["passage_ids"]
            for c in supporting
        ):
            continue
        if any(not _reviewed_chunk(c, state) for c in supporting):
            continue
        public = [c for c in supporting if c.get("audience") == "player"]
        if proposal.kind == "discover":
            public += [
                c
                for c in state.get("story_reveal_context", [])
                if c.get("collection") == proposal.target.collection
                and c.get("manifest_sha256") == proposal.target.manifest_sha256
                and c.get("source_sha256") == proposal.target.source_sha256
                and c.get("audience") == "player"
                and c.get("passage_id") in candidate.get("reveal_passage_ids", [])
                and _reviewed_chunk(c, state)
            ]
            public = list({source_citation(c, edition)["reference_id"]: c for c in public}.values())
        if not public:
            continue
        if candidate.get("reveal_on", "success") == "success" and not adj.success:
            continue
        if (
            proposal.kind not in _OBJECT_KINDS
            and candidate.get("statuses")
            and proposal.status not in candidate["statuses"]
        ):
            continue
        previous = [f for f in facts if f.kind == proposal.kind and f.target == proposal.target]
        if previous and previous[-1].status in {
            "completed",
            "failed",
            "fulfilled",
            "broken",
            "consumed",
        }:
            continue
        if proposal.kind == "clock":
            maximum = candidate.get("clock_max")
            prior = max((f.quantity or 0 for f in previous), default=0)
            if (
                maximum is None
                or proposal.quantity is None
                or not prior < proposal.quantity <= maximum
                or (proposal.status == "completed") != (proposal.quantity == maximum)
            ):
                continue
        effect = proposal.model_copy(
            update={
                "player_text": "\n".join(
                    c["text"]
                    for c in sorted(
                        public, key=lambda c: source_citation(c, edition)["reference_id"]
                    )
                )
            }
        )
        if effect.kind in _OBJECT_KINDS:
            try:
                from agent.object_effects import validate_object_effect
            except ImportError:
                continue
            effect = validate_object_effect(effect, state, accepted)
            if effect is None:
                continue
        sealed = accept_story_effect(
            effect,
            turn_id=state["turn_id"],
            source_refs=list(
                {
                    source_citation(c, edition)["reference_id"]: source_citation(c, edition)
                    for c in supporting + public
                }.values()
            ),
            display_name=(
                candidate["title"]
                if any(candidate["title"] in c["text"] for c in public)
                or (
                    proposal.kind in _OBJECT_KINDS
                    and any(
                        (
                            entry.get("name", "") if isinstance(entry, dict) else str(entry)
                        ).casefold()
                        == candidate["title"].casefold()
                        for entries in (
                            (state.get("character") or {}).get("inventory") or {}
                        ).values()
                        if isinstance(entries, list)
                        for entry in entries
                    )
                )
                else proposal.target.kind.capitalize()
            ),
        )
        if sealed.effect_id in seen_effects:
            continue
        seen_effects.add(sealed.effect_id)
        accepted.append(sealed)
        known_ids.update((sealed.effect_id, story_target_id(sealed.target)))
        # Same-turn proposals cannot clear a newly committed terminal state.
        facts.append(sealed.model_copy())
    adj.accepted_story_effects = accepted
    moves = [effect for effect in accepted if effect.kind == "move"]
    adj.scene_id = moves[-1].target.canonical_id if moves else state.get("current_scene_id")
    discovery_flags = {
        entity.discovery_flag
        for manifest in (state.get("adventure_manifests") or {}).values()
        for entity in require_published(manifest).entities
        if entity.discovery_flag
    }
    # Newly learned knowledge belongs to the session ledger, never campaign WorldFlags.
    adj.flags_set = [flag for flag in adj.flags_set if flag.key not in discovery_flags]
    adj.flags_cleared = [flag for flag in adj.flags_cleared if flag not in discovery_flags]
    return adj


def safe_story_summary(adj: AdjudicationResult, engine_summary: str) -> None:
    """Free-form model summaries cannot smuggle rejected effects into narration/storage."""
    adj.mechanical_summary = engine_summary
    if adj.damage:
        adj.mechanical_summary += f" Damage taken: {adj.damage}."
    if adj.completion_status == "pending":
        adj.mechanical_summary += " Ask the player whether they want to conclude the adventure."


async def prepare_story_validation_state(adj: AdjudicationResult, state: AgentState) -> None:
    """Load exact self-gated player reveals through the existing reviewed retrieval path.

    This happens before persistence opens a transaction. Evidence stays validator-only:
    only accepted quotations ever reach narration. Other prerequisites remain persisted.
    """
    import asyncio

    from db.vector.retrieval import retrieve_active_scene

    if state.get("manifest_status") != "reviewed":
        return
    candidates = {
        StoryRef.model_validate(c["target"]).model_dump_json(): c for c in story_candidates(state)
    }
    edition = state.get("rules_edition", "2014")
    chunks = eligible_context(
        state.get("adventure_context", []) + state.get("planner_context", []), edition
    )
    evidence = {source_citation(c, edition)["reference_id"]: c for c in chunks}
    flags = dict(state.get("world_flags") or {})
    changed = False
    for effect in adj.story_effects[:20]:
        candidate = candidates.get(effect.target.model_dump_json())
        if (
            effect.kind != "discover"
            or effect.status != "discovered"
            or not candidate
            or not candidate.get("reveal_passage_ids")
            or not candidate.get("discovery_flag")
            or (not adj.success and candidate.get("reveal_on") != "always")
            or not effect.source_reference_ids
            or not set(effect.source_reference_ids) <= evidence.keys()
        ):
            continue
        if any(
            evidence[rid].get("collection") != effect.target.collection
            or evidence[rid].get("manifest_sha256") != effect.target.manifest_sha256
            or evidence[rid].get("source_sha256") != effect.target.source_sha256
            or evidence[rid].get("passage_id") not in candidate["passage_ids"]
            for rid in effect.source_reference_ids
        ):
            continue
        flags[candidate["discovery_flag"]] = "discovered"
        changed = True
    if not changed:
        return
    try:
        refreshed = await asyncio.to_thread(
            retrieve_active_scene,
            state.get("adventure_manifests") or {},
            state.get("current_scene_id"),
            flags,
            client=state.get("qdrant_client"),
            rules_edition=edition,
        )
    except Exception:
        # No quotation, no discovery. Never substitute private model prose on read failure.
        return
    state["story_reveal_context"] = refreshed["adventure_context"]
