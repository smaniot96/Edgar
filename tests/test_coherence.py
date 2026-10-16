"""Focused tests for version-bound whole-adventure review contracts."""

from test_prepared_campaign import _approved, _fixture

from edgar_core.coherence import (
    CriticReport,
    RetainedChapter,
    RetainedManuscript,
    _digest,
    build_adventure_review,
    build_path_exploration_artifact,
    build_review_bindings,
    explore_prepared_paths,
    make_deterministic_review_validator,
    make_independent_critic_validator,
    make_path_exploration_validator,
)
from edgar_core.preparation import (
    PreparationBudget,
    PreparationRunBinding,
    PreparationStage,
    build_artifact,
    canonical_sha256,
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
    review = build_adventure_review(
        binding,
        manifest_artifact=manifest_artifact,
        prepared_artifact=prepared_artifact,
        manuscript_artifact=manuscript_artifact,
        source_artifacts=deps,
    )
    review_bindings = build_review_bindings(
        binding, manifest_artifact, prepared_artifact, manuscript_artifact, (*deps, *subjects)
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
        (*subjects, report_artifact, critic_artifact),
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
