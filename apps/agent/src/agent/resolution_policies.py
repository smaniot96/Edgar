"""Resolve reviewed source policy IDs into engine-owned checks and story consequences."""

from __future__ import annotations

import re

from agent.committed_facts import committed_prepared_facts
from agent.models.parsed_input import ParsedInput
from agent.resolution import normalise_check
from edgar_core.artifacts import text_digest
from edgar_core.manifests import (
    AdventureManifest,
    ManifestError,
    policy_fact_requirements_met,
    require_published,
    select_scene_context,
)
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryRef, StoryState, player_story_effect


def policy_effect_key(effect: StoryEffect) -> str:
    """Same identity before validation and after accepted-effect metadata is attached."""
    proposal = StoryEffect.model_validate(effect.model_dump(include=set(StoryEffect.model_fields)))
    proposal.source_reference_ids = sorted(set(proposal.source_reference_ids))
    proposal.knowledge_fact_ids = sorted(set(proposal.knowledge_fact_ids))
    return proposal.model_dump_json(exclude={"player_text"})


def describe_policy_result(success: bool, effects: list, existing: list) -> str:
    """Describe committed outcomes without quoting obstacle setup as an accomplishment."""
    lines = ["Your approach succeeds." if success else "The check fails."]
    verbs = {
        "encounter": "You meet",
        "relationship": "Your relationship changes with",
        "move": "You reach",
        "learn_place": "You learn about",
        "objective": "Objective",
        "milestone": "Milestone",
        "acquire": "You acquire",
        "consume": "You spend",
        "transfer": "You move",
        "use": "You use",
        "environment": "You change",
    }
    for effect in effects:
        name = effect.display_name or "the supported result"
        if effect.kind == "discover":
            lines.append(f"You learn:\n{effect.player_text}")
        elif effect.kind == "clock":
            public = player_story_effect(effect)
            if public is None:
                continue
            details = []
            if public["quantity"] is not None:
                details.append(f"{public['quantity']} intervals recorded")
            if public["clock_maximum"] is not None:
                details.append(f"maximum {public['clock_maximum']}")
            if effect.clock_disclosure and effect.clock_disclosure.completion:
                details.append(public["status"])
            lines.append(
                f"{name}: " + (", ".join(details) if details else "the known urgency changes.")
            )
        else:
            lines.append(f"{verbs.get(effect.kind, 'Result')}: {name} ({effect.status}).")
    for effect in existing:
        if effect.kind == "discover":
            lines.append(f"You already know:\n{effect.player_text}")
    if len(lines) == 1:
        lines.append("That approach produces no further change. You can try another approach.")
    return "\n\n".join(dict.fromkeys(lines))


def policies_for_state(state: dict) -> list[dict]:
    """Return current-scene policies with only an availability bit and safe task label."""
    if state.get("source_mode") != "module":
        return []
    manifests = state.get("adventure_manifests") or {}
    scene_id = state.get("current_scene_id")
    edition = state.get("rules_edition", "2014")
    policies = []
    for collection, raw in manifests.items():
        try:
            manifest = require_published(raw)
        except ManifestError:
            continue
        if not any(scene.scene_id == scene_id for scene in manifest.scenes):
            continue
        try:
            scene_selection = select_scene_context(manifest, scene_id, state.get("world_flags"))
        except ManifestError:
            continue
        readable_ids = set(
            scene_selection["player_passage_ids"] + scene_selection["planner_passage_ids"]
        )
        inventory = state.get("character") or {}
        from agent.resolution import inventory_items, match_inventory_item

        items = inventory_items(inventory)
        source_status = next(
            (row for row in (state.get("source_status") or []) if row.get("slug") == collection),
            None,
        )
        source_ready = source_status is None or source_status.get("status") == "ready"
        committed = committed_prepared_facts(state, collection, manifest)
        for policy in manifest.resolution_policies:
            if scene_id not in policy.scene_ids:
                continue
            if policy.mode == "automatic" and not any(
                effect.kind not in {"objective", "milestone"} for effect in policy.on_automatic
            ):
                continue  # Narrative-only legacy actions belong to the live DM.
            supported_check = True
            if policy.mode == "check":
                normalized = normalise_check(policy.check)
                supported_check = bool(
                    normalized
                    and normalized[1] in {"skill", "ability", "save", "tool"}
                    and normalized[0] == policy.check
                )
            prerequisites_met = all(
                (state.get("world_flags") or {}).get(key) == value
                for key, value in policy.requires.items()
            ) and policy_fact_requirements_met(policy.requires_facts, committed)
            gear_met = all(match_inventory_item(items, item) for item in policy.requires_items)
            evidence_met = set(policy.passage_ids) <= readable_ids
            edition_met = (
                manifest.rules_edition in {"2014", "2024"} and manifest.rules_edition == edition
            )
            policies.append(
                {
                    "policy_key": f"{collection}:{policy.policy_id}",
                    "collection": collection,
                    "source_sha256": manifest.source_sha256,
                    "manifest_sha256": manifest.reviewed_sha256,
                    "rules_edition": manifest.rules_edition,
                    "policy": policy,
                    "available": bool(
                        source_ready
                        and supported_check
                        and prerequisites_met
                        and gear_met
                        and evidence_met
                        and edition_met
                    ),
                    "rejection_reason": "That approach is not available in the current circumstances.",
                }
            )
    return policies


def parser_policy_choices(state: dict) -> list[dict]:
    """A parser-only selector. It never receives DCs or effect payloads."""
    return [
        {
            "policy_key": item["policy_key"],
            "action": item["policy"].action,
            "available": item["available"],
        }
        for item in policies_for_state(state)
    ]


def parser_earned_clues(state: dict) -> list[dict]:
    """Give the parser only session-earned clue identities and their exact public text."""
    facts = StoryState.model_validate(state.get("story_state") or {}).facts
    choices = []
    for fact in facts:
        if fact.kind != "discover" or fact.status != "discovered" or not fact.player_text:
            continue
        ref = _valid_public_source(fact, state)
        if ref is None:
            continue
        choices.append(
            {
                "fact_id": fact.fact_id,
                "label": fact.display_name or "earned clue",
                "text": fact.player_text,
                "source": ref.get("source") or "earned clue",
            }
        )
    return choices


def _valid_public_source(fact, state: dict) -> dict | None:
    """Return the original public citation for a committed clue.

    A clue is an immutable session fact. If the module is republished after the clue was
    earned, the committed quotation and its sealed public citation remain the historical
    record; current retrieval must never substitute a newer passage.
    """
    historical_refs = [
        ref
        for ref in fact.source_refs
        if ref.get("collection") == fact.target.collection
        and ref.get("source_sha256") == fact.target.source_sha256
        and ref.get("manifest_sha256") == fact.target.manifest_sha256
        and ref.get("audience") == "player"
        and type(ref.get("page")) is int
        and ref["page"] > 0
        and all(
            isinstance(ref.get(key), str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
            for key in ("reference_id", "source_sha256", "extraction_sha256")
        )
    ]
    if not historical_refs:
        return None
    raw = (state.get("adventure_manifests") or {}).get(fact.target.collection)
    if raw is None:
        return historical_refs[0]
    try:
        manifest = require_published(raw)
    except ManifestError:
        return historical_refs[0]
    if (
        manifest.source_sha256 != fact.target.source_sha256
        or manifest.reviewed_sha256 != fact.target.manifest_sha256
    ):
        return historical_refs[0]
    passages = {p.passage_id: p for p in manifest.passages}
    for ref in fact.source_refs:
        passage = passages.get(ref.get("passage_id"))
        if (
            ref.get("collection") == fact.target.collection
            and ref.get("source_sha256") == manifest.source_sha256
            and ref.get("manifest_sha256") == manifest.reviewed_sha256
            and ref.get("audience") == "player"
            and passage is not None
            and passage.audience == "player"
            and ref.get("page") == passage.page
        ):
            return ref
    return historical_refs[0]


def selected_policy(state: dict, parsed: ParsedInput | None) -> dict | None:
    key = getattr(parsed, "resolution_policy_id", None)
    if not key:
        return None
    return next((item for item in policies_for_state(state) if item["policy_key"] == key), None)


def policy_source_is_fresh(selected: dict, state: dict) -> bool:
    """Require every declared action passage to match this turn's verified retrieval."""
    policy = selected["policy"]
    chunks = state.get("adventure_context", []) + state.get("planner_context", [])
    by_passage = {
        chunk.get("passage_id"): chunk
        for chunk in chunks
        if chunk.get("collection") == selected["collection"]
    }
    try:
        manifest = require_published(
            (state.get("adventure_manifests") or {})[selected["collection"]]
        )
    except (KeyError, ManifestError):
        return False
    for passage_id in policy.passage_ids:
        chunk = by_passage.get(passage_id)
        passage = next((item for item in manifest.passages if item.passage_id == passage_id), None)
        if (
            chunk is None
            or passage is None
            or chunk.get("source_sha256") != manifest.source_sha256
            or chunk.get("extraction_sha256") != manifest.extraction_sha256
            or chunk.get("manifest_sha256") != manifest.reviewed_sha256
            or chunk.get("audience") != passage.audience
            or chunk.get("text_sha256") != passage.text_sha256
            or text_digest(chunk.get("text", "")) != passage.text_sha256
        ):
            return False
    return True


def _effect_target_kind(kind: str) -> str:
    if kind in {"discover", "encounter", "relationship"}:
        return "entity"
    if kind in {"acquire", "consume", "transfer", "use", "environment"}:
        return "object"
    if kind in {"move", "learn_place"}:
        return "scene"
    if kind in {"objective", "milestone", "clock"}:
        return kind
    raise ValueError(f"Unsupported policy effect: {kind}")


def policy_effects_for_outcome(
    state: dict, selected: dict, *, success: bool
) -> tuple[list[StoryEffect], list[dict]]:
    """Build server-owned proposals from reviewed policy data and exact retrieved passages."""
    policy = selected["policy"]
    definitions = (
        policy.on_automatic
        if policy.mode == "automatic"
        else (policy.on_success if success else policy.on_failure)
    )
    selected["expected_effect_count"] = len(definitions)
    selected["authorized_effect_keys"] = []
    selected["existing_effects"] = []
    current = next(
        (item for item in policies_for_state(state) if item["policy_key"] == selected.get("policy_key")),
        None,
    )
    if (
        current is None
        or not current["available"]
        or current["source_sha256"] != selected.get("source_sha256")
        or current["manifest_sha256"] != selected.get("manifest_sha256")
        or current["policy"] != policy
        or not policy_source_is_fresh(current, state)
    ):
        selected["generation_failed"] = True
        return [], []
    # A reviewed policy can resolve mechanics and disclosures; selecting it does not
    # establish that its narrative goals were earned. The live DM judges all such goals.
    definitions = [
        outcome for outcome in definitions if outcome.kind not in {"objective", "milestone"}
    ]
    selected["expected_effect_count"] = len(definitions)
    if not definitions:
        return [], []

    sources = state.get("adventure_context", []) + state.get("planner_context", [])
    refs_by_passage = {}
    for chunk in sources:
        if (
            chunk.get("collection") == selected["collection"]
            and chunk.get("source_sha256") == selected["source_sha256"]
            and chunk.get("manifest_sha256") == selected["manifest_sha256"]
        ):
            refs_by_passage[chunk.get("passage_id")] = source_citation(
                chunk, state.get("rules_edition", "2014")
            )

    candidate_effects: list[StoryEffect] = []
    authorizations: list[dict] = []
    facts = StoryState.model_validate(state.get("story_state") or {}).facts
    expected_effect_count = 0
    for outcome in definitions:
        target = StoryRef(
            collection=selected["collection"],
            source_sha256=selected["source_sha256"],
            manifest_sha256=selected["manifest_sha256"],
            kind=_effect_target_kind(outcome.kind),
            canonical_id=outcome.target_id,
        )
        previous = [fact for fact in facts if fact.kind == outcome.kind and fact.target == target]
        if outcome.kind == "clock":
            manifest = AdventureManifest.model_validate(
                state["adventure_manifests"][selected["collection"]]
            )
            definition = next(
                clock for clock in manifest.clocks if clock.canonical_id == outcome.target_id
            )
            prior = max((fact.quantity or 0 for fact in previous), default=0)
            if prior >= (definition.clock_max or 0):
                continue  # An already completed clock is an idempotent no-op.
        elif previous and previous[-1].status == outcome.status:
            selected["existing_effects"].append(previous[-1])
            continue  # Repeating an already committed outcome is an idempotent no-op.
        expected_effect_count += 1
        # Effect evidence is exact and authored for this outcome. In particular, an essential
        # reveal cannot borrow another public span or silently reveal a different entity.
        if outcome.kind == "discover":
            manifest = AdventureManifest.model_validate(
                state["adventure_manifests"][selected["collection"]]
            )
            target_passage_ids = {
                passage.passage_id
                for passage in manifest.passages
                if passage.audience == "player" and outcome.target_id in passage.entity_ids
            }
            evidence_passages = [pid for pid in outcome.passage_ids if pid in refs_by_passage]
            if not evidence_passages:
                evidence_passages = [pid for pid in policy.passage_ids if pid in target_passage_ids]
        else:
            evidence_passages = outcome.passage_ids
        evidence_ids = [
            refs_by_passage[passage_id]["reference_id"]
            for passage_id in evidence_passages
            if passage_id in refs_by_passage
            and refs_by_passage[passage_id].get("audience") == "player"
        ]
        if not evidence_ids:
            selected["generation_failed"] = True
            selected["expected_effect_count"] = expected_effect_count
            return [], []
        quantity = None
        status = outcome.status
        if outcome.kind == "clock":
            prior = max(
                (
                    fact.quantity or 0
                    for fact in facts
                    if fact.kind == "clock" and fact.target == target
                ),
                default=0,
            )
            manifest = AdventureManifest.model_validate(
                state["adventure_manifests"][selected["collection"]]
            )
            definition = next(
                clock for clock in manifest.clocks if clock.canonical_id == outcome.target_id
            )
            quantity = min(prior + (outcome.quantity_delta or 0), definition.clock_max or 0)
            status = "completed" if quantity == definition.clock_max else "advanced"
            if quantity <= prior:
                continue
        effect = StoryEffect(
            kind=outcome.kind,
            target=target,
            status=status,
            source_reference_ids=evidence_ids[:12],
            quantity=quantity,
            destination=outcome.destination,
            operation=outcome.operation,
        )
        candidate_effects.append(effect)
        if outcome.kind == "discover":
            authorizations.append(
                {
                    "effect_key": policy_effect_key(effect),
                    "passage_ids": outcome.passage_ids,
                }
            )
        selected.setdefault("authorized_effect_keys", []).append(policy_effect_key(effect))
    selected["expected_effect_count"] = expected_effect_count
    return candidate_effects, authorizations


def explicit_recall_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:reread|re-read|read\s+(?:it|that|the\s+(?:clue|note|letter|message|inscription))?\s*again|quote|repeat|recall|what\s+(?:did|was)\s+.{0,80}\s+(?:say|read|write|written))\b",
            text,
            re.IGNORECASE,
        )
    )


def exact_recall_fact(state: dict, parsed: ParsedInput | None) -> dict | None:
    if parsed is None or not explicit_recall_request(state.get("player_input", "")):
        return None
    facts = StoryState.model_validate(state.get("story_state") or {}).facts
    earned_clues = [
        (fact, _valid_public_source(fact, state))
        for fact in facts
        if fact.kind == "discover" and fact.status == "discovered" and fact.player_text
    ]
    earned_clues = [(fact, ref) for fact, ref in earned_clues if ref is not None]
    requested_id = parsed.recall_fact_id
    selected = next(
        ((fact, ref) for fact, ref in earned_clues if fact.fact_id == requested_id), None
    )
    text = (state.get("player_input") or "").casefold()
    if selected is None:
        for fact, ref in earned_clues:
            labels = [fact.display_name, ref.get("source", "")]
            if any(label and label.casefold() in text for label in labels):
                selected = (fact, ref)
                break
    # A lone clue can satisfy only an explicitly generic request that names a clue-like item.
    # Targeted requests such as “what did the guard say?” need a parser-selected or matching fact.
    if (
        selected is None
        and len(earned_clues) == 1
        and re.search(r"\b(?:clue|note|letter|message|inscription|writing)\b", text)
    ):
        selected = earned_clues[0]
    if selected is None:
        return None
    fact, public_ref = selected
    return {
        "fact_id": fact.fact_id,
        "text": fact.player_text,
        "source": public_ref.get("source") or "earned clue",
        "page": public_ref.get("page"),
        "citation": public_ref,
    }
