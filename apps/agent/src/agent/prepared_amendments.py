"""Engine acceptance and session-local context for bounded prepared amendments.

The model can suggest a small additive branch after an unexpected plausible action. This
module stamps acceptance only after the exact reviewed source, prepared digest, active scene,
committed story ledger and engine world flags all validate. Persistence and replay live in
the API service layer; this module never writes campaign data.
"""

from __future__ import annotations

import json

from agent.state import AgentState
from edgar_core.manifests import require_published
from edgar_core.prepared import (
    CommittedPreparedFact,
    PreparedAmendment,
    PreparedCampaign,
    prepared_digest,
    validate_prepared_amendment,
)
from edgar_core.story import StoryState

_STORY_KIND = {
    "discover": "entity",
    "encounter": "entity",
    "relationship": "entity",
    "objective": "objective",
    "milestone": "milestone",
    "clock": "clock",
    "acquire": "object",
    "consume": "object",
    "transfer": "object",
    "use": "object",
    "environment": "object",
    "move": "scene",
    "visit": "scene",
    "learn_place": "scene",
}
_TERMINAL_STATUSES = {"completed", "failed", "fulfilled", "broken", "dead", "released"}
_MAX_CONTEXT_BRANCHES = 12
_MAX_CONTEXT_OUTCOMES = 24


def _manifest_prepared(state: AgentState, collection: str, raw_manifest: dict):
    manifest = require_published(raw_manifest)
    overlay = (state.get("prepared_campaign_overlays") or {}).get(collection)
    prepared = (
        PreparedCampaign.model_validate(overlay) if overlay is not None else manifest.prepared
    )
    if prepared is None:
        return manifest, None
    return manifest, prepared


def prepared_runtime_context(state: AgentState) -> list[dict]:
    """Return only current-scene branches whose predicates match committed world state."""
    current_scene = state.get("current_scene_id")
    if not current_scene:
        return []
    flags = state.get("world_flags") or {}
    contexts = []
    for collection, raw_manifest in (state.get("adventure_manifests") or {}).items():
        try:
            manifest, prepared = _manifest_prepared(state, collection, raw_manifest)
        except (TypeError, ValueError):
            continue
        if prepared is None:
            continue
        by_id = {item.outcome_id: item for item in prepared.outcomes}
        quests = {item.quest_id: item for item in prepared.quests}
        policies = {item.policy_id: item for item in manifest.resolution_policies}
        branches = [
            item
            for item in prepared.branches
            if item.scene_id == current_scene
            and all(flags.get(key) == value for key, value in item.requires.items())
            and item.quest_id in quests
            and all(
                flags.get(key) == value
                for key, value in quests[item.quest_id].prerequisites.items()
            )
        ][:_MAX_CONTEXT_BRANCHES]
        outcomes = []
        for branch in branches:
            for outcome_id in branch.outcome_ids:
                outcome = by_id.get(outcome_id)
                if outcome is None or not all(
                    flags.get(key) == value for key, value in outcome.requires.items()
                ):
                    continue
                selected_policies = [
                    policies.get(selection.policy_id) for selection in outcome.policy_selections
                ]
                if any(
                    policy is None
                    or current_scene not in policy.scene_ids
                    or not all(flags.get(key) == value for key, value in policy.requires.items())
                    for policy in selected_policies
                ):
                    continue
                outcomes.append(
                    {
                        "outcome_id": outcome.outcome_id,
                        "branch_id": outcome.branch_id,
                        "result": outcome.result,
                        "policy_selections": [
                            selection.model_dump(mode="json")
                            for selection in outcome.policy_selections
                        ],
                        "compatible_with": list(outcome.compatible_with),
                    }
                )
                if len(outcomes) >= _MAX_CONTEXT_OUTCOMES:
                    break
            if len(outcomes) >= _MAX_CONTEXT_OUTCOMES:
                break
        extensions = [
            {
                "extension_id": item.extension_id,
                "passage_id": item.passage_id,
                "audience": item.audience,
                "text": item.text,
                "requires": item.requires,
                "provenance_origin": item.provenance.origin,
                "provenance_passage_ids": item.provenance.passage_ids,
                "provenance_rationale": item.provenance.rationale,
            }
            for item in prepared.extensions
            if current_scene in item.scene_ids
            and all(flags.get(key) == value for key, value in item.requires.items())
        ][:8]
        reviewed_policies = [
            {
                "policy_id": policy.policy_id,
                "action": policy.action,
                "scene_ids": policy.scene_ids,
                "requires": policy.requires,
                "mode": policy.mode,
                "check": policy.check,
                "failure_mode": policy.failure_mode,
                "effects": {
                    "automatic": [
                        {"kind": effect.kind, "target_id": effect.target_id, "status": effect.status}
                        for effect in policy.on_automatic
                    ],
                    "success": [
                        {"kind": effect.kind, "target_id": effect.target_id, "status": effect.status}
                        for effect in policy.on_success
                    ],
                    "failure": [
                        {"kind": effect.kind, "target_id": effect.target_id, "status": effect.status}
                        for effect in policy.on_failure
                    ],
                },
            }
            for policy in manifest.resolution_policies
            if current_scene in policy.scene_ids
            and all(flags.get(key) == value for key, value in policy.requires.items())
        ][:24]
        contexts.append(
            {
                "collection": collection,
                "source_sha256": manifest.source_sha256,
                "manifest_sha256": manifest.reviewed_sha256,
                "prepared_sha256": prepared_digest(prepared),
                "completed_outcome_ids": _completed_outcomes(prepared, manifest, state),
                "branches": [
                    {
                        "branch_id": branch.branch_id,
                        "quest_id": branch.quest_id,
                        "scene_id": branch.scene_id,
                        "approach": branch.approach,
                        "action": branch.action,
                        "requires": branch.requires,
                        "policy_ids": branch.policy_ids,
                        "outcome_ids": branch.outcome_ids,
                    }
                    for branch in branches
                ],
                "outcomes": outcomes,
                "extensions": extensions,
                "reviewed_policies": reviewed_policies,
            }
        )
    return contexts


def _committed_facts(state: AgentState, manifest) -> list[CommittedPreparedFact]:
    facts: list[CommittedPreparedFact] = []
    for key, value in sorted((state.get("world_flags") or {}).items()):
        facts.append(
            CommittedPreparedFact(
                kind="world_flag", target_id=key, status=value, value=value, irreversible=True
            )
        )
    passage_by_id = {row.passage_id: row for row in manifest.passages}
    story = StoryState.model_validate(state.get("story_state") or {}).facts
    for item in story:
        target_kind = _STORY_KIND.get(item.kind)
        if target_kind:
            passage_ids = sorted(
                {
                    ref["passage_id"]
                    for ref in item.source_refs
                    if isinstance(ref.get("passage_id"), str)
                    and ref.get("collection") == item.target.collection
                    and ref.get("manifest_sha256") == item.target.manifest_sha256
                }
            )
            facts.append(
                CommittedPreparedFact(
                    kind=target_kind,
                    target_id=item.target.canonical_id,
                    status=item.status,
                    value=(str(item.quantity) if item.quantity is not None else None),
                    passage_ids=passage_ids[:32],
                    irreversible=item.status in _TERMINAL_STATUSES
                    or item.kind in {"consume", "transfer"},
                )
            )
        for ref in item.source_refs:
            passage_id = ref.get("passage_id")
            if (
                not isinstance(passage_id, str)
                or ref.get("collection") != item.target.collection
                or ref.get("manifest_sha256") != item.target.manifest_sha256
            ):
                continue
            passage = passage_by_id.get(passage_id)
            if passage is not None:
                facts.append(
                    CommittedPreparedFact(
                        kind="passage",
                        target_id=passage_id,
                        status="revealed",
                        text_sha256=passage.text_sha256,
                        irreversible=True,
                    )
                )
    # Preserve encounter resolutions the combat engine has already ended. These facts are
    # deliberately engine-derived; an adjudication proposal cannot create them.
    for row in state.get("resolved_combat_outcomes") or []:
        identity = row.get("id") or row.get("combat_id")
        outcome = row.get("outcome")
        if isinstance(identity, str) and outcome in _TERMINAL_STATUSES:
            facts.append(
                CommittedPreparedFact(
                    kind="entity", target_id=identity, status=outcome, irreversible=True
                )
            )
    return facts


def _completed_outcomes(prepared: PreparedCampaign, manifest, state: AgentState) -> list[str]:
    """Derive achieved prepared outcome IDs from existing engine effects and flags."""
    from edgar_core.story import StoryState

    facts = StoryState.model_validate(state.get("story_state") or {}).facts
    policies = {row.policy_id: row for row in manifest.resolution_policies}
    done: list[str] = []
    for outcome in prepared.outcomes:
        expected = []
        for selection in outcome.policy_selections:
            policy = policies.get(selection.policy_id)
            if policy is None:
                expected = []
                break
            effects = (
                policy.on_automatic
                if selection.result == "automatic"
                else policy.on_success
                if selection.result == "success"
                else policy.on_failure
            )
            expected.extend(effects)
        if not expected:
            continue
        if all(
            any(
                fact.kind == expected_effect.kind
                and fact.target.canonical_id == expected_effect.target_id
                and fact.status == expected_effect.status
                for fact in facts
            )
            for expected_effect in expected
        ):
            done.append(outcome.outcome_id)
    return done


def accept_runtime_amendment(
    proposal: PreparedAmendment | dict | None,
    state: AgentState,
    *,
    in_combat: bool = False,
) -> dict | None:
    """Return a code-sealed amendment receipt, or discard an invalid model suggestion."""
    if proposal is None or in_combat or state.get("manifest_status") != "reviewed":
        return None
    try:
        amendment = (
            proposal
            if isinstance(proposal, PreparedAmendment)
            else PreparedAmendment.model_validate(proposal)
        )
        current_scene = state.get("current_scene_id")
        candidates = []
        for collection, raw_manifest in (state.get("adventure_manifests") or {}).items():
            manifest, prepared = _manifest_prepared(state, collection, raw_manifest)
            if (
                prepared is not None
                and current_scene in {scene.scene_id for scene in manifest.scenes}
                and any(branch.scene_id == current_scene for branch in amendment.branches)
            ):
                candidates.append((collection, manifest, prepared))
        if len(candidates) != 1:
            return None
        collection, manifest, prepared = candidates[0]
        # One action can add one compact approach with its success and failure/partial paths.
        # It cannot install a campaign plan or make unbounded narrative facts during play.
        if (
            len(amendment.branches) != 1
            or len(amendment.outcomes) != 2
            or len(amendment.extensions) > 1
            or amendment.branches[0].scene_id != current_scene
            or amendment.branches[0].approach != "unexpected"
        ):
            return None
        facts = _committed_facts(state, manifest)
        completed = _completed_outcomes(prepared, manifest, state)
        checked = validate_prepared_amendment(manifest, prepared, amendment, facts, completed)
        if not checked.valid or checked.amended is None:
            return None
        return {
            "collection": collection,
            "source_sha256": manifest.source_sha256,
            "manifest_sha256": manifest.reviewed_sha256,
            "base_prepared_sha256": prepared_digest(prepared),
            "prepared_sha256": prepared_digest(checked.amended),
            "amendment": amendment.model_dump(mode="json"),
            "committed_facts": [fact.model_dump(mode="json") for fact in facts],
            "completed_outcomes": completed,
        }
    except (TypeError, ValueError, KeyError):
        return None


def amendment_prompt_block(state: AgentState) -> str:
    """Private instruction and exact digest for the existing consequence planner."""
    contexts = prepared_runtime_context(state)
    if not contexts:
        return ""
    active = contexts
    block = [
        "Private prepared-play context for this scene (engine accepted definitions only):",
        json.dumps(active, ensure_ascii=False, sort_keys=True),
        "Use an authored branch when it fits. If the player's plausible action has no fit, you may propose one bounded additive amendment: exactly one unexpected branch in this current scene, exactly two outcomes (success plus failure or partial), existing reviewed policy IDs only, and at most one DM extension. Planner-audience extension text is private guidance; player-audience text is usable only after its exact committed flag gates match. Preserve every committed fact and completed outcome. Never propose statistics, rolls, facts already established, or rewrite quoted source text. Set base_prepared_sha256 to the exact digest above. The engine will validate and may discard the proposal; do not claim it was accepted.",
    ]
    return "\n".join(block)


__all__ = [
    "accept_runtime_amendment",
    "amendment_prompt_block",
    "prepared_runtime_context",
]
