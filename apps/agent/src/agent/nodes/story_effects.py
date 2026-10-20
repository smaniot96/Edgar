"""Canonical proposal validation and bounded planning/player projections.

Only retrieved, currently eligible reviewed spans may establish a source fact. The live
DM adjudicates goals from play; private criteria never become quotations.
"""

from __future__ import annotations

import re

from agent.committed_facts import committed_prepared_facts
from agent.models.adjudication import AdjudicationResult
from agent.resolution_policies import policy_effect_key
from agent.state import AgentState
from edgar_core.artifacts import text_digest
from edgar_core.manifests import require_published, select_scene_context
from edgar_core.sources import eligible_context, source_citation
from edgar_core.story import (
    HOMEBREW_KINDS,
    AcceptedStoryEffect,
    ClockDisclosure,
    StoryEffect,
    StoryFact,
    StoryRef,
    StoryState,
    accept_story_effect,
    dm_adjudication_reference,
    homebrew_name_key,
    homebrew_reference,
    homebrew_target,
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
    "learn_place": {"known"},
    "relationship": {"friendly", "neutral", "hostile"},
    "objective": {"announced", "active", "completed", "failed"},
    "milestone": {"announced", "active", "completed", "failed"},
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
            known_place_passage_ids = [
                p.passage_id
                for p in manifest.passages
                if p.passage_id in eligible_ids
                and p.audience == "player"
                and scene.scene_id in p.known_scene_ids
            ]
            can_move = scene.scene_id in selected["allowed_scene_ids"]
            if can_move or known_place_passage_ids:
                add(
                    "scene",
                    scene.scene_id,
                    scene.title,
                    [
                        p.passage_id
                        for p in manifest.passages
                        if p.passage_id in eligible_ids
                        and (
                            state.get("current_scene_id") in p.scene_ids
                            or p.passage_id in known_place_passage_ids
                        )
                    ],
                    known_place_passage_ids=known_place_passage_ids,
                    can_move=can_move,
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
                object_id=entity.get("object_id"),
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
                        completion_condition=definition.completion_condition,
                        engine_requirements=[
                            r.model_dump() for r in definition.engine_requirements
                        ],
                        engine_forbidden=[r.model_dump() for r in definition.engine_forbidden],
                        reveal_on=definition.reveal_on,
                        clock_max=definition.clock_max,
                        portable=definition.portable,
                        consumable=definition.consumable,
                        initial_owner=definition.initial_owner,
                        operations=definition.operations,
                        announcement_passage_ids=definition.announcement_passage_ids,
                        clock_reveal=definition.clock_reveal.model_dump()
                        if definition.clock_reveal
                        else None,
                    )
    return candidates + _player_promise_candidates(state)


def is_dm_goal_candidate(candidate: dict) -> bool:
    return candidate["target"]["kind"] in {"objective", "milestone"}


def _goal_engine_allows_completion(candidate: dict, state: AgentState) -> bool:
    """Check only engine facts; natural-language achievement remains the DM's judgment."""
    if not candidate.get("engine_requirements") and not candidate.get("engine_forbidden"):
        return True
    target = candidate["target"]
    manifest = require_published(state["adventure_manifests"][target["collection"]])
    statuses: dict[str, str] = {}
    for fact in committed_prepared_facts(state, target["collection"], manifest):
        if fact.kind == "entity" and fact.status in {"dead", "defeated", "captive", "released"}:
            statuses[fact.target_id] = fact.status
    return all(
        statuses.get(row["actor_id"]) in row["statuses"]
        for row in candidate.get("engine_requirements", [])
    ) and not any(
        statuses.get(row["actor_id"]) in row["statuses"]
        for row in candidate.get("engine_forbidden", [])
    )


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
    if effect.kind in {"visit", "move", "learn_place"}:
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
    if state.get("manifest_status") == "homebrew":
        return _accept_homebrew(adj, state)
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
    authorized_policy_effects = set(state.get("authorized_policy_effect_keys") or [])
    policy_reveals = {
        row.get("effect_key"): set(row.get("passage_ids") or [])
        for row in state.get("policy_reveal_authorizations") or []
    }
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
        policy_key = policy_effect_key(proposal)
        if candidate is None or not _kind_matches(proposal, candidate):
            continue
        dm_goal = is_dm_goal_candidate(candidate)
        if dm_goal and (
            not state.get("player_input", "").strip()
            or not (proposal.achievement_basis or "").strip()
        ):
            continue
        if (
            proposal.kind in {"objective", "milestone"}
            and proposal.status == "completed"
            and not _goal_engine_allows_completion(candidate, state)
        ):
            continue
        if proposal.kind == "visit" and proposal.target.canonical_id != state.get(
            "current_scene_id"
        ):
            continue
        if proposal.kind == "move" and any(effect.kind == "move" for effect in accepted):
            continue
        if proposal.kind == "move" and not candidate.get("can_move"):
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
        if proposal.kind == "learn_place":
            public = [
                c
                for c in public
                if c.get("passage_id") in candidate.get("known_place_passage_ids", [])
            ]
        if proposal.status == "announced":
            public = [
                c
                for c in public
                if c.get("passage_id") in candidate.get("announcement_passage_ids", [])
            ]
        if proposal.kind == "discover":
            public += [
                c
                for c in state.get("story_reveal_context", [])
                if c.get("collection") == proposal.target.collection
                and c.get("manifest_sha256") == proposal.target.manifest_sha256
                and c.get("source_sha256") == proposal.target.source_sha256
                and c.get("audience") == "player"
                and c.get("passage_id") in candidate.get("reveal_passage_ids", [])
                and (
                    policy_key not in policy_reveals
                    or c.get("passage_id") in policy_reveals[policy_key]
                )
                and _reviewed_chunk(c, state)
            ]
            public = list({source_citation(c, edition)["reference_id"]: c for c in public}.values())
            if policy_key in policy_reveals:
                public = [c for c in public if c.get("passage_id") in policy_reveals[policy_key]]
                if not policy_reveals[policy_key] <= {c.get("passage_id") for c in public}:
                    continue
        if not public and not dm_goal:
            continue
        if (
            not dm_goal
            and candidate.get("reveal_on", "success") == "success"
            and not adj.success
            and policy_key not in authorized_policy_effects
        ):
            continue
        if (
            proposal.kind not in _OBJECT_KINDS
            and candidate.get("statuses")
            and proposal.status not in candidate["statuses"]
        ):
            continue
        previous = [f for f in facts if f.kind == proposal.kind and f.target == proposal.target]
        if (
            proposal.kind != "clock"
            and previous
            and previous[-1].status
            in {
                "completed",
                "failed",
                "fulfilled",
                "broken",
                "consumed",
            }
        ):
            continue
        disclosure = None
        if proposal.kind == "clock":
            maximum = candidate.get("clock_max")
            prior = max((f.quantity or 0 for f in previous), default=0)
            disclosure = _clock_disclosure(candidate, state)
            prior_disclosure = previous[-1].clock_disclosure if previous else None
            if prior_disclosure:
                # Once learned, a quantity/limit/outcome does not become secret again.
                disclosure = ClockDisclosure(
                    current=prior_disclosure.current or bool(disclosure and disclosure.current),
                    maximum=prior_disclosure.maximum
                    or (disclosure.maximum if disclosure else None),
                    completion=prior_disclosure.completion
                    or bool(disclosure and disclosure.completion),
                )
            old_visibility = prior_disclosure or ClockDisclosure()
            new_visibility = disclosure or ClockDisclosure()
            if (
                maximum is None
                or proposal.quantity is None
                or not prior <= proposal.quantity <= maximum
                or (prior == proposal.quantity and old_visibility == new_visibility)
                or (proposal.status == "completed") != (proposal.quantity == maximum)
            ):
                continue
        public_text = "\n".join(
            c["text"]
            for c in sorted(public, key=lambda c: source_citation(c, edition)["reference_id"])
        )
        if dm_goal:
            # Private achievement evidence earns progress, not disclosure permission.
            # The journal names a goal only when its reviewed public support names it.
            public_text = (
                candidate["title"]
                if any(candidate["title"] in c["text"] for c in public)
                else None
            )
        effect = proposal.model_copy(update={"player_text": public_text})
        if effect.kind in _OBJECT_KINDS:
            try:
                from agent.object_effects import validate_object_effect
            except ImportError:
                continue
            effect = validate_object_effect(effect, state, accepted)
            if effect is None:
                continue
        source_refs = list(
            {
                source_citation(c, edition)["reference_id"]: source_citation(c, edition)
                for c in supporting + public
            }.values()
        )
        if dm_goal:
            source_refs.append(
                dm_adjudication_reference(
                    state["turn_id"],
                    effect.target,
                    action=state["player_input"],
                    success=adj.success,
                )
            )
        sealed = accept_story_effect(
            effect,
            turn_id=state["turn_id"],
            source_refs=source_refs,
            evidence_kind="dm_adjudication" if dm_goal else "source",
            origin_turn_id=state["turn_id"] if dm_goal else None,
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
            knowledge_kind="hint"
            if proposal.kind == "discover" and candidate.get("entity_kind") == "hint"
            else None,
            clock_disclosure=disclosure,
            related_object=proposal.target.model_copy(
                update={"kind": "object", "canonical_id": candidate["object_id"]}
            )
            if proposal.kind == "discover" and candidate.get("object_id")
            else None,
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


# EDG-169: homebrew proposals are judged only by engine-owned rules here, never by prose alone.
_HOMEBREW_PER_TURN = 3
_HOMEBREW_PROPOSAL_LIMIT = 6
_HOMEBREW_NUMBER = re.compile(r"\b\d*d\d+\b|\b(?:DC|AC|HP|XP|CR)\b|(?<!\w)[+-]\d", re.IGNORECASE)
_HOMEBREW_PRIVATE = re.compile(
    r"\b(?:gm|dm)\s+(?:only|note|secret)s?\b|\bspoiler\b|[\[(]\s*(?:gm|dm|secret|private)\b",
    re.IGNORECASE,
)
_HOMEBREW_MARKUP = re.compile(r"[<>\x00-\x08\x0b-\x1f]")


def _homebrew_sealed(proposal, turn_id: str, taken: set[str]):
    """Seal one homebrew proposal into an accepted effect, or return None when it fails."""
    name = " ".join(proposal.name.split())
    text = proposal.text.strip()
    joined = f"{name} {text}"
    if not name or not text or _HOMEBREW_MARKUP.search(joined):
        return None
    if _HOMEBREW_NUMBER.search(joined) or _HOMEBREW_PRIVATE.search(joined):
        return None
    # The display name becomes earned only when it is quoted in the player-visible text.
    if homebrew_name_key(name) not in homebrew_name_key(text) or homebrew_name_key(name) in taken:
        return None
    effect_kind, _, status = HOMEBREW_KINDS[proposal.kind]
    target = homebrew_target(proposal.kind, name)
    ref = homebrew_reference(turn_id, target)
    effect = StoryEffect(
        kind=effect_kind,
        target=target,
        status=status,
        source_reference_ids=[ref["reference_id"]],
        player_text=text,
    )
    return accept_story_effect(
        effect,
        turn_id=turn_id,
        source_refs=[ref],
        display_name=name,
    )


def _accept_homebrew(adj: AdjudicationResult, state: AgentState) -> AdjudicationResult:
    """Accept homebrew improvisations only in homebrew play, after a successful outcome.

    A failed check reveals nothing. Module play never reaches this path, so reviewed
    manifests, retrieval and their invented-fact prohibitions stay unchanged.
    """
    proposals = list(adj.homebrew_proposals[:_HOMEBREW_PROPOSAL_LIMIT])
    adj.homebrew_proposals = []
    if state.get("source_mode") != "homebrew" or not state.get("turn_id") or not adj.success:
        return adj
    facts = committed_facts(state)
    taken = {homebrew_name_key(f.display_name) for f in facts if f.display_name}
    character = state.get("character") or {}
    if character.get("name"):
        taken.add(homebrew_name_key(str(character["name"])))
    seen = {f.effect_id for f in facts}
    accepted: list[AcceptedStoryEffect] = []
    for proposal in proposals:
        if len(accepted) >= _HOMEBREW_PER_TURN:
            break
        sealed = _homebrew_sealed(proposal, state["turn_id"], taken)
        if sealed is None or sealed.effect_id in seen:
            continue
        seen.add(sealed.effect_id)
        taken.add(homebrew_name_key(sealed.display_name))
        accepted.append(sealed)
    adj.accepted_story_effects = accepted
    return adj


def _clock_disclosure(candidate: dict, state: AgentState) -> ClockDisclosure | None:
    reveal = candidate.get("clock_reveal")
    if reveal is None:
        return None
    flags = state.get("world_flags") or {}

    def allowed(field: str) -> bool:
        predicates = reveal.get(field)
        return predicates is not None and all(
            flags.get(key) == value for key, value in predicates.items()
        )

    return ClockDisclosure(
        current=allowed("current_requires"),
        maximum=candidate.get("clock_max") if allowed("maximum_requires") else None,
        completion=allowed("completion_requires"),
    )


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
        policy_key = policy_effect_key(effect)
        authorized_policy_effects = set(state.get("authorized_policy_effect_keys") or [])
        authorized_reveals = next(
            (
                set(row.get("passage_ids") or [])
                for row in state.get("policy_reveal_authorizations") or []
                if row.get("effect_key") == policy_key
            ),
            None,
        )
        if (
            effect.kind != "discover"
            or effect.status != "discovered"
            or not candidate
            or not candidate.get("reveal_passage_ids")
            or not candidate.get("discovery_flag")
            or (
                not adj.success
                and candidate.get("reveal_on") != "always"
                and policy_key not in authorized_policy_effects
            )
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
        if authorized_reveals is not None and not set(candidate["reveal_passage_ids"]).intersection(
            authorized_reveals
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
    reveal_allowlists = [
        set(row.get("passage_ids") or []) for row in state.get("policy_reveal_authorizations") or []
    ]
    if reveal_allowlists:
        allowed_passages = set.union(*reveal_allowlists)
        refreshed["adventure_context"] = [
            chunk
            for chunk in refreshed["adventure_context"]
            if chunk.get("passage_id") in allowed_passages
        ]
    state["story_reveal_context"] = refreshed["adventure_context"]
