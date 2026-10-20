"""Focused tests for version-bound whole-adventure review contracts."""

import pytest
from test_prepared_campaign import _approved, _fixture

from edgar_core.coherence import (
    ContinuityAssertion,
    CriticEvidence,
    CriticFinding,
    CriticReport,
    RepairEnvelope,
    RepairReplacement,
    RetainedChapter,
    RetainedManuscript,
    _digest,
    build_adventure_review,
    build_review_bindings,
    make_deterministic_review_validator,
    make_independent_critic_validator,
    validate_critic_report,
    validate_repair_envelope,
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
