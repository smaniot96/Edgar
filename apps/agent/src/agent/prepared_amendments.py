"""Compatibility validation for amendment receipts retained in older saved rulings.

Live play has no amendment proposal, branch-generation context or amendment prompt. The API
still imports this validator for receipt checks; saved overlay replay remains in its service.
"""

from __future__ import annotations

from agent.committed_facts import committed_prepared_facts as _committed_facts
from agent.state import AgentState
from edgar_core.manifests import require_published
from edgar_core.prepared import (
    PreparedAmendment,
    PreparedCampaign,
    prepared_digest,
    validate_prepared_amendment,
)
from edgar_core.story import StoryState


def _manifest_prepared(state: AgentState, collection: str, raw_manifest: dict):
    manifest = require_published(raw_manifest)
    overlay = (state.get("prepared_campaign_overlays") or {}).get(collection)
    prepared = (
        PreparedCampaign.model_validate(overlay) if overlay is not None else manifest.prepared
    )
    if prepared is None:
        return manifest, None
    return manifest, prepared


def _completed_outcomes(
    prepared: PreparedCampaign, manifest, collection: str, state: AgentState
) -> list[str]:
    """Derive achieved prepared outcomes only from facts bound to this manifest version."""
    facts = StoryState.model_validate(state.get("story_state") or {}).facts
    facts = [
        fact
        for fact in facts
        if fact.target.collection == collection
        and fact.target.source_sha256 == manifest.source_sha256
        and fact.target.manifest_sha256 == manifest.reviewed_sha256
    ]
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
    """Revalidate a retained receipt; the live agent never proposes or calls this tool."""
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
        # Preserve the original receipt contract when validating an older saved ruling.
        if (
            len(amendment.branches) != 1
            or len(amendment.outcomes) != 2
            or len(amendment.extensions) > 1
            or amendment.branches[0].scene_id != current_scene
            or amendment.branches[0].approach != "unexpected"
        ):
            return None
        facts = _committed_facts(state, collection, manifest)
        completed = _completed_outcomes(prepared, manifest, collection, state)
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


__all__ = ["accept_runtime_amendment"]
