"""Focused tests for version-bound whole-adventure review contracts."""

import pytest
from test_prepared_campaign import _approved, _fixture

from edgar_core.coherence import (
    ContinuityAssertion,
    CriticEvidence,
    CriticFinding,
    CriticReport,
    PathBudget,
    RepairEnvelope,
    RepairReplacement,
    RetainedChapter,
    RetainedManuscript,
    _digest,
    _inventory_names,
    build_adventure_review,
    build_path_exploration_artifact,
    build_review_bindings,
    explore_prepared_paths,
    make_deterministic_review_validator,
    make_independent_critic_validator,
    make_path_exploration_validator,
    validate_critic_report,
    validate_repair_envelope,
)
from edgar_core.manifests import (
    AdventureManifest,
    Entity,
    Link,
    PolicyEffect,
    ResolutionPolicy,
    Scene,
    StoryDefinition,
)
from edgar_core.preparation import (
    PreparationBudget,
    PreparationRunBinding,
    PreparationStage,
    build_artifact,
    canonical_sha256,
)
from edgar_core.prepared import (
    PreparedBranch,
    PreparedCampaign,
    PreparedOutcome,
    PreparedPolicySelection,
    PreparedQuest,
    PreparedRoute,
    PreparedScenePacket,
    validate_prepared_campaign,
)


def _bound_review_inventory():
    manifest, prepared = _fixture()
    manifest = _approved(manifest, prepared)
    party = {"members": [{"name": "Mira", "inventory": []}]}
    profile = {"model": "fixture", "reasoning_effort": "high"}
    commission = {"tone": "adventure"}
    binding = PreparationRunBinding(
        run_id="prep-" + "a" * 32,
        source_mode="pdf",
        source_slug="fixture",
        source_version="1",
        source_sha256="1" * 64,
        extraction_sha256="2" * 64,
        rules_edition="2014",
        commission=commission,
        commission_sha256=canonical_sha256(commission),
        hero_party_snapshot=party,
        hero_party_sha256=canonical_sha256(party),
        profile_snapshot=profile,
        profile_snapshot_sha256=canonical_sha256(profile),
        budget=PreparationBudget(
            max_provider_calls=10, max_total_tokens=10000, max_cost_microusd=1000
        ),
    )
    grounding = build_artifact(
        binding,
        stage=PreparationStage.P02,
        task_id="p02_grounding",
        artifact_kind="grounding_report",
        payload={"grounded": True},
        dependency_artifacts=(),
        created_at="2026-10-16T00:00:00Z",
    )
    stale_dependencies = tuple(
        build_artifact(
            binding,
            stage=PreparationStage.P08,
            task_id="p08_stale",
            artifact_kind=kind,
            payload=payload,
            dependency_artifacts=(grounding,),
            created_at="2026-10-15T00:00:00Z",
        )
        for kind, payload in (
            ("canonical_manifest", {"manifest": {"stale": True}}),
            ("prepared_campaign", {"prepared": {"stale": True}}),
            ("retained_manuscript", {"manuscript": {"stale": True}}),
        )
    )
    chapter_text = "Mira reaches the ferry dock and meets the keeper."
    manuscript = RetainedManuscript(
        source_mode="pdf",
        chapters=(
            RetainedChapter(
                chapter_id="chapter_one",
                act_id="act_one",
                order=1,
                role="setup",
                scene_ids=("dock",),
                advance_ids=("rescue_goal",),
                passage_ids=("source_intro",),
                text=chapter_text,
            ),
        ),
    )
    deps = (grounding, *stale_dependencies)
    common = dict(
        binding=binding,
        stage=PreparationStage.P09,
        task_id="p09_review",
        dependency_artifacts=deps,
        created_at="2026-10-16T00:00:00Z",
        scope_keys=("chapter_one",),
    )
    manifest_artifact = build_artifact(
        **common,
        artifact_kind="canonical_manifest",
        payload={"manifest": manifest.model_dump(mode="json")},
    )
    prepared_artifact = build_artifact(
        **common,
        artifact_kind="prepared_campaign",
        payload={"prepared": prepared.model_dump(mode="json")},
    )
    manuscript_artifact = build_artifact(
        **common,
        artifact_kind="retained_manuscript",
        payload={"manuscript": manuscript.model_dump(mode="json")},
    )
    subjects = (manifest_artifact, prepared_artifact, manuscript_artifact)
    additional = tuple(
        build_artifact(
            **common,
            artifact_kind=kind,
            payload=payload,
        )
        for kind, payload in (
            ("canonical_extraction", {"text": "The keeper is in danger."}),
            ("asset_coverage", {"covered": ["dock"]}),
        )
    )
    review = build_adventure_review(
        binding,
        manifest_artifact=manifest_artifact,
        prepared_artifact=prepared_artifact,
        manuscript_artifact=manuscript_artifact,
        source_artifacts=(*deps, *additional),
    )
    review_bindings = build_review_bindings(
        binding,
        manifest_artifact,
        prepared_artifact,
        manuscript_artifact,
        (*deps, *subjects, *additional),
    )
    critic = CriticReport(
        bindings=review_bindings,
        reviewed_chapter_text_sha256={"chapter_one": _digest(chapter_text)},
        findings=(),
    )
    report_artifact = build_artifact(
        **common,
        artifact_kind="deterministic_review_report",
        payload={"review_report": review.model_dump(mode="json")},
    )
    critic_artifact = build_artifact(
        **common,
        artifact_kind="semantic_critic_report",
        payload={"critic_report": critic.model_dump(mode="json")},
    )
    return (
        binding,
        deps,
        (*subjects, *additional, report_artifact, critic_artifact),
        manifest_artifact,
        prepared_artifact,
        manuscript_artifact,
    )


def test_p09_validators_select_proposed_final_artifacts_and_receipt_covers_all_proposals():
    binding, deps, proposed, *_ = _bound_review_inventory()

    deterministic = make_deterministic_review_validator()(binding, None, proposed, deps)
    critic = make_independent_critic_validator()(binding, None, proposed, deps)

    expected = {artifact.artifact_id: artifact.content_sha256 for artifact in proposed}
    assert deterministic.passed
    assert critic.passed
    assert deterministic.receipts[0].artifact_digests == expected
    assert critic.receipts[0].artifact_digests == expected


def test_path_exploration_applies_worldflag_policy_prerequisites():
    binding, dependencies, proposed, manifest, prepared, manuscript = _bound_review_inventory()
    path_dependencies = (*dependencies, *proposed)

    closed = explore_prepared_paths(binding, manifest, prepared, manuscript)
    open_gate = explore_prepared_paths(
        binding, manifest, prepared, manuscript, initial_world_flags={"gate_open": "yes"}
    )

    assert set(closed.unreachable_outcome_ids) == set(closed.expected_outcome_ids)
    assert set(open_gate.reached_outcome_ids) == set(open_gate.expected_outcome_ids)
    assert open_gate.exhaustive

    artifact = build_path_exploration_artifact(
        binding,
        dependency_artifacts=path_dependencies,
        created_at="2026-10-16T00:00:00Z",
        initial_world_flags={"gate_open": "yes"},
    )
    result = make_path_exploration_validator(initial_world_flags={"gate_open": "yes"})(
        binding, None, (artifact,), path_dependencies
    )
    assert result.passed
    assert result.receipts[0].artifact_digests == {artifact.artifact_id: artifact.content_sha256}


def _replace_artifact_payload(binding, artifact, payload):
    return build_artifact(
        binding,
        stage=artifact.stage,
        task_id=artifact.task_id,
        artifact_kind=artifact.artifact_kind,
        payload=payload,
        dependency_artifacts=(),
        created_at="2026-10-16T00:00:00Z",
        scope_keys=artifact.scope_keys,
        version=artifact.version,
    )


def test_path_exploration_requires_discovery_before_using_a_gated_route():
    binding, _, _, manifest_artifact, prepared_artifact, manuscript_artifact = (
        _bound_review_inventory()
    )
    manifest = AdventureManifest.model_validate(manifest_artifact.payload["manifest"])
    prepared = PreparedCampaign.model_validate(prepared_artifact.payload["prepared"])
    passage = manifest.passages[0].model_copy(
        update={"scene_ids": ["dock", "cave"], "entity_ids": ["cave_clue"]}
    )
    objective = manifest.objectives[0].model_copy(update={"scene_ids": ["dock", "cave"]})
    cave_objective = StoryDefinition(
        canonical_id="find_cave_keeper",
        title="Find the keeper in the cave",
        scene_ids=["cave"],
        passage_ids=["source_intro"],
        statuses=["active", "completed", "failed"],
    )
    policy = manifest.resolution_policies[0].model_copy(
        update={
            "on_success": [
                *manifest.resolution_policies[0].on_success,
                PolicyEffect(
                    kind="discover",
                    target_id="cave_clue",
                    status="discovered",
                    passage_ids=["source_intro"],
                ),
            ],
        }
    )
    cave_policy = ResolutionPolicy(
        policy_id="cave_search",
        action="Search the cave for the keeper",
        scene_ids=["cave"],
        passage_ids=["source_intro"],
        mode="check",
        check="perception",
        dc=10,
        failure_mode="partial",
        on_success=[
            PolicyEffect(
                kind="objective",
                target_id="find_cave_keeper",
                status="completed",
                passage_ids=["source_intro"],
            )
        ],
        on_failure=[
            PolicyEffect(
                kind="objective",
                target_id="find_cave_keeper",
                status="failed",
                passage_ids=["source_intro"],
            )
        ],
    )
    dock = manifest.scenes[0].model_copy(
        update={
            "links": [
                *manifest.scenes[0].links,
                Link(scene_id="cave", requires={"cave_clue_known": "discovered"}),
            ]
        }
    )
    updated_manifest = AdventureManifest.model_validate(
        manifest.model_dump(mode="json")
        | {
            "scenes": [
                dock.model_dump(mode="json"),
                Scene(scene_id="cave", title="The cave").model_dump(mode="json"),
            ],
            "entities": [
                Entity(
                    entity_id="cave_clue",
                    kind="clue",
                    title="A cave entrance",
                    discovery_flag="cave_clue_known",
                ).model_dump(mode="json")
            ],
            "passages": [passage.model_dump(mode="json")],
            "objectives": [
                objective.model_dump(mode="json"),
                cave_objective.model_dump(mode="json"),
            ],
            "resolution_policies": [
                policy.model_dump(mode="json"),
                cave_policy.model_dump(mode="json"),
            ],
        }
    )
    provenance = prepared.quests[0].provenance
    cave_quest = PreparedQuest(
        quest_id="cave_quest",
        objective_id="find_cave_keeper",
        scene_ids=["cave"],
        required_approaches=["negotiation", "combat"],
        provenance=provenance,
    )
    cave_branch = PreparedBranch(
        branch_id="cave_branch",
        quest_id="cave_quest",
        scene_id="cave",
        approach="negotiation",
        action="Search the cave for the keeper.",
        policy_ids=["cave_search"],
        outcome_ids=["cave_success", "cave_partial"],
        provenance=provenance,
    )
    cave_combat_branch = PreparedBranch(
        branch_id="cave_combat_branch",
        quest_id="cave_quest",
        scene_id="cave",
        approach="combat",
        action="Fight through the cave to find the keeper.",
        policy_ids=["cave_search"],
        outcome_ids=["cave_combat_success", "cave_combat_partial"],
        provenance=provenance,
    )
    cave_outcomes = (
        PreparedOutcome(
            outcome_id="cave_success",
            branch_id="cave_branch",
            result="success",
            policy_selections=[PreparedPolicySelection(policy_id="cave_search", result="success")],
            provenance=provenance,
        ),
        PreparedOutcome(
            outcome_id="cave_partial",
            branch_id="cave_branch",
            result="partial",
            policy_selections=[PreparedPolicySelection(policy_id="cave_search", result="failure")],
            provenance=provenance,
        ),
        PreparedOutcome(
            outcome_id="cave_combat_success",
            branch_id="cave_combat_branch",
            result="success",
            policy_selections=[PreparedPolicySelection(policy_id="cave_search", result="success")],
            provenance=provenance,
        ),
        PreparedOutcome(
            outcome_id="cave_combat_partial",
            branch_id="cave_combat_branch",
            result="partial",
            policy_selections=[PreparedPolicySelection(policy_id="cave_search", result="failure")],
            provenance=provenance,
        ),
    )
    updated_prepared = prepared.model_copy(
        update={
            "acts": [prepared.acts[0].model_copy(update={"scene_ids": ["dock", "cave"]})],
            "scene_packets": [
                *prepared.scene_packets,
                PreparedScenePacket(
                    scene_id="cave",
                    passage_ids=["source_intro"],
                    objective_ids=["find_cave_keeper"],
                    policy_ids=["cave_search"],
                    dm_notes="The keeper may be found in this cave.",
                    provenance=provenance,
                ),
            ],
            "routes": [
                *prepared.routes,
                PreparedRoute(
                    from_scene_id="dock",
                    to_scene_id="cave",
                    visibility="known",
                    traversal_requires={"cave_clue_known": "discovered"},
                    provenance=provenance,
                ),
            ],
            "quests": [*prepared.quests, cave_quest],
            "branches": [*prepared.branches, cave_branch, cave_combat_branch],
            "outcomes": [*prepared.outcomes, *cave_outcomes],
        }
    )
    updated_manifest = _approved(updated_manifest, updated_prepared)
    assert validate_prepared_campaign(updated_manifest, updated_prepared).valid, (
        validate_prepared_campaign(updated_manifest, updated_prepared).findings
    )
    updated_manifest_artifact = _replace_artifact_payload(
        binding, manifest_artifact, {"manifest": updated_manifest.model_dump(mode="json")}
    )
    no_discovery_manifest = updated_manifest.model_copy(
        update={
            "resolution_policies": [
                policy.model_copy(
                    update={"on_success": manifest.resolution_policies[0].on_success}
                ),
                cave_policy,
            ]
        }
    )
    no_discovery_manifest = _approved(no_discovery_manifest, updated_prepared)
    no_discovery_artifact = _replace_artifact_payload(
        binding, manifest_artifact, {"manifest": no_discovery_manifest.model_dump(mode="json")}
    )
    updated_prepared_artifact = _replace_artifact_payload(
        binding, prepared_artifact, {"prepared": updated_prepared.model_dump(mode="json")}
    )

    closed = explore_prepared_paths(
        binding,
        no_discovery_artifact,
        updated_prepared_artifact,
        manuscript_artifact,
        initial_world_flags={"gate_open": "yes"},
    )
    unlocked = explore_prepared_paths(
        binding,
        updated_manifest_artifact,
        updated_prepared_artifact,
        manuscript_artifact,
        initial_world_flags={"gate_open": "yes"},
    )

    assert "cave_success" not in closed.reached_outcome_ids
    # The clue is discovered only on a successful authored resolution, which unlocks the route.
    assert "cave_success" in unlocked.reached_outcome_ids
    assert "cave_success" in unlocked.expected_outcome_ids


def test_path_exploration_tracks_acquire_use_and_consumption_ownership():
    binding, _, _, manifest_artifact, prepared_artifact, manuscript_artifact = (
        _bound_review_inventory()
    )
    manifest = AdventureManifest.model_validate(manifest_artifact.payload["manifest"])
    prepared = PreparedCampaign.model_validate(prepared_artifact.payload["prepared"])
    key_objective = StoryDefinition(
        canonical_id="open_cave_door",
        title="Open the cave door",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        statuses=["active", "completed", "failed"],
    )
    after_use_objective = StoryDefinition(
        canonical_id="use_key_again_goal",
        title="Use the key again",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        statuses=["active", "completed", "failed"],
    )
    key = StoryDefinition(
        canonical_id="silver_key",
        title="Silver key",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        consumable=True,
        portable=True,
        initial_owner="dock",
        operations=["unlock"],
        statuses=["active", "acquired", "consumed", "used"],
    )
    acquire = PolicyEffect(
        kind="acquire", target_id="silver_key", status="acquired", passage_ids=["source_intro"]
    )
    rescue_policy = manifest.resolution_policies[0].model_copy(
        update={"on_success": [*manifest.resolution_policies[0].on_success, acquire]}
    )
    use_effect = PolicyEffect(
        kind="use",
        target_id="silver_key",
        status="used",
        passage_ids=["source_intro"],
        operation="unlock",
    )
    key_used_effect = PolicyEffect(
        kind="discover",
        target_id="key_used_clue",
        status="discovered",
        passage_ids=["source_intro"],
    )
    consume_effect = PolicyEffect(
        kind="consume", target_id="silver_key", status="consumed", passage_ids=["source_intro"]
    )
    use_policy = ResolutionPolicy(
        policy_id="use_key",
        action="Unlock the cave door",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        requires_items=["Silver key"],
        mode="check",
        check="thieves_tools",
        dc=10,
        failure_mode="partial",
        on_success=[use_effect, key_used_effect],
        on_failure=[use_effect, key_used_effect],
    )
    consume_policy = ResolutionPolicy(
        policy_id="consume_key",
        action="Break the silver key",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        requires_items=["Silver key"],
        mode="check",
        check="thieves_tools",
        dc=10,
        failure_mode="partial",
        on_success=[consume_effect],
        on_failure=[consume_effect],
    )
    use_again_policy = ResolutionPolicy(
        policy_id="use_key_again",
        action="Use the silver key again",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        requires_items=["Silver key"],
        requires={"key_used": "discovered"},
        mode="check",
        check="thieves_tools",
        dc=10,
        failure_mode="partial",
        on_success=[use_effect],
        on_failure=[use_effect],
    )
    updated_manifest = AdventureManifest.model_validate(
        manifest.model_dump(mode="json")
        | {
            "entities": [
                Entity(
                    entity_id="key_used_clue",
                    kind="clue",
                    title="The door unlocks",
                    discovery_flag="key_used",
                    reveal_on="always",
                ).model_dump(mode="json")
            ],
            "passages": [
                manifest.passages[0]
                .model_copy(update={"entity_ids": ["key_used_clue"]})
                .model_dump(mode="json")
            ],
            "objects": [key.model_dump(mode="json")],
            "objectives": [
                *manifest.objectives,
                key_objective.model_dump(mode="json"),
                after_use_objective.model_dump(mode="json"),
            ],
            "resolution_policies": [
                rescue_policy.model_dump(mode="json"),
                use_policy.model_dump(mode="json"),
                consume_policy.model_dump(mode="json"),
                use_again_policy.model_dump(mode="json"),
            ],
        }
    )
    provenance = prepared.quests[0].provenance
    object_quest = PreparedQuest(
        quest_id="object_quest",
        objective_id="rescue_keeper",
        scene_ids=["dock"],
        required_approaches=["negotiation", "combat"],
        provenance=provenance,
    )
    after_use_quest = PreparedQuest(
        quest_id="after_use_quest",
        objective_id="use_key_again_goal",
        scene_ids=["dock"],
        prerequisites={"key_used": "discovered"},
        required_approaches=["negotiation", "combat"],
        provenance=provenance,
    )
    object_quest = object_quest.model_copy(update={"objective_id": "open_cave_door"})
    use_branch = PreparedBranch(
        branch_id="use_branch",
        quest_id="object_quest",
        scene_id="dock",
        approach="negotiation",
        action="Unlock the door with the silver key.",
        policy_ids=["use_key"],
        outcome_ids=["use_key_outcome", "use_key_other"],
        provenance=provenance,
    )
    consume_branch = PreparedBranch(
        branch_id="consume_branch",
        quest_id="object_quest",
        scene_id="dock",
        approach="combat",
        action="Break the silver key to open the door.",
        policy_ids=["consume_key"],
        outcome_ids=["consume_key_outcome", "consume_key_other"],
        provenance=provenance,
    )
    after_use_branch = PreparedBranch(
        branch_id="after_use_branch",
        quest_id="after_use_quest",
        scene_id="dock",
        approach="negotiation",
        action="Use the key again after opening the door.",
        policy_ids=["use_key_again"],
        outcome_ids=["after_use_success", "after_use_other"],
        provenance=provenance,
    )
    after_use_combat_branch = PreparedBranch(
        branch_id="after_use_combat_branch",
        quest_id="after_use_quest",
        scene_id="dock",
        approach="combat",
        action="Use the key again during the fight.",
        policy_ids=["use_key_again"],
        outcome_ids=["after_use_combat_success", "after_use_combat_other"],
        provenance=provenance,
    )
    new_branches = (use_branch, consume_branch, after_use_branch, after_use_combat_branch)
    new_outcomes = tuple(
        PreparedOutcome(
            outcome_id=outcome_id,
            branch_id=branch.branch_id,
            result=result,
            policy_selections=[
                PreparedPolicySelection(
                    policy_id=policy_id,
                    result="success" if result == "success" else "failure",
                )
            ],
            provenance=provenance,
        )
        for branch, policy_id, ids in (
            (use_branch, "use_key", ("use_key_outcome", "use_key_other")),
            (consume_branch, "consume_key", ("consume_key_outcome", "consume_key_other")),
            (after_use_branch, "use_key_again", ("after_use_success", "after_use_other")),
            (
                after_use_combat_branch,
                "use_key_again",
                ("after_use_combat_success", "after_use_combat_other"),
            ),
        )
        for outcome_id, result in zip(ids, ("success", "partial"), strict=True)
    )
    updated_prepared = prepared.model_copy(
        update={
            "quests": [*prepared.quests, object_quest, after_use_quest],
            "scene_packets": [
                prepared.scene_packets[0].model_copy(
                    update={
                        "objective_ids": ["rescue_keeper", "open_cave_door", "use_key_again_goal"],
                        "policy_ids": ["rescue_check", "use_key", "consume_key", "use_key_again"],
                    }
                )
            ],
            "branches": [*prepared.branches, *new_branches],
            "outcomes": [*prepared.outcomes, *new_outcomes],
        }
    )
    updated_manifest = _approved(updated_manifest, updated_prepared)
    validation = validate_prepared_campaign(updated_manifest, updated_prepared)
    assert validation.valid, validation.findings
    updated_manifest_artifact = _replace_artifact_payload(
        binding, manifest_artifact, {"manifest": updated_manifest.model_dump(mode="json")}
    )
    updated_prepared_artifact = _replace_artifact_payload(
        binding, prepared_artifact, {"prepared": updated_prepared.model_dump(mode="json")}
    )

    report = explore_prepared_paths(
        binding,
        updated_manifest_artifact,
        updated_prepared_artifact,
        manuscript_artifact,
        initial_world_flags={"gate_open": "yes"},
    )

    assert "use_key_outcome" in report.reached_outcome_ids
    assert "consume_key_outcome" in report.reached_outcome_ids
    assert "after_use_success" in report.reached_outcome_ids
    assert any(
        "use_key_outcome" in example.outcome_ids and "after_use_success" in example.outcome_ids
        for example in report.examples
    )
    assert not any(
        "consume_key_outcome" in example.outcome_ids
        and "after_use_success" in example.outcome_ids
        and example.outcome_ids.index("consume_key_outcome")
        < example.outcome_ids.index("after_use_success")
        for example in report.examples
    )


def test_path_exploration_projects_irrelevant_history_for_a_24_scene_campaign():
    binding, _, _, manifest_artifact, prepared_artifact, manuscript_artifact = (
        _bound_review_inventory()
    )
    manifest = AdventureManifest.model_validate(manifest_artifact.payload["manifest"])
    prepared = PreparedCampaign.model_validate(prepared_artifact.payload["prepared"])
    scene_ids = ["dock", *(f"chapter_{index}" for index in range(1, 25))]
    provenance = prepared.quests[0].provenance
    scenes = [
        manifest.scenes[0].model_copy(
            update={
                "links": [
                    *manifest.scenes[0].links,
                    Link(scene_id=scene_ids[1]),
                ]
            }
        )
    ]
    scenes.extend(
        Scene(
            scene_id=scene_id,
            title=f"Chapter {index}",
            links=[Link(scene_id=scene_ids[index + 1])] if index < 24 else [],
        )
        for index, scene_id in enumerate(scene_ids[1:], start=1)
    )
    passage = manifest.passages[0].model_copy(update={"scene_ids": scene_ids})
    objectives = [
        StoryDefinition(
            canonical_id=f"objective_{index}",
            title=f"Complete chapter {index}",
            scene_ids=[scene_ids[index]],
            passage_ids=["source_intro"],
            statuses=["active", "completed", "failed"],
        )
        for index in range(1, 25)
    ]
    policies = [
        ResolutionPolicy(
            policy_id=f"policy_{index}",
            action=f"Resolve chapter {index}",
            scene_ids=[scene_ids[index]],
            passage_ids=["source_intro"],
            mode="check",
            check="perception",
            dc=10,
            failure_mode="partial",
            on_success=[
                PolicyEffect(
                    kind="objective",
                    target_id=f"objective_{index}",
                    status="completed",
                    passage_ids=["source_intro"],
                )
            ],
            on_failure=[
                PolicyEffect(
                    kind="objective",
                    target_id=f"objective_{index}",
                    status="failed",
                    passage_ids=["source_intro"],
                )
            ],
        )
        for index in range(1, 25)
    ]
    updated_manifest = AdventureManifest.model_validate(
        manifest.model_dump(mode="json")
        | {
            "scenes": [scene.model_dump(mode="json") for scene in scenes],
            "passages": [passage.model_dump(mode="json")],
            "objectives": [
                *manifest.objectives,
                *(row.model_dump(mode="json") for row in objectives),
            ],
            "resolution_policies": [
                *[row.model_dump(mode="json") for row in manifest.resolution_policies],
                *(row.model_dump(mode="json") for row in policies),
            ],
        }
    )
    quests = [
        PreparedQuest(
            quest_id=f"quest_{index}",
            objective_id=f"objective_{index}",
            scene_ids=[scene_ids[index]],
            required_approaches=["negotiation", "combat"],
            provenance=provenance,
        )
        for index in range(1, 25)
    ]
    branches = []
    outcomes = []
    packets = []
    routes = list(prepared.routes)
    for index in range(1, 25):
        quest_id = f"quest_{index}"
        policy_id = f"policy_{index}"
        negotiation = PreparedBranch(
            branch_id=f"negotiation_{index}",
            quest_id=quest_id,
            scene_id=scene_ids[index],
            approach="negotiation",
            action=f"Negotiate in chapter {index}.",
            policy_ids=[policy_id],
            outcome_ids=[f"success_{index}", f"partial_{index}"],
            provenance=provenance,
        )
        combat = PreparedBranch(
            branch_id=f"combat_{index}",
            quest_id=quest_id,
            scene_id=scene_ids[index],
            approach="combat",
            action=f"Fight in chapter {index}.",
            policy_ids=[policy_id],
            outcome_ids=[f"combat_success_{index}", f"combat_partial_{index}"],
            provenance=provenance,
        )
        branches.extend((negotiation, combat))
        for branch, pairs in (
            (negotiation, (("success", "success"), ("partial", "failure"))),
            (combat, (("combat_success", "success"), ("combat_partial", "failure"))),
        ):
            for label, selection_result in pairs:
                outcome_id = f"{label}_{index}"
                outcomes.append(
                    PreparedOutcome(
                        outcome_id=outcome_id,
                        branch_id=branch.branch_id,
                        result="success" if selection_result == "success" else "partial",
                        policy_selections=[
                            PreparedPolicySelection(policy_id=policy_id, result=selection_result)
                        ],
                        provenance=provenance,
                    )
                )
        packets.append(
            PreparedScenePacket(
                scene_id=scene_ids[index],
                passage_ids=["source_intro"],
                objective_ids=[f"objective_{index}"],
                policy_ids=[policy_id],
                dm_notes=f"Chapter {index} scene notes.",
                provenance=provenance,
            )
        )
        routes.append(
            PreparedRoute(
                from_scene_id=scene_ids[index - 1],
                to_scene_id=scene_ids[index],
                visibility="known",
                provenance=provenance,
            )
        )
    updated_prepared = prepared.model_copy(
        update={
            "acts": [prepared.acts[0].model_copy(update={"scene_ids": scene_ids})],
            "quests": [*prepared.quests, *quests],
            "scene_packets": [prepared.scene_packets[0], *packets],
            "routes": routes,
            "branches": [*prepared.branches, *branches],
            "outcomes": [*prepared.outcomes, *outcomes],
        }
    )
    updated_manifest = _approved(updated_manifest, updated_prepared)
    validation = validate_prepared_campaign(updated_manifest, updated_prepared)
    assert validation.valid, validation.findings
    updated_manifest_artifact = _replace_artifact_payload(
        binding, manifest_artifact, {"manifest": updated_manifest.model_dump(mode="json")}
    )
    updated_prepared_artifact = _replace_artifact_payload(
        binding, prepared_artifact, {"prepared": updated_prepared.model_dump(mode="json")}
    )

    report = explore_prepared_paths(
        binding,
        updated_manifest_artifact,
        updated_prepared_artifact,
        manuscript_artifact,
        initial_world_flags={"gate_open": "yes"},
        budget=PathBudget(max_states=512, max_depth=64),
    )

    assert report.exhaustive
    assert len(report.expected_outcome_ids) == 100
    assert set(report.reached_outcome_ids) == set(report.expected_outcome_ids)
    assert report.explored_states < 512


def test_path_exploration_stops_after_irreversible_ending_outcomes():
    binding, _, _, manifest_artifact, prepared_artifact, manuscript_artifact = (
        _bound_review_inventory()
    )
    manifest = AdventureManifest.model_validate(manifest_artifact.payload["manifest"])
    prepared = PreparedCampaign.model_validate(prepared_artifact.payload["prepared"])
    ending = Entity(
        entity_id="final_ending",
        kind="ending",
        title="The keeper is rescued",
        discovery_flag="ending_reached",
        reveal_on="always",
    )
    final_objective = StoryDefinition(
        canonical_id="after_ending_objective",
        title="Continue after the ending",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        statuses=["active", "completed", "failed"],
    )
    ending_effect = PolicyEffect(
        kind="discover",
        target_id="final_ending",
        status="discovered",
        passage_ids=["source_intro"],
    )
    ending_policy = manifest.resolution_policies[0].model_copy(
        update={
            "on_success": [*manifest.resolution_policies[0].on_success, ending_effect],
            "on_failure": [*manifest.resolution_policies[0].on_failure, ending_effect],
        }
    )
    after_policy = ResolutionPolicy(
        policy_id="after_ending_policy",
        action="Attempt to continue beyond the ending",
        scene_ids=["dock"],
        passage_ids=["source_intro"],
        requires={"ending_reached": "discovered"},
        mode="check",
        check="perception",
        dc=10,
        failure_mode="partial",
        on_success=[
            PolicyEffect(
                kind="objective",
                target_id="after_ending_objective",
                status="completed",
                passage_ids=["source_intro"],
            )
        ],
        on_failure=[
            PolicyEffect(
                kind="objective",
                target_id="after_ending_objective",
                status="failed",
                passage_ids=["source_intro"],
            )
        ],
    )
    passage = manifest.passages[0].model_copy(update={"entity_ids": ["final_ending"]})
    updated_manifest = AdventureManifest.model_validate(
        manifest.model_dump(mode="json")
        | {
            "entities": [ending.model_dump(mode="json")],
            "passages": [passage.model_dump(mode="json")],
            "objectives": [*manifest.objectives, final_objective.model_dump(mode="json")],
            "resolution_policies": [
                ending_policy.model_dump(mode="json"),
                after_policy.model_dump(mode="json"),
            ],
        }
    )
    provenance = prepared.quests[0].provenance
    after_quest = PreparedQuest(
        quest_id="after_ending_quest",
        objective_id="after_ending_objective",
        scene_ids=["dock"],
        prerequisites={"ending_reached": "discovered"},
        required_approaches=["negotiation", "combat"],
        provenance=provenance,
    )
    late_branches = (
        PreparedBranch(
            branch_id="after_ending_negotiation",
            quest_id="after_ending_quest",
            scene_id="dock",
            approach="negotiation",
            action="Negotiate after the ending.",
            policy_ids=["after_ending_policy"],
            outcome_ids=["late_success", "late_partial"],
            provenance=provenance,
        ),
        PreparedBranch(
            branch_id="after_ending_combat",
            quest_id="after_ending_quest",
            scene_id="dock",
            approach="combat",
            action="Fight after the ending.",
            policy_ids=["after_ending_policy"],
            outcome_ids=["late_combat_success", "late_combat_partial"],
            provenance=provenance,
        ),
    )
    late_outcomes = tuple(
        PreparedOutcome(
            outcome_id=outcome_id,
            branch_id=branch.branch_id,
            result=result,
            policy_selections=[
                PreparedPolicySelection(
                    policy_id="after_ending_policy",
                    result="success" if result == "success" else "failure",
                )
            ],
            provenance=provenance,
        )
        for branch, ids in (
            (late_branches[0], ("late_success", "late_partial")),
            (late_branches[1], ("late_combat_success", "late_combat_partial")),
        )
        for outcome_id, result in zip(ids, ("success", "partial"), strict=True)
    )
    existing_outcomes = [
        outcome.model_copy(update={"ending_entity_ids": ["final_ending"]})
        for outcome in prepared.outcomes
    ]
    updated_prepared = prepared.model_copy(
        update={
            "quests": [*prepared.quests, after_quest],
            "scene_packets": [
                prepared.scene_packets[0].model_copy(
                    update={
                        "objective_ids": ["rescue_keeper", "after_ending_objective"],
                        "policy_ids": ["rescue_check", "after_ending_policy"],
                    }
                )
            ],
            "branches": [*prepared.branches, *late_branches],
            "outcomes": [*existing_outcomes, *late_outcomes],
        }
    )
    updated_manifest = _approved(updated_manifest, updated_prepared)
    validation = validate_prepared_campaign(updated_manifest, updated_prepared)
    assert validation.valid, validation.findings
    updated_manifest_artifact = _replace_artifact_payload(
        binding, manifest_artifact, {"manifest": updated_manifest.model_dump(mode="json")}
    )
    updated_prepared_artifact = _replace_artifact_payload(
        binding, prepared_artifact, {"prepared": updated_prepared.model_dump(mode="json")}
    )

    report = explore_prepared_paths(
        binding,
        updated_manifest_artifact,
        updated_prepared_artifact,
        manuscript_artifact,
        initial_world_flags={"gate_open": "yes"},
    )

    assert set(report.reached_outcome_ids) == {outcome.outcome_id for outcome in existing_outcomes}
    assert set(report.unreachable_outcome_ids) == {
        "late_success",
        "late_partial",
        "late_combat_success",
        "late_combat_partial",
    }


def test_repair_envelope_preserves_source_digests_and_revealed_facts():
    binding, _, proposed, *_ = _bound_review_inventory()
    source = build_artifact(
        binding,
        stage=PreparationStage.P01,
        task_id="p01_source",
        artifact_kind="retained_source",
        payload={"source": "immutable source text"},
        dependency_artifacts=(),
        created_at="2026-10-15T00:00:00Z",
    )
    grounding = build_artifact(
        binding,
        stage=PreparationStage.P02,
        task_id="p02_grounding",
        artifact_kind="grounding_report",
        payload={"source_artifact": source.artifact_id, "grounded": True},
        dependency_artifacts=(source,),
        created_at="2026-10-15T00:00:00Z",
    )
    original = next(item for item in proposed if item.artifact_kind == "prepared_campaign")
    replacement = build_artifact(
        binding,
        stage=PreparationStage.P11,
        task_id="p11_repair",
        artifact_kind=original.artifact_kind,
        payload={**original.payload, "repair_note": "bounded repair"},
        dependency_artifacts=(source, grounding),
        created_at="2026-10-16T00:00:00Z",
        version=2,
    )
    preserved_sources = {item.artifact_id: item.content_sha256 for item in (source, grounding)}
    revealed_facts = {"keeper_rescued": "a" * 64}
    envelope = RepairEnvelope(
        run_binding_sha256=binding.binding_sha256,
        source_binding_sha256=binding.source_binding_sha256,
        finding_ids=("coh-" + "b" * 24,),
        affected_artifact_ids=(original.artifact_id,),
        replacements=(
            RepairReplacement(
                replaces_artifact_id=original.artifact_id,
                replacement_artifact_id=replacement.artifact_id,
                original_version=original.version,
                replacement_version=replacement.version,
            ),
        ),
        preserved_source_artifact_digests=preserved_sources,
        preserved_revealed_fact_digests=revealed_facts,
    )
    assert (
        validate_repair_envelope(
            envelope,
            binding=binding,
            originals=(source, grounding, original),
            replacements=(replacement,),
            expected_finding_ids=envelope.finding_ids,
            preserved_revealed_fact_digests=revealed_facts,
        )
        == envelope
    )

    changed_facts = envelope.model_copy(
        update={"preserved_revealed_fact_digests": {"keeper_rescued": "c" * 64}}
    )
    with pytest.raises(ValueError, match="sealed revealed-fact"):
        validate_repair_envelope(
            changed_facts,
            binding=binding,
            originals=(source, grounding, original),
            replacements=(replacement,),
            expected_finding_ids=envelope.finding_ids,
            preserved_revealed_fact_digests=revealed_facts,
        )

    changed_sources = envelope.model_copy(
        update={
            "preserved_source_artifact_digests": {
                **preserved_sources,
                source.artifact_id: "0" * 64,
            }
        }
    )
    with pytest.raises(ValueError, match="source or grounding artifact digest"):
        validate_repair_envelope(
            changed_sources,
            binding=binding,
            originals=(source, grounding, original),
            replacements=(replacement,),
            expected_finding_ids=envelope.finding_ids,
            preserved_revealed_fact_digests=revealed_facts,
        )

    source_replacement = build_artifact(
        binding,
        stage=PreparationStage.P11,
        task_id="p11_source_repair",
        artifact_kind="retained_source",
        payload={"source": "rewritten source"},
        dependency_artifacts=(),
        created_at="2026-10-16T00:00:00Z",
        version=2,
    )
    source_envelope = RepairEnvelope(
        run_binding_sha256=binding.binding_sha256,
        source_binding_sha256=binding.source_binding_sha256,
        finding_ids=envelope.finding_ids,
        affected_artifact_ids=(source.artifact_id,),
        replacements=(
            RepairReplacement(
                replaces_artifact_id=source.artifact_id,
                replacement_artifact_id=source_replacement.artifact_id,
                original_version=source.version,
                replacement_version=source_replacement.version,
            ),
        ),
        preserved_source_artifact_digests=preserved_sources,
        preserved_revealed_fact_digests=revealed_facts,
    )
    with pytest.raises(ValueError, match="immutable during repair"):
        validate_repair_envelope(
            source_envelope,
            binding=binding,
            originals=(source, grounding),
            replacements=(source_replacement,),
            expected_finding_ids=envelope.finding_ids,
            preserved_revealed_fact_digests=revealed_facts,
        )


def test_actual_party_inventory_snapshot_includes_nested_hero_items_and_scalar_gear():
    snapshot = {
        "heroes": [
            {
                "name": "Mira",
                "inventory": {
                    "weapons": ["Longsword"],
                    "items": [{"name": "Silver key"}],
                    "armor": "Chain mail",
                },
            }
        ]
    }

    assert _inventory_names(snapshot) == {"longsword", "silver key", "chain mail"}


def test_path_search_state_and_depth_exhaustion_leave_outcomes_unverified():
    binding, dependencies, proposed, manifest, prepared, manuscript = _bound_review_inventory()
    prepared_model = PreparedCampaign.model_validate(prepared.payload["prepared"])
    extra_outcome = prepared_model.outcomes[0].model_copy(
        update={"outcome_id": "gated_outcome", "requires": {"deep_gate": "yes"}}
    )
    second_quest = prepared_model.quests[0].model_copy(update={"quest_id": "second_quest"})
    second_branch = prepared_model.branches[0].model_copy(
        update={
            "branch_id": "second_branch",
            "quest_id": "second_quest",
            "outcome_ids": ["second_success", "second_partial"],
        }
    )
    second_outcomes = [
        prepared_model.outcomes[index].model_copy(
            update={
                "outcome_id": f"second_{result}",
                "branch_id": "second_branch",
            }
        )
        for index, result in enumerate(("success", "partial"))
    ]
    first_branch = prepared_model.branches[0].model_copy(
        update={"outcome_ids": [*prepared_model.branches[0].outcome_ids, "gated_outcome"]}
    )
    prepared_model = prepared_model.model_copy(
        update={
            "quests": [*prepared_model.quests, second_quest],
            "branches": [first_branch, *prepared_model.branches[1:], second_branch],
            "outcomes": [*prepared_model.outcomes, extra_outcome, *second_outcomes],
        }
    )
    prepared_artifact = build_artifact(
        binding,
        stage=PreparationStage.P09,
        task_id="p09_review",
        artifact_kind="prepared_campaign",
        payload={"prepared": prepared_model.model_dump(mode="json")},
        dependency_artifacts=dependencies,
        created_at="2026-10-16T00:00:00Z",
    )

    state_limited = explore_prepared_paths(
        binding,
        manifest,
        prepared_artifact,
        manuscript,
        initial_world_flags={"gate_open": "yes"},
        budget=PathBudget(max_states=1, max_depth=64),
    )
    assert state_limited.exhausted
    assert state_limited.unverified_outcome_ids
    assert not state_limited.unreachable_outcome_ids

    depth_limited = explore_prepared_paths(
        binding,
        manifest,
        prepared_artifact,
        manuscript,
        initial_world_flags={"gate_open": "yes"},
        budget=PathBudget(max_states=64, max_depth=1),
    )
    assert depth_limited.exhausted
    assert depth_limited.unverified_outcome_ids
    assert not depth_limited.unreachable_outcome_ids


def test_critic_rejects_stale_bindings_and_forged_evidence():
    binding, dependencies, proposed, _, _, manuscript = _bound_review_inventory()
    report_artifact = next(a for a in proposed if a.artifact_kind == "semantic_critic_report")
    reviewed = CriticReport.model_validate(report_artifact.payload["critic_report"], strict=False)
    manuscript = RetainedManuscript.model_validate(manuscript.payload["manuscript"], strict=False)
    inventory = (
        *dependencies,
        *(
            a
            for a in proposed
            if a.artifact_kind not in {"deterministic_review_report", "semantic_critic_report"}
        ),
    )
    stale = reviewed.model_copy(
        update={"bindings": reviewed.bindings.model_copy(update={"run_id": "prep-" + "b" * 32})}
    )
    with pytest.raises(ValueError, match="stale"):
        validate_critic_report(
            stale,
            expected_bindings=reviewed.bindings,
            expected_chapters=manuscript,
            artifacts=inventory,
        )

    manuscript_artifact = next(a for a in proposed if a.artifact_kind == "retained_manuscript")
    chapter_text = manuscript.chapters[0].text
    forged_evidence = CriticEvidence(
        artifact_id=manuscript_artifact.artifact_id,
        artifact_sha256=manuscript_artifact.content_sha256,
        json_pointer="/manuscript/chapters/0/text",
        excerpt=chapter_text,
        excerpt_sha256="0" * 64,
    )
    forged = reviewed.model_copy(
        update={
            "findings": (
                CriticFinding(
                    finding_id="forged",
                    code="unsupported_claim",
                    severity="high",
                    message="Forged evidence digest.",
                    evidence=(forged_evidence,),
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="digest"):
        validate_critic_report(
            forged,
            expected_bindings=reviewed.bindings,
            expected_chapters=manuscript,
            artifacts=inventory,
        )


def test_deterministic_review_reports_coverage_repeated_climax_and_contradictory_continuity():
    binding, dependencies, _, manifest, prepared, manuscript_artifact = _bound_review_inventory()
    manuscript = RetainedManuscript.model_validate(
        manuscript_artifact.payload["manuscript"], strict=False
    )
    chapter_one = manuscript.chapters[0].model_copy(
        update={
            "climax_ids": ("keeper_rescue",),
            "continuity_assertions": (
                ContinuityAssertion(
                    subject_id="rescue_keeper",
                    field="motive",
                    value="Protect the keeper",
                    passage_ids=("source_intro",),
                ),
            ),
        }
    )
    chapter_two = chapter_one.model_copy(
        update={
            "chapter_id": "chapter_two",
            "order": 2,
            "role": "aftermath",
            "climax_ids": ("keeper_rescue",),
            "continuity_assertions": (
                ContinuityAssertion(
                    subject_id="rescue_keeper",
                    field="motive",
                    value="Abandon the keeper",
                    passage_ids=("source_intro",),
                ),
            ),
        }
    )
    expanded = RetainedManuscript(source_mode="pdf", chapters=(chapter_one, chapter_two))
    expanded_artifact = build_artifact(
        binding,
        stage=PreparationStage.P09,
        task_id="p09_review",
        artifact_kind="retained_manuscript",
        payload={"manuscript": expanded.model_dump(mode="json")},
        dependency_artifacts=(),
        created_at="2026-10-16T00:00:00Z",
    )
    report = build_adventure_review(
        binding,
        manifest_artifact=manifest,
        prepared_artifact=prepared,
        manuscript_artifact=expanded_artifact,
        source_artifacts=(dependencies[0],),
    )
    codes = {finding.code for finding in report.findings}
    assert {"chapter_scene_repeated", "repeated_climax", "continuity_contradiction"} <= codes

    missing_scene_chapter = chapter_two.model_copy(update={"scene_ids": ("unknown_scene",)})
    missing_scene_manuscript = RetainedManuscript(
        source_mode="pdf", chapters=(chapter_one, missing_scene_chapter)
    )
    missing_scene_artifact = build_artifact(
        binding,
        stage=PreparationStage.P09,
        task_id="p09_review",
        artifact_kind="retained_manuscript",
        payload={"manuscript": missing_scene_manuscript.model_dump(mode="json")},
        dependency_artifacts=(),
        created_at="2026-10-16T00:00:00Z",
    )
    missing_report = build_adventure_review(
        binding,
        manifest_artifact=manifest,
        prepared_artifact=prepared,
        manuscript_artifact=missing_scene_artifact,
        source_artifacts=(dependencies[0],),
    )
    assert "chapter_scene_coverage" in {finding.code for finding in missing_report.findings}
