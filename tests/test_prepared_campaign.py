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
from edgar_core.story import (
    StoryEffect,
    StoryFact,
    StoryRef,
    accept_story_effect,
    story_target_id,
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
        acts=[
            PreparedAct(
                act_id="act_one",
                title="The rescue",
                order=1,
                scene_ids=["dock"],
                provenance=provenance,
            )
        ],
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
                policy_selections=[
                    PreparedPolicySelection(policy_id="rescue_check", result="success")
                ],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="negotiation_partial",
                branch_id="negotiate",
                result="partial",
                policy_selections=[
                    PreparedPolicySelection(policy_id="rescue_check", result="failure")
                ],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="fight_success",
                branch_id="fight",
                result="success",
                policy_selections=[
                    PreparedPolicySelection(policy_id="rescue_check", result="success")
                ],
                provenance=provenance,
            ),
            PreparedOutcome(
                outcome_id="fight_partial",
                branch_id="fight",
                result="partial",
                policy_selections=[
                    PreparedPolicySelection(policy_id="rescue_check", result="failure")
                ],
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
    assert (
        project_prepared_extension_chunks(
            approved, scene_id="dock", world_flags={}, audience="player"
        )
        == []
    )
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
    assert "amendment_rewrites_irreversible_fact" in {row.code for row in rejected.report.findings}


def test_ordinary_play_amendment_is_engine_accepted_and_replayed_in_future_context():
    from agent.prepared_amendments import (
        accept_runtime_amendment,
        amendment_prompt_block,
        prepared_runtime_context,
    )

    manifest, prepared = _fixture()
    reviewed = _approved(manifest, prepared)
    state = {
        "manifest_status": "reviewed",
        "current_scene_id": "dock",
        "adventure_manifests": {"sample.pdf": reviewed.model_dump(mode="json")},
        "prepared_campaign_overlays": {},
        "world_flags": {"gate_open": "yes"},
        "story_state": {"facts": []},
    }
    amendment = _additive_amendment(prepared)
    receipt = accept_runtime_amendment(amendment, state)
    assert receipt is not None
    assert receipt["base_prepared_sha256"] == prepared_digest(prepared)
    assert receipt["manifest_sha256"] == reviewed.reviewed_sha256
    assert prepared_digest(reviewed.prepared) == prepared_digest(prepared)

    from apps.api.services.prepared_amendments import (
        PreparedAmendmentConflict,
        revalidate_runtime_amendment,
    )

    assert revalidate_runtime_amendment(
        receipt, state=state, manifests=state["adventure_manifests"]
    ) == receipt
    with pytest.raises(PreparedAmendmentConflict, match="no longer matches"):
        revalidate_runtime_amendment(
            receipt,
            state={**state, "world_flags": {"gate_open": "no"}},
            manifests=state["adventure_manifests"],
        )

    from agent.nodes.rules_adjudicator import build_adjudicator_prompt

    planner_prompt = build_adjudicator_prompt(state, None)[-1]["content"]
    assert "proposed_prepared_amendment" in planner_prompt
    assert receipt["base_prepared_sha256"] in planner_prompt

    accepted = validate_prepared_amendment(reviewed, prepared, amendment, [], [])
    state["prepared_campaign_overlays"] = {"sample.pdf": accepted.amended.model_dump(mode="json")}
    future = prepared_runtime_context(state)
    assert future[0]["prepared_sha256"] == receipt["prepared_sha256"]
    assert "unexpected_plan" in {branch["branch_id"] for branch in future[0]["branches"]}
    prompt = amendment_prompt_block(state)
    assert "Try an unanticipated plan." in prompt
    assert receipt["prepared_sha256"] in prompt

    stale = amendment.model_copy(update={"base_prepared_sha256": "f" * 64})
    assert accept_runtime_amendment(stale, {**state, "prepared_campaign_overlays": {}}) is None

    from agent.models.adjudication import AdjudicationResult
    from apps.api.services.turn_runner import public_adjudication

    public = public_adjudication(
        AdjudicationResult(
            success=True,
            proposed_prepared_amendment=amendment,
            accepted_prepared_amendment=receipt,
        )
    )
    assert "proposed_prepared_amendment" not in public
    assert "accepted_prepared_amendment" not in public


def test_runtime_amendment_rejects_a_reversal_of_a_committed_objective():
    from agent.prepared_amendments import accept_runtime_amendment

    manifest, prepared = _fixture()
    reviewed = _approved(manifest, prepared)
    effect = accept_story_effect(
        StoryEffect(
            kind="objective",
            target=StoryRef(
                collection="sample.pdf",
                source_sha256=reviewed.source_sha256,
                manifest_sha256=reviewed.reviewed_sha256,
                kind="objective",
                canonical_id="rescue_keeper",
            ),
            status="completed",
            source_reference_ids=["source_intro"],
        ),
        turn_id="turn-committed",
        source_refs=[
            {
                "reference_id": "a" * 64,
                "collection": "sample.pdf",
                "source_sha256": reviewed.source_sha256,
                "extraction_sha256": reviewed.extraction_sha256,
                "manifest_sha256": reviewed.reviewed_sha256,
                "passage_id": "source_intro",
                "audience": "player",
                "page": 1,
            }
        ],
    )
    fact = StoryFact(**effect.model_dump(), fact_id="b" * 64)
    state = {
        "manifest_status": "reviewed",
        "current_scene_id": "dock",
        "adventure_manifests": {"sample.pdf": reviewed.model_dump(mode="json")},
        "world_flags": {"gate_open": "yes"},
        "story_state": {"facts": [fact.model_dump(mode="json")]},
    }
    assert accept_runtime_amendment(_additive_amendment(prepared), state) is None


def _actor_bound_amendment_fixture():
    manifest, prepared = _fixture()
    body = manifest.model_dump()
    body["entities"] = [{"entity_id": "guard_eli", "kind": "actor", "title": "Guard Eli"}]
    body["passages"][0]["entity_ids"] = ["guard_eli"]
    body["resolution_policies"].append(
        {
            "policy_id": "guard_check",
            "action": "Confront Guard Eli",
            "scene_ids": ["dock"],
            "passage_ids": ["source_intro"],
            "requires": {"gate_open": "yes"},
            "mode": "check",
            "check": "athletics",
            "dc": 12,
            "failure_mode": "partial",
            "on_success": [
                {
                    "kind": "encounter",
                    "target_id": "guard_eli",
                    "status": "encountered",
                    "passage_ids": ["source_intro"],
                }
            ],
            "on_failure": [
                {
                    "kind": "encounter",
                    "target_id": "guard_eli",
                    "status": "encountered",
                    "passage_ids": ["source_intro"],
                }
            ],
        }
    )
    reviewed = _approved(AdventureManifest.model_validate(body), prepared)
    amendment_body = _additive_amendment(prepared).model_dump()
    amendment_body["branches"][0]["policy_ids"].append("guard_check")
    amendment_body["outcomes"][0]["policy_selections"].append(
        {"policy_id": "guard_check", "result": "success"}
    )
    amendment_body["outcomes"][1]["policy_selections"].append(
        {"policy_id": "guard_check", "result": "failure"}
    )
    amendment = PreparedAmendment.model_validate(amendment_body)
    return reviewed, prepared, amendment


@pytest.mark.parametrize(
    ("disposition", "expected_status"),
    [("dead", "dead"), ("captured", "captive")],
)
def test_runtime_amendment_preserves_manifest_bound_combatant_outcomes(
    disposition, expected_status
):
    from agent.prepared_amendments import _committed_facts

    reviewed, prepared, amendment = _actor_bound_amendment_fixture()
    state = {
        "world_flags": {"gate_open": "yes"},
        "story_state": {"facts": []},
        "resolved_combat_outcomes": [
            {
                "id": 73,
                # Row-level outcome is not a canonical entity fate.
                "outcome": "victory",
                "combatants": [
                    {
                        "name": "Guard Eli",
                        "is_player": False,
                        "disposition": disposition,
                        "source_reference": {
                            "actor_id": "guard_eli",
                            "actor_collection": "sample.pdf",
                            "actor_manifest_sha256": reviewed.reviewed_sha256,
                        },
                    }
                ],
            }
        ],
    }

    facts = _committed_facts(state, "sample.pdf", reviewed)
    actor_fact = next(row for row in facts if row.target_id == "guard_eli")
    assert actor_fact.status == expected_status and actor_fact.irreversible
    checked = validate_prepared_amendment(reviewed, prepared, amendment, facts, [])
    assert not checked.valid
    assert "amendment_rewrites_irreversible_fact" in {
        finding.code for finding in checked.report.findings
    }


def test_combat_fate_requires_the_exact_actor_manifest_binding():
    from agent.prepared_amendments import _committed_facts

    reviewed, _, _ = _actor_bound_amendment_fixture()
    state = {
        "world_flags": {},
        "story_state": {"facts": []},
        "resolved_combat_outcomes": [
            {
                "id": 73,
                "outcome": "victory",
                "combatants": [
                    {
                        "name": "Guard Eli",
                        "is_player": False,
                        "disposition": "dead",
                        "source_reference": {
                            "actor_id": "guard_eli",
                            "actor_collection": "other.pdf",
                            "actor_manifest_sha256": reviewed.reviewed_sha256,
                        },
                    },
                    {
                        "name": "Another guard",
                        "is_player": False,
                        "disposition": "dead",
                        "source_reference": {
                            "actor_id": "guard_eli",
                            "actor_collection": "sample.pdf",
                            "actor_manifest_sha256": "f" * 64,
                        },
                    },
                    {
                        "name": "Player",
                        "is_player": True,
                        "disposition": "dead",
                        "source_reference": {
                            "actor_id": "guard_eli",
                            "actor_collection": "sample.pdf",
                            "actor_manifest_sha256": reviewed.reviewed_sha256,
                        },
                    },
                ],
            }
        ],
    }
    assert not any(
        row.kind == "entity" and row.target_id == "guard_eli"
        for row in _committed_facts(state, "sample.pdf", reviewed)
    )


def test_released_story_fact_advances_the_immutable_combat_capture_snapshot():
    from agent.prepared_amendments import _committed_facts
    from apps.api.services.story_state import combat_outcome_target

    reviewed, _, _ = _actor_bound_amendment_fixture()
    target = combat_outcome_target(73, "Guard Eli")
    effect = accept_story_effect(
        StoryEffect(kind="encounter", target=target, status="released"),
        turn_id="turn-release",
        source_refs=[
            {
                "reference_id": "a" * 64,
                "audience": "engine",
                "combat_id": 73,
                "combatant_id": "Guard Eli",
            }
        ],
    )
    release = StoryFact(
        **effect.model_dump(), fact_id=story_target_id(target)
    ).model_dump()
    state = {
        "world_flags": {},
        "story_state": {"facts": [release]},
        "resolved_combat_outcomes": [
            {
                "id": 73,
                "outcome": "victory",
                "combatants": [
                    {
                        "name": "Guard Eli",
                        "is_player": False,
                        "disposition": "captured",
                        "source_reference": {
                            "actor_id": "guard_eli",
                            "actor_collection": "sample.pdf",
                            "actor_manifest_sha256": reviewed.reviewed_sha256,
                        },
                    }
                ],
            }
        ],
    }

    facts = _committed_facts(state, "sample.pdf", reviewed)
    actor_fact = next(row for row in facts if row.target_id == "guard_eli")
    assert actor_fact.status == "released" and actor_fact.irreversible


def test_completed_outcomes_ignore_story_facts_from_another_source_version():
    from agent.prepared_amendments import _completed_outcomes

    manifest, prepared = _fixture()
    reviewed = _approved(manifest, prepared)

    def objective_fact(collection: str, source_sha256: str, manifest_sha256: str) -> dict:
        target = StoryRef(
            collection=collection,
            source_sha256=source_sha256,
            manifest_sha256=manifest_sha256,
            kind="objective",
            canonical_id="rescue_keeper",
        )
        effect = accept_story_effect(
            StoryEffect(kind="objective", target=target, status="completed"),
            turn_id=f"turn-{collection}",
            source_refs=[],
        )
        return StoryFact(**effect.model_dump(), fact_id=story_target_id(target)).model_dump()

    foreign_facts = [
        objective_fact("other.pdf", reviewed.source_sha256, reviewed.reviewed_sha256),
        objective_fact("sample.pdf", "3" * 64, reviewed.reviewed_sha256),
        objective_fact("sample.pdf", reviewed.source_sha256, "4" * 64),
    ]
    for foreign_fact in foreign_facts:
        assert _completed_outcomes(
            prepared, reviewed, "sample.pdf", {"story_state": {"facts": [foreign_fact]}}
        ) == []

    current_source = {
        "facts": [
            objective_fact(
                "sample.pdf", reviewed.source_sha256, reviewed.reviewed_sha256
            )
        ]
    }
    completed = _completed_outcomes(
        prepared, reviewed, "sample.pdf", {"story_state": current_source}
    )
    assert {"negotiation_success", "fight_success"} <= set(completed)


async def test_session_amendment_event_replay_is_private_and_source_version_bound(db_session):
    from apps.api.services.prepared_amendments import (
        append_session_prepared_amendment,
        load_session_prepared_amendments,
    )
    from db.postgres.models import Campaign, Session, User

    manifest, prepared = _fixture()
    reviewed = _approved(manifest, prepared)
    state = {
        "manifest_status": "reviewed",
        "current_scene_id": "dock",
        "adventure_manifests": {"sample.pdf": reviewed.model_dump(mode="json")},
        "world_flags": {"gate_open": "yes"},
        "story_state": {"facts": []},
    }
    from agent.prepared_amendments import accept_runtime_amendment

    receipt = accept_runtime_amendment(_additive_amendment(prepared), state)
    assert receipt is not None
    user = User(email="amendment@example.test")
    db_session.add(user)
    await db_session.flush()
    campaign = Campaign(title="Amendment", system="5e", created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    session_a = Session(campaign_id=campaign.id)
    session_b = Session(campaign_id=campaign.id)
    db_session.add_all([session_a, session_b])
    await db_session.flush()

    await append_session_prepared_amendment(
        db_session,
        session_id=session_a.id,
        game_session=session_a,
        receipt=receipt,
        turn_id="turn-one",
    )
    await db_session.commit()
    overlays, events = await load_session_prepared_amendments(
        db_session,
        session_a.id,
        campaign.id,
        {"sample.pdf": reviewed.model_dump(mode="json")},
    )
    assert overlays["sample.pdf"]["branches"][-1]["branch_id"] == "unexpected_plan"
    assert events[0]["turn_id"] == "turn-one"
    other_overlays, other_events = await load_session_prepared_amendments(
        db_session,
        session_b.id,
        campaign.id,
        {"sample.pdf": reviewed.model_dump(mode="json")},
    )
    assert other_overlays == {}
    assert other_events == []
    changed_data = prepared.model_dump()
    changed_data["acts"][0]["title"] = "A revised published act"
    changed = _approved(manifest, PreparedCampaign.model_validate(changed_data))
    from edgar_core.manifests import require_published

    require_published(changed)  # A valid new publication must still reject the old event chain.
    from apps.api.services.prepared_amendments import PreparedAmendmentConflict

    with pytest.raises(PreparedAmendmentConflict, match="source or base version is stale"):
        await load_session_prepared_amendments(
            db_session,
            session_a.id,
            campaign.id,
            {"sample.pdf": changed.model_dump(mode="json")},
        )


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


def test_outcome_label_cannot_reverse_the_selected_engine_result():
    manifest, prepared = _fixture()
    data = prepared.model_dump()
    data["outcomes"][0]["policy_selections"][0]["result"] = "failure"
    report = validate_prepared_campaign(manifest, data)
    assert "outcome_result_mismatch" in {row.code for row in report.findings}


def test_scene_packet_entry_gate_must_have_a_runtime_contract():
    manifest, prepared = _fixture()
    data = prepared.model_dump()
    data["scene_packets"][0]["entry_requires"] = {"invented_gate": "yes"}
    report = validate_prepared_campaign(manifest, data)
    assert "unbacked_world_flag" in {row.code for row in report.findings}
