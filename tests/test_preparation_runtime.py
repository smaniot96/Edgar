"""Offline safety checks for the private preparation runtime projections."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from apps.api.services import adventures_meta, campaign_sources, preparation_runtime as runtime
from edgar_core.preparation import (
    PreparationBudget,
    PreparationJobStatus,
    PreparationRunBinding,
    PreparationStage,
    PublicPreparationStatus,
    build_artifact,
    build_publication,
    build_validation_receipt,
    canonical_sha256,
)


@pytest.fixture
def private_meta_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    return tmp_path / "adventures"


def test_missing_setup_has_safe_restart_projection(private_meta_dir):
    adventures_meta.save_meta(
        "private",
        {
            "private_preparation": True,
            "failure_code": "preparation_setup_required",
            "error": "private request details",
            "job": {"job_id": "a" * 32, "kind": "generation", "state": "failed"},
        },
    )

    projected = runtime.safe_preparation_status(adventures_meta.load_meta("private"), "private")

    assert projected is not None
    assert projected.run_id == "prep-" + "a" * 32
    assert projected.status == PreparationJobStatus.BLOCKED
    assert projected.stage == "Preparation needs attention"
    assert projected.action == "restart"
    assert "private request details" not in projected.model_dump_json()


def test_private_source_read_hides_every_unready_artifact(private_meta_dir, monkeypatch):
    adventures_meta.save_meta(
        "private",
        {
            "private_preparation": True,
            "status": "ready",
            "manifest": {"secret": "ending"},
            "prepared_campaign": {"secret": "branch"},
            "ingestion": {"source_sha256": "b" * 64},
            "source_filename": "private-book.pdf",
            "job": {"job_id": "c" * 32, "kind": "generation", "state": "running"},
        },
    )
    monkeypatch.setattr(runtime, "private_preparation_ready", lambda *_: False)
    monkeypatch.setattr(
        runtime,
        "safe_preparation_status",
        lambda *_: PublicPreparationStatus(
            run_id="prep-" + "c" * 32,
            status=PreparationJobStatus.PREPARING,
            stage="Preparing your adventure",
            retryable=False,
            action="wait",
        ),
    )

    source = campaign_sources.source_read("private")

    assert source.status == "processing"
    assert source.title == "Private adventure"
    assert source.usable_chunks == 0
    assert source.source_pdf_available is False
    assert source.ingestion is None
    assert source.extraction_preview_available is False
    assert "ending" not in source.model_dump_json()
    assert "private-book.pdf" not in source.model_dump_json()


def test_generated_extraction_must_bind_its_exact_text():
    from edgar_core.preparation import (
        PreparationBudget,
        PreparationRunBinding,
        canonical_sha256,
    )

    commission = {"title_hint": "secret"}
    heroes = {"members": ["hero"]}
    profiles = {"profiles": {}}
    binding = PreparationRunBinding(
        run_id="prep-" + "d" * 32,
        source_mode="ai",
        source_slug="private",
        source_version="v1",
        rules_edition="2014",
        commission=commission,
        commission_sha256=canonical_sha256(commission),
        hero_party_snapshot=heroes,
        hero_party_sha256=canonical_sha256(heroes),
        profile_snapshot=profiles,
        profile_snapshot_sha256=canonical_sha256(profiles),
        budget=PreparationBudget(max_provider_calls=1, max_total_tokens=1, max_cost_microusd=0),
    )

    with pytest.raises(runtime.PreparationError, match="exact manuscript text"):
        runtime._make_extraction(
            "private",
            binding,
            {
                "source": "private",
                "source_sha256": "e" * 64,
                "rules_edition": "2014",
                "pages": [{"page": 1, "text": "one exact page", "method": "generated"}],
            },
        )


def _binding(slug="private", run_id="prep-" + "d" * 32, heroes=None, commission=None):
    heroes = heroes or {
        "heroes": [
            {
                "character_id": 7,
                "name": "Mira",
                "character_class": "Ranger",
                "level": 3,
                "hp_max": 25,
                "stats": {"dexterity": 16},
                "inventory": {"bow": 1},
            }
        ]
    }
    commission = commission or {"kind": "ai", "size": "medium", "act_count": 6}
    profiles = {"profiles": {}}
    return PreparationRunBinding(
        run_id=run_id,
        source_mode="ai",
        source_slug=slug,
        source_version="ai-test",
        rules_edition="2014",
        commission=commission,
        commission_sha256=canonical_sha256(commission),
        hero_party_snapshot=heroes,
        hero_party_sha256=canonical_sha256(heroes),
        profile_snapshot=profiles,
        profile_snapshot_sha256=canonical_sha256(profiles),
        budget=PreparationBudget(
            max_provider_calls=20, max_total_tokens=1000, max_cost_microusd=10
        ),
    )


def test_captured_setup_is_frozen_as_the_grounding_party(private_meta_dir):
    from edgar_core.grounding import HeroPartySnapshot

    token = "e" * 32
    request = {
        "character_ids": [7],
        "target_level": 3,
        "max_cost_microusd": 5000,
        "max_provider_calls": 20,
        "max_total_tokens": 1000,
        "required_maps": True,
        "required_handouts": True,
        "preferences": "",
    }
    captured_setup = {
        "request": request,
        "heroes": [
            {
                "character_id": 7,
                "name": "Mira",
                "character_class": "Ranger",
                "level": 3,
                "hp_max": 25,
                "stats": {"dexterity": 16},
                "inventory": {"bow": 1},
            }
        ],
    }
    meta = {
        "private_preparation": True,
        "rules_edition": "2014",
        "preparation_setup": captured_setup,
        "generation": {"kind": "ai", "size": "medium", "theme": "woods"},
        "attempt": {"id": token},
        "job": {"attempt_id": token, "state": "running"},
    }
    setup = runtime._setup_snapshot(meta)
    assert setup is not None
    binding = runtime._binding_for(
        "private", meta, setup, {"profiles": {}}, "prep-" + "f" * 32, None
    )
    party = HeroPartySnapshot.model_validate(binding.hero_party_snapshot)
    # Production stage _party validates this exact frozen snapshot contract.
    try:
        from apps.api.services.preparation_stages import _party
    except ModuleNotFoundError:
        stage_party = party
    else:
        stage_party = _party(binding)

    meta["preparation_binding"] = {
        "binding_sha256": binding.binding_sha256,
        "hero_party_sha256": binding.hero_party_sha256,
        "profile_snapshot_sha256": binding.profile_snapshot_sha256,
    }
    adventures_meta.save_meta("private", meta)
    assert stage_party.model_dump(mode="json") == party.model_dump(mode="json")
    assert runtime._current_source_matches("private", token, binding)
    assert "hp_current" not in binding.hero_party_snapshot["heroes"][0]


@pytest.mark.parametrize(
    ("size", "act_count"),
    [("small", 3), ("medium", 6), ("large", 12), ("gigantic", 24)],
)
def test_generation_commission_freezes_validated_scope(size, act_count):
    setup = {"request": {"target_level": 5, "preferences": "keep it cozy"}}
    commission = runtime._generation_commission(
        {"generation": {"kind": "ai", "size": size, "theme": "forest"}}, setup
    )
    assert commission["act_count"] == act_count
    assert commission["target_level"] == 5
    assert commission["setup"] == setup["request"]


def test_asset_coverage_uses_the_real_receipt_contract():
    from edgar_core.assets import AssetCoverageReceipt

    value = {
        "manifest_sha256": "a" * 64,
        "prepared_sha256": "c" * 64,
        "coverage": [
            {
                "asset_id": "act_map",
                "kind": "schematic_map",
                "required": True,
                "status": "ready",
                "artifact_sha256": "d" * 64,
            }
        ],
        "act_coverage": [
            {"scope": "act", "scope_id": "act_one", "required": True, "status": "ready"}
        ],
        "scene_coverage": [
            {"scope": "scene", "scope_id": "dock", "required": True, "status": "ready"}
        ],
    }
    receipt = runtime._validated_asset_coverage(
        value, manifest_sha256="a" * 64, prepared_sha256="c" * 64
    )
    assert isinstance(receipt, AssetCoverageReceipt)
    assert receipt.required_ready
    with pytest.raises(runtime.PreparationError):
        runtime._validated_asset_coverage(value, manifest_sha256="b" * 64, prepared_sha256="c" * 64)
    with pytest.raises(runtime.PreparationError):
        runtime._validated_asset_coverage(
            {**value, "findings": ["required asset missing"]},
            manifest_sha256="a" * 64,
            prepared_sha256="c" * 64,
        )


def test_retained_manuscript_json_lists_validate_against_real_coherence_contract():
    from edgar_core.coherence import RetainedManuscript

    value = {
        "source_mode": "ai",
        "chapters": [
            {
                "chapter_id": "chapter_one",
                "act_id": "act_one",
                "order": 1,
                "role": "setup",
                "scene_ids": ["scene_one"],
                "advance_ids": ["advance_one"],
                "text": "The keeper waits at the dock.",
            }
        ],
    }
    guide, serialized = runtime._render_private_manuscript(value)
    manuscript = RetainedManuscript.model_validate_json(serialized)
    assert guide == "The keeper waits at the dock."
    assert manuscript.chapters[0].scene_ids == ("scene_one",)


def test_duplicate_publication_invocation_is_serialized_and_idempotent(
    private_meta_dir, monkeypatch
):
    binding = _binding(run_id="prep-" + "a" * 32)
    artifact = build_artifact(
        binding,
        stage=PreparationStage.P12,
        task_id="seal_inventory",
        artifact_kind="publication_inventory",
        payload={"inventory": "sealed"},
        dependency_artifacts=(),
        created_at="2026-01-01T00:00:00+00:00",
    )
    receipt = build_validation_receipt(
        "offline_test",
        artifacts=(artifact,),
        dependencies=(),
        passed=True,
        details={"accepted": True},
        created_at="2026-01-01T00:00:00+00:00",
    )
    publication = build_publication(
        binding,
        artifacts=(artifact,),
        validation_receipts=(receipt,),
        findings=(),
    )
    state = SimpleNamespace(
        status=PreparationJobStatus.PUBLISHING,
        binding=binding,
        artifacts={artifact.artifact_id: artifact},
    )
    completed = False
    publish_calls = 0

    monkeypatch.setattr(runtime.PreparationStore, "load_run", lambda *_: state)
    monkeypatch.setattr(runtime, "_current_source_matches", lambda *_: True)

    def is_complete(*_):
        return completed

    def publish_once(*_):
        nonlocal completed, publish_calls
        publish_calls += 1
        time.sleep(0.05)
        completed = True
        return publication.bundle_sha256

    monkeypatch.setattr(runtime, "_publication_candidate_complete", is_complete)
    monkeypatch.setattr(runtime, "_publish_candidate_locked", publish_once)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: runtime._publish_candidate("private", "d" * 32, publication), range(2)
            )
        )
    assert results == [publication.bundle_sha256, publication.bundle_sha256]
    assert publish_calls == 1


def test_recovered_publisher_rejects_an_incomplete_asset_bundle(private_meta_dir, monkeypatch):
    """A superficially accepted inventory cannot bypass missing required asset bytes."""
    from datetime import UTC, datetime

    from test_prepared_campaign import _fixture

    from edgar_core.assets import AssetCoverageReceipt
    from edgar_core.manifests import manifest_digest
    from edgar_core.preparation import (
        PreparationTask,
        PreparationTaskState,
        PreparationTaskStatus,
    )
    from edgar_core.prepared import prepared_digest
    from ingestion.artifacts import text_digest
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.quality import check_text

    slug = "accepted-bundle"
    run_id = "prep-" + "8" * 32
    token = "9" * 32
    binding = _binding(slug=slug, run_id=run_id)
    store = runtime.PreparationStore()
    store.create_run(binding)

    chapter_text = (
        "At the old ferry dock, the keeper waves for help while the guards close in. "
        "Mira can bargain with them, sneak past their watch, or fight before the tide rises."
    )
    page_text = "\n\n" + chapter_text
    page = ExtractedPage(1, page_text, "generated", check_text(page_text))
    extraction = Extraction(slug, "0" * 64, 1, [page], rules_edition="2014")
    source_sha256 = extraction.text_sha256()
    extraction.source_sha256 = source_sha256
    extraction_payload = {
        "source": slug,
        "source_sha256": source_sha256,
        "extraction_sha256": extraction.text_sha256(),
        "rules_edition": "2014",
        "pages": [{"page": 1, "text": page_text, "method": "generated"}],
    }

    manifest, prepared_campaign = _fixture(slug)
    passage = manifest.passages[0].model_copy(
        update={
            "start": 2,
            "end": len(page_text),
            "text_sha256": text_digest(chapter_text),
        }
    )
    manifest = manifest.model_copy(
        update={
            "source": slug,
            "source_sha256": source_sha256,
            "extraction_sha256": extraction.text_sha256(),
            "passages": [passage],
            "prepared": prepared_campaign,
        }
    )
    assert (
        runtime._make_extraction(slug, binding, extraction_payload).text_sha256() == source_sha256
    )

    manuscript = {
        "source_mode": "ai",
        "chapters": [
            {
                "chapter_id": "chapter_one",
                "act_id": "act_one",
                "order": 1,
                "role": "setup",
                "scene_ids": ["dock"],
                "advance_ids": ["advance_one"],
                "text": chapter_text,
            }
        ],
    }
    coverage = AssetCoverageReceipt(
        manifest_sha256=manifest_digest(manifest),
        prepared_sha256=prepared_digest(prepared_campaign),
        coverage=[],
    )

    artifacts = []
    artifacts.append(
        build_artifact(
            binding,
            stage=PreparationStage.P08,
            task_id="retain_source",
            artifact_kind="canonical_extraction",
            payload=extraction_payload,
            dependency_artifacts=(),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    artifacts.append(
        build_artifact(
            binding,
            stage=PreparationStage.P08,
            task_id="retain_source",
            artifact_kind="retained_manuscript",
            payload={"manuscript": manuscript},
            dependency_artifacts=(artifacts[0],),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    artifacts.append(
        build_artifact(
            binding,
            stage=PreparationStage.P09,
            task_id="final_review",
            artifact_kind="canonical_manifest",
            payload={"manifest": manifest.model_dump(mode="json")},
            dependency_artifacts=(artifacts[0], artifacts[1]),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    artifacts.append(
        build_artifact(
            binding,
            stage=PreparationStage.P09,
            task_id="final_review",
            artifact_kind="prepared_campaign",
            payload={"prepared": prepared_campaign.model_dump(mode="json")},
            dependency_artifacts=(artifacts[2],),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    artifacts.append(
        build_artifact(
            binding,
            stage=PreparationStage.P07,
            task_id="asset_coverage",
            artifact_kind="asset_coverage",
            payload={"receipt": coverage.model_dump(mode="json")},
            dependency_artifacts=(artifacts[2], artifacts[3]),
            created_at="2026-01-01T00:00:00+00:00",
        )
    )
    artifact_tuple = tuple(artifacts)
    receipt = build_validation_receipt(
        "p12_publication_inventory",
        artifacts=artifact_tuple,
        dependencies=(),
        passed=True,
        details={"accepted_inventory": True},
        created_at="2026-01-01T00:00:00+00:00",
    )
    publication = build_publication(
        binding,
        artifacts=artifact_tuple,
        validation_receipts=(receipt,),
        findings=(),
    )
    task = PreparationTask(
        task_id="seal_bundle",
        stage=PreparationStage.P12,
        task_kind="seal_bundle",
        max_input_tokens=0,
        max_output_tokens=1,
        max_provider_calls=1,
        max_cost_microusd=0,
        requires_provider=False,
    )
    task_state = PreparationTaskState(
        task=task,
        status=PreparationTaskStatus.ACCEPTED,
        accepted_artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        validation_receipt_ids=(receipt.validator_id + ":" + receipt.details_sha256,),
        updated_at=datetime.now(UTC).isoformat(),
    )
    store.mutate_run(
        slug,
        run_id,
        update=lambda data: data.update(
            status="publishing",
            current_stage="P12",
            stage_statuses={"P12": "accepted"},
            tasks={"P12:seal_bundle": task_state.model_dump(mode="json")},
            artifacts={
                artifact.artifact_id: artifact.model_dump(mode="json") for artifact in artifacts
            },
            validation_receipts={
                receipt.validator_id + ":" + receipt.details_sha256: receipt.model_dump(mode="json")
            },
        ),
    )
    adventures_meta.save_meta(
        slug,
        {
            "private_preparation": True,
            "preparation_run_id": run_id,
            "attempt": {"id": token},
            "job": {
                "job_id": run_id.removeprefix("prep-"),
                "attempt_id": token,
                "state": "running",
            },
            "status": "processing",
        },
    )
    monkeypatch.setattr(runtime, "_current_source_matches", lambda *_: True)
    from ingestion import embed

    monkeypatch.setattr(embed, "_embed_and_upsert", lambda chunks, *_args, **_kwargs: len(chunks))

    with pytest.raises(runtime.PreparationError, match="Required assets"):
        runtime._publish_candidate(slug, token, publication)
    assert not adventures_meta.load_meta(slug).get("preparation_publication_candidate")
    assert store.load_run(slug, run_id).status == PreparationJobStatus.PUBLISHING


def test_lease_wait_does_not_spin_while_a_worker_lease_is_live():
    task = SimpleNamespace(stage=SimpleNamespace(value="P01"), task_id="bind_commission")
    state = SimpleNamespace(
        current_stage=SimpleNamespace(value="P01"),
        stage_plans={"P01": SimpleNamespace(tasks=(task,))},
        tasks={
            "P01:bind_commission": SimpleNamespace(
                status=SimpleNamespace(value="running"),
                lease_expires_at="2099-01-01T00:00:00+00:00",
            )
        },
    )
    assert runtime._active_lease_wait_seconds(state) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["preparing", "ready"])
@pytest.mark.parametrize("mirrored_run_id", [True, False])
async def test_startup_recovery_claims_interrupted_private_checkpoint(
    private_meta_dir, monkeypatch, status, mirrored_run_id
):
    run_id = "prep-" + "6" * 32
    binding = _binding(slug="restart", run_id=run_id)
    store = runtime.PreparationStore()
    state = store.create_run(binding)
    store.mutate_run("restart", run_id, update=lambda payload: payload.update(status=status))
    adventures_meta.save_meta(
        "restart",
        {
            "private_preparation": True,
            "status": "failed",
            "preparation_run_id": run_id if mirrored_run_id else None,
            "job": {
                "job_id": run_id.removeprefix("prep-"),
                "kind": "generation",
                "state": "interrupted",
            },
        },
    )
    started = []

    async def fake_prepare(slug, *, attempt_token=None, **kwargs):
        started.append((slug, attempt_token))
        return state

    monkeypatch.setattr(runtime, "prepare_adventure", fake_prepare)
    assert await runtime.recover_private_preparations() == ["restart"]
    await asyncio.sleep(0)
    meta = adventures_meta.load_meta("restart")
    assert meta["status"] == "processing"
    assert meta["job"]["state"] == "queued"
    assert meta["attempt"]["id"] == started[0][1]
    assert started[0][0] == "restart"
    assert meta["preparation_run_id"] == run_id
    assert await runtime.recover_private_preparations() == []


@pytest.mark.parametrize(
    "code",
    [
        "required_books_unavailable",
        "required_book_unavailable",
        "required_books_missing",
        "book_version_unavailable",
        "unsupported_party",
        "unsupported_edition",
        "unsupported_progression",
        "unsupported_hero_capability",
        "budget_exhausted",
        "providerbudget_exhausted",
        "repair_budget_exhausted",
        "required_assets_unavailable",
    ],
)
def test_commission_level_failures_have_fixed_safe_restart_messages(code):
    assert runtime._needs_new_commission(code)
    assert "private" not in runtime._preparation_failure_message(code).lower()
    assert "start a new" in runtime._preparation_failure_message(code).lower()
