"""Deterministic tests for the private prepared-campaign contract."""

from copy import deepcopy

import pytest

from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, ManifestError, manifest_digest
from edgar_core.prepared import (
    CommittedPreparedFact,
    PreparedAct,
    PreparedAmendment,
    PreparedBranch,
    PreparedCampaign,
    PreparedExtension,
    PreparedOutcome,
    PreparedPolicySelection,
    PreparedProvenance,
    PreparedQuest,
    PreparedRoute,
    PreparedScenePacket,
    prepared_digest,
    project_prepared_extension_chunks,
    validate_prepared_amendment,
    validate_prepared_campaign,
)


def _fixture(source: str = "sample.pdf") -> tuple[AdventureManifest, PreparedCampaign]:
    passage = {
        "passage_id": "source_intro",
        "page": 1,
        "start": 0,
        "end": 20,
        "text_sha256": text_digest("The keeper is in danger."),
        "scene_ids": ["dock"],
        "entity_ids": [],
        "audience": "player",
    }
    objective = {
        "canonical_id": "rescue_keeper",
        "title": "Rescue the keeper",
        "scene_ids": ["dock"],
        "passage_ids": ["source_intro"],
        "statuses": ["active", "completed", "failed"],
    }
    manifest_data = {
        "source": source,
        "source_sha256": "1" * 64,
        "extraction_sha256": "2" * 64,
        "rules_edition": "2014",
        "opening_scene_id": "dock",
        "scenes": [
            {
                "scene_id": "dock",
                "title": "The ferry dock",
                "links": [{"scene_id": "dock", "requires": {"gate_open": "yes"}}],
            }
        ],
        "passages": [passage],
        "objectives": [objective],
        "resolution_policies": [
            {
                "policy_id": "rescue_check",
                "action": "Convince or outmaneuver the guards",
                "scene_ids": ["dock"],
                "passage_ids": ["source_intro"],
                "requires": {"gate_open": "yes"},
                "mode": "check",
                "check": "persuasion",
                "dc": 12,
                "failure_mode": "partial",
                "on_success": [
                    {
                        "kind": "objective",
                        "target_id": "rescue_keeper",
                        "status": "completed",
                        "passage_ids": ["source_intro"],
                    }
                ],
                "on_failure": [
                    {
                        "kind": "objective",
                        "target_id": "rescue_keeper",
                        "status": "failed",
                        "passage_ids": ["source_intro"],
                    }
                ],
            }
        ],
    }
    manifest = AdventureManifest.model_validate(manifest_data)
    provenance = PreparedProvenance(origin="source", passage_ids=["source_intro"])
    dm_provenance = PreparedProvenance(
        origin="dm_extension", passage_ids=["source_intro"], rationale="Add a private reminder."
    )
    prepared = PreparedCampaign(
        source_mode="pdf" if source.endswith(".pdf") else "ai",
        acts=[PreparedAct(act_id="act_one", title="The rescue", order=1, scene_ids=["dock"], provenance=provenance)],
        quests=[
            PreparedQuest(
                quest_id="keeper_quest",
                objective_id="rescue_keeper",
                scene_ids=["dock"],
                required_approaches=["negotiation", "combat"],
                provenance=provenance,
            )
        ],
        scene_packets=[
            PreparedScenePacket(
                scene_id="dock",
                passage_ids=["source_intro", "dm_note"],
                objective_ids=["rescue_keeper"],
                policy_ids=["rescue_check"],
                dm_notes="The guards can be negotiated with or fought.",
                provenance=provenance,
            )
        ],
        routes=[
            PreparedRoute(
                from_scene_id="dock",
                to_scene_id="dock",
                visibility="known",
                traversal_requires={"gate_open": "yes"},
                provenance=provenance,
            )
        ],
        extensions=[
            PreparedExtension(
                extension_id="keeper_note",
                passage_id="dm_note",
                scene_ids=["dock"],
                audience="player",
                requires={"gate_open": "yes"},
                text="The guards can be distracted by the ferry bell.",
                provenance=dm_provenance,
            )
        ],
        branches=[
            PreparedBranch(
                branch_id="negotiate",
                quest_id="keeper_quest",
                scene_id="dock",
                approach="negotiation",
                action="Parley with the guards.",
                policy_ids=["rescue_check"],
                outcome_ids=["negotiation_success", "negotiation_partial"],
                provenance=provenance,
            ),
            PreparedBranch(
                branch_id="fight",
                quest_id="keeper_quest",
                scene_id="dock",
                approach="combat",
                action="Defeat the guards.",
                policy_ids=["rescue_check"],
                outcome_ids=["fight_success", "fight_partial"],
                provenance=provenance,
            ),
        ],
        outcomes=[
            PreparedOutcome(
                outcome_id="negotiation_success",
                branch_id="negotiate",
                result="success",
                policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="success")],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="negotiation_partial",
                branch_id="negotiate",
                result="partial",
                policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="failure")],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="fight_success",
                branch_id="fight",
                result="success",
                policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="success")],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="fight_partial",
                branch_id="fight",
                result="partial",
                policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="failure")],
                provenance=provenance,
            ),
        ],
    )
    return manifest, prepared


def _approved(manifest: AdventureManifest, prepared: PreparedCampaign) -> AdventureManifest:
    data = manifest.model_dump()
    data["prepared"] = prepared.model_dump()
    candidate = AdventureManifest.model_validate(data)
    data = candidate.model_dump()
    data["reviewed_by"] = "Private preparation fixture"
    approved = AdventureManifest.model_validate(data)
    data = approved.model_dump()
    data["reviewed_sha256"] = manifest_digest(approved)
    return AdventureManifest.model_validate(data)


def _additive_amendment(
    prepared: PreparedCampaign,
    *,
    second_result: str = "partial",
) -> PreparedAmendment:
    provenance = PreparedProvenance(origin="source", passage_ids=["source_intro"])
    branch = PreparedBranch(
        branch_id="unexpected_plan",
        quest_id="keeper_quest",
        scene_id="dock",
        approach="unexpected",
        action="Try an unanticipated plan.",
        policy_ids=["rescue_check"],
        outcome_ids=["unexpected_success", "unexpected_other"],
        provenance=provenance,
    )
    outcomes = [
        PreparedOutcome(
            outcome_id="unexpected_success",
            branch_id="unexpected_plan",
            result="success",
            policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="success")],
            provenance=provenance,
        ),
        PreparedOutcome(
            outcome_id="unexpected_other",
            branch_id="unexpected_plan",
            result=second_result,
            policy_selections=[PreparedPolicySelection(policy_id="rescue_check", result="failure")],
            provenance=provenance,
        ),
    ]
    return PreparedAmendment(
        amendment_id="new_approach",
        base_prepared_sha256=prepared_digest(prepared),
        branches=[branch],
        outcomes=outcomes,
    )


@pytest.mark.parametrize("source,mode", [("sample.pdf", "pdf"), ("generated-manuscript", "ai")])
def test_pdf_and_ai_candidates_validate_against_the_same_manifest_contract(source, mode):
    manifest, prepared = _fixture(source)
    assert prepared.source_mode == mode
    report = validate_prepared_campaign(manifest, prepared)
    assert report.valid, report.findings
    assert report.prepared_sha256 == prepared_digest(prepared)


def test_topology_and_runtime_predicate_must_match_reviewed_routes():
    manifest, prepared = _fixture()
    missing_route = prepared.model_copy(update={"routes": []})
    report = validate_prepared_campaign(manifest, missing_route)
    assert "topology_coverage" in {row.code for row in report.findings}

    mismatched = deepcopy(prepared.model_dump())
    mismatched["routes"][0]["traversal_requires"] = {"gate_open": "no"}
    report = validate_prepared_campaign(manifest, mismatched)
    assert "route_predicate_mismatch" in {row.code for row in report.findings}


def test_outcomes_cannot_be_cross_wired_or_require_contradictory_conditions():
    manifest, prepared = _fixture()
    cross_wired = deepcopy(prepared.model_dump())
    cross_wired["branches"][0]["outcome_ids"][0] = "fight_success"
    report = validate_prepared_campaign(manifest, cross_wired)
    assert "outcome_branch_mismatch" in {row.code for row in report.findings}

    impossible = deepcopy(prepared.model_dump())
    impossible["outcomes"][0]["requires"] = {"gate_open": "no"}
    report = validate_prepared_campaign(manifest, impossible)
    assert "unreachable_outcome" in {row.code for row in report.findings}


def test_old_manifest_digest_remains_compatible_when_preparation_is_absent():
    manifest, _ = _fixture()
    old_body = manifest.model_dump()
    old_body.pop("prepared")
    old_manifest = AdventureManifest.model_validate(old_body)
    assert manifest_digest(old_manifest) == manifest_digest(manifest)


def test_extension_projection_requires_earned_gate_and_preserves_private_spans():
    manifest, prepared = _fixture()
    approved = _approved(manifest, prepared)
    assert project_prepared_extension_chunks(
        approved, scene_id="dock", world_flags={}, audience="player"
    ) == []
    visible = project_prepared_extension_chunks(
        approved, scene_id="dock", world_flags={"gate_open": "yes"}, audience="player"
    )
    assert len(visible) == 1
    chunk = visible[0]
    assert chunk["text"] == prepared.extensions[0].text
    assert chunk["source_kind"] == "dm_extension"
    assert chunk["page"] is None and chunk["source_span"] is None
    assert chunk["passage_id"] == "dm_note"


def test_amendment_is_bounded_additive_and_preserves_irreversible_facts():
    manifest, prepared = _fixture()
    amendment = _additive_amendment(prepared)
    accepted = validate_prepared_amendment(manifest, prepared, amendment, [], [])
    assert accepted.valid, accepted.report.findings
    assert len(accepted.amended.branches) == len(prepared.branches) + 1

    rejected = validate_prepared_amendment(
        manifest,
        prepared,
        _additive_amendment(prepared),
        [
            CommittedPreparedFact(
                kind="objective",
                target_id="rescue_keeper",
                status="completed",
                irreversible=True,
            )
        ],
        [],
    )
    assert not rejected.valid
    assert "amendment_rewrites_irreversible_fact" in {
        row.code for row in rejected.report.findings
    }


def test_require_published_rejects_an_unvalidated_prepared_bundle():
    manifest, prepared = _fixture()
    approved = _approved(manifest, prepared)
    data = approved.model_dump()
    data["prepared"]["routes"][0]["traversal_requires"] = {"gate_open": "no"}
    changed = AdventureManifest.model_validate(data)
    data["reviewed_sha256"] = manifest_digest(changed)
    with pytest.raises(ManifestError, match="deterministic validation"):
        from edgar_core.manifests import require_published

        require_published(data)
