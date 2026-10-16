"""Durable image reservation and fencing tests use only temporary local files."""

import base64
import io
import json

import pytest

from apps.api.services import adventure_assets as assets
from edgar_core.assets import AssetCoverage, AssetPlan, AssetPlanItem
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


def test_image_reservations_are_budgeted_and_old_attempts_are_fenced(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    binding = {
        "collection": "synthetic",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "prepared_sha256": "c" * 64,
        "job_key": "d" * 64,
        "max_image_calls": 2,
        "max_image_cost_microusd": 500_000,
        "max_image_call_cost_microusd": 250_000,
        "image_price_revision": "vendor-price-2026-09",
        "image_price_model": "gpt-image-1",
        "image_price_size": "1024x1024",
        "image_price_quality": "medium",
        "timeout_seconds": 1,
    }
    first = assets._reserve_image_call(**binding)
    job_path = assets._version_dir("synthetic", "a" * 64, "b" * 64) / "jobs" / f"{'d' * 64}.json"
    job = json.loads(job_path.read_text())
    job["stale_after"] = 0
    job_path.write_text(json.dumps(job))

    second = assets._reserve_image_call(**binding)
    assert second != first
    assert not assets._finish_job(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        job_key="d" * 64,
        attempt_id=first,
        state="completed",
    )
    assert assets._finish_job(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        job_key="d" * 64,
        attempt_id=second,
        state="failed",
    )

    usage = assets.image_usage(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        prepared_sha256="c" * 64,
    )
    assert usage["reserved"] == 2
    assert usage["reserved_cost_microusd"] == 500_000
    assert usage["failed_cost_microusd"] == 500_000
    assert usage["failed"] == 2
    assert usage["running"] == 0
    assert usage["job_keys"] == ["d" * 64]


def test_completed_image_job_cannot_be_reserved_again(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    binding = {
        "collection": "synthetic",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "prepared_sha256": "c" * 64,
        "job_key": "d" * 64,
        "max_image_calls": 1,
        "max_image_cost_microusd": 300_000,
        "max_image_call_cost_microusd": 300_000,
        "image_price_revision": "vendor-price-2026-09",
        "image_price_model": "gpt-image-1",
        "image_price_size": "1024x1024",
        "image_price_quality": "medium",
        "timeout_seconds": 5,
    }
    attempt = assets._reserve_image_call(**binding)
    assert assets._finish_job(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        job_key="d" * 64,
        attempt_id=attempt,
        state="completed",
    )
    with pytest.raises(assets.AssetServiceError, match="completed during reservation"):
        assets._reserve_image_call(**binding)
    usage = assets.image_usage(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        prepared_sha256="c" * 64,
    )
    assert usage["reserved"] == 1


def test_image_spend_reserves_call_cap_and_refunds_only_authoritative_actual(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    binding = {
        "collection": "synthetic",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "prepared_sha256": "c" * 64,
        "job_key": "d" * 64,
        "max_image_calls": 3,
        "max_image_cost_microusd": 500_000,
        "max_image_call_cost_microusd": 250_000,
        "image_price_revision": "vendor-price-2026-09",
        "image_price_model": "gpt-image-1",
        "image_price_size": "1024x1024",
        "image_price_quality": "medium",
        "timeout_seconds": 5,
    }
    attempt = assets._reserve_image_call(**binding)
    assert assets._finish_job(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        job_key="d" * 64,
        attempt_id=attempt,
        state="completed",
        actual_cost_microusd=75_000,
    )
    usage = assets.image_usage(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        prepared_sha256="c" * 64,
    )
    assert usage["gross_reserved_cost_microusd"] == 250_000
    assert usage["reserved_cost_microusd"] == 75_000
    assert usage["succeeded_cost_microusd"] == 75_000
    assert usage["refunded_cost_microusd"] == 175_000

    next_attempt = assets._reserve_image_call(**{**binding, "job_key": "e" * 64})
    assert next_attempt
    with pytest.raises(assets.ImageBudgetExceeded):
        assets._reserve_image_call(**{**binding, "job_key": "f" * 64})


def test_run_image_reservations_remain_bounded_across_repair_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    run_id = "prep-" + "9" * 32
    base = {
        "collection": "synthetic",
        "max_image_calls": 2,
        "max_image_cost_microusd": 500_000,
        "max_image_call_cost_microusd": 250_000,
        "image_price_revision": "vendor-price-2026-09",
        "image_price_model": "gpt-image-1",
        "image_price_size": "1024x1024",
        "image_price_quality": "medium",
        "timeout_seconds": 1,
        "run_id": run_id,
    }
    first = assets._reserve_image_call(
        **base,
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        prepared_sha256="c" * 64,
        job_key="d" * 64,
    )
    assert assets._finish_job(
        collection="synthetic",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        job_key="d" * 64,
        attempt_id=first,
        state="failed",
    )
    # Repair changes all version digests, while the same run-level allowance is charged.
    second = assets._reserve_image_call(
        **base,
        source_sha256="e" * 64,
        manifest_sha256="f" * 64,
        prepared_sha256="1" * 64,
        job_key="2" * 64,
    )
    assert assets._finish_job(
        collection="synthetic",
        source_sha256="e" * 64,
        manifest_sha256="f" * 64,
        job_key="2" * 64,
        attempt_id=second,
        state="failed",
    )
    with pytest.raises(assets.ImageBudgetExceeded):
        assets._reserve_image_call(
            **base,
            source_sha256="3" * 64,
            manifest_sha256="4" * 64,
            prepared_sha256="5" * 64,
            job_key="6" * 64,
        )
    usage = assets.image_usage(
        collection="synthetic",
        source_sha256="e" * 64,
        manifest_sha256="f" * 64,
        prepared_sha256="1" * 64,
        run_id=run_id,
    )
    assert usage["reserved"] == 2
    assert usage["reserved_cost_microusd"] == 500_000
    assert usage["failed"] == 2
    assert usage["failed_cost_microusd"] == 500_000
    assert len(usage["job_keys"]) == 2


def test_commissioned_image_budget_freezes_matching_profile_and_trusted_price(monkeypatch):
    profiles = assets.current_model_profiles()
    profile = assets.effective_image_profile("image.illustration", profiles)
    price = {
        "revision": "documented-price-test",
        "model": profile.model,
        "size": profile.size,
        "quality": profile.quality,
        "unit_cost_microusd": 125_000,
    }
    monkeypatch.setenv("EDGAR_TRUSTED_IMAGE_PRICE_RECORD", json.dumps(price))
    frozen = assets.commissioned_image_budget(
        profile_snapshot=profiles.to_dict(),
        max_image_calls=4,
        max_image_cost_microusd=400_000,
    )
    assert frozen["max_image_calls"] == 4
    assert frozen["max_image_call_cost_microusd"] == 125_000
    assert frozen["image_price_revision"] == "documented-price-test"
    assert frozen["effective_profile"] == profile.to_dict()
    assert frozen["profile_digest"] == profiles.digest

    price["model"] = "other-image-model"
    monkeypatch.setenv("EDGAR_TRUSTED_IMAGE_PRICE_RECORD", json.dumps(price))
    with pytest.raises(assets.AssetServiceError, match="does not match"):
        assets.commissioned_image_budget(
            profile_snapshot=profiles.to_dict(),
            max_image_calls=4,
            max_image_cost_microusd=400_000,
        )


def test_image_budget_requires_frozen_total_and_per_call_cost_caps():
    with pytest.raises(ValueError, match="per-call image cost cap"):
        assets.AssetImageBudget(max_image_calls=1, max_image_cost_microusd=10)
    with pytest.raises(ValueError, match="exceeds the total"):
        assets.AssetImageBudget(
            max_image_calls=1,
            max_image_cost_microusd=10,
            max_image_call_cost_microusd=20,
        )


@pytest.mark.asyncio
async def test_production_image_adapter_requires_server_trusted_documented_price(monkeypatch):
    from tests.test_prepared_campaign import _fixture as prepared_fixture

    monkeypatch.delenv("EDGAR_TRUSTED_IMAGE_PRICE_RECORD", raising=False)
    manifest, prepared = prepared_fixture()
    manifest.prepared = prepared
    manifest.reviewed_sha256 = manifest_digest(manifest)
    profiles = assets.current_model_profiles()
    profile = assets.effective_image_profile("image.illustration", profiles)

    with pytest.raises(assets.AssetServiceError, match="server-configured documented price"):
        await assets._run_image_job(
            collection="synthetic",
            manifest=manifest,
            prepared=prepared,
            item_id="illustration_one",
            prompt="Decorative illustration.",
            scene_ids=["dock"],
            provider=assets.OpenAIImageProvider(),
            profile=profile,
            profile_digest=profiles.digest,
            max_image_calls=1,
            max_image_cost_microusd=500_000,
            max_image_call_cost_microusd=250_000,
            image_price_revision="caller-invented-price",
            image_price_model=profile.model,
            image_price_size=profile.size,
            image_price_quality=profile.quality,
            retry_failed=False,
            on_reserved=lambda _job_key: pytest.fail("Unknown pricing must fail before reservation."),
        )


def test_receipt_can_report_act_and_scene_map_coverage():
    plan = AssetPlan(
        source_mode="pdf",
        manifest_sha256="a" * 64,
        prepared_sha256="b" * 64,
        items=[
            AssetPlanItem(
                asset_id="map_act_one",
                kind="schematic_map",
                required=True,
                act_id="act_one",
                scene_ids=["harbor", "tower"],
            )
        ],
        required_act_ids=["act_one"],
        required_scene_ids=["harbor", "tower"],
    )
    act_coverage, scene_coverage = assets._scope_coverage(
        plan,
        [
            AssetCoverage(
                asset_id="map_act_one",
                kind="schematic_map",
                required=True,
                status="ready",
                artifact_sha256="c" * 64,
            )
        ],
    )
    assert [(item.scope_id, item.status) for item in act_coverage] == [("act_one", "ready")]
    assert [(item.scope_id, item.status) for item in scene_coverage] == [
        ("harbor", "ready"),
        ("tower", "ready"),
    ]


def test_player_asset_alias_is_session_scoped():
    assert assets._public_asset_id(1, "schematic-map") != assets._public_asset_id(2, "schematic-map")


def test_handout_assets_require_player_passage_knowledge_from_the_same_source_version(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    manifest = AdventureManifest.model_validate(
        {
            "source": "synthetic",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "harbor",
            "scenes": [{"scene_id": "harbor", "title": "The harbor"}],
            "passages": [
                {
                    "passage_id": "same_passage_id",
                    "page": 1,
                    "start": 0,
                    "end": 21,
                    "scene_ids": ["harbor"],
                    "audience": "player",
                }
            ],
            "reviewed_by": "test",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    content = b"An exact player handout."
    record = assets.AssetArtifactRecord(
        artifact_id="handout_one",
        plan_asset_id="handout_one",
        job_key="deterministic",
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        extraction_sha256=manifest.extraction_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        prepared_sha256="c" * 64,
        kind="handout",
        sha256=assets._sha256(content),
        media_type="text/plain; charset=utf-8",
        passage_ids=["same_passage_id"],
        relative_file="handout_one.txt",
    )
    private_content = b'<svg xmlns="http://www.w3.org/2000/svg"><text>DM secret</text></svg>'
    private_map = record.model_copy(
        update={
            "artifact_id": "private_map",
            "kind": "schematic_map",
            "passage_ids": [],
            "scene_ids": ["harbor"],
            "relative_file": "private_map.svg",
            "sha256": assets._sha256(private_content),
        }
    )
    unsigned_visual = record.model_copy(
        update={
            "artifact_id": "unsigned_visual",
            "kind": "source_map",
            "scene_ids": ["harbor"],
            "passage_ids": [],
            "relative_file": "unsigned_visual.png",
            "media_type": "image/png",
        }
    )
    empty_gate = unsigned_visual.model_copy(
        update={
            "artifact_id": "empty_gate",
            "scene_ids": [],
            "passage_ids": [],
            "entity_id": None,
            "relative_file": "empty_gate.png",
        }
    )
    version_dir = assets._version_dir("synthetic", manifest.source_sha256, manifest.reviewed_sha256)
    version_dir.mkdir(parents=True)
    (version_dir / record.relative_file).write_bytes(content)
    (version_dir / private_map.relative_file).write_bytes(private_content)
    assets._atomic_write(
        assets._catalog_path("synthetic", manifest.source_sha256, manifest.reviewed_sha256),
        assets.AssetCatalog(
            entries={
                record.artifact_id: record,
                private_map.artifact_id: private_map,
                unsigned_visual.artifact_id: unsigned_visual,
                empty_gate.artifact_id: empty_gate,
            }
        ).model_dump_json().encode(),
    )

    target = StoryRef(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        kind="scene",
        canonical_id="harbor",
    )
    def fact_from(source_collection: str) -> StoryFact:
        source_ref = {
            "reference_id": "d" * 64,
            "audience": "player",
            "collection": source_collection,
            "source_sha256": manifest.source_sha256,
            "manifest_sha256": manifest.reviewed_sha256,
            "passage_id": "same_passage_id",
        }
        effect = accept_story_effect(
            StoryEffect(kind="learn_place", target=target, status="known"),
            turn_id="turn-one",
            source_refs=[source_ref],
            display_name="The harbor",
        )
        return StoryFact(**effect.model_dump(), fact_id=story_target_id(target), event_id=1)

    wrong_binding = fact_from("other-collection")
    assert assets.authorized_player_assets(
        session_id=7, collection="synthetic", manifest=manifest, facts=[wrong_binding]
    ) == []
    correct_binding = fact_from("synthetic")
    assert len(
        assets.authorized_player_assets(
            session_id=7, collection="synthetic", manifest=manifest, facts=[correct_binding]
        )
    ) == 1


def test_signed_visual_release_is_digest_and_full_scope_bound(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from edgar_core.assets import (
        AssetCoverage,
        AssetCoverageReceipt,
        AssetScopeCoverage,
        IndependentVisualReviewDecision,
    )

    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    manifest = AdventureManifest.model_validate(
        {
            "source": "synthetic",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "harbor",
            "scenes": [{"scene_id": "harbor", "title": "The harbor"}],
            "passages": [
                {
                    "passage_id": "opening",
                    "page": 1,
                    "start": 0,
                    "end": 6,
                    "text_sha256": "c" * 64,
                    "scene_ids": ["harbor"],
                    "audience": "player",
                }
            ],
            "reviewed_by": "test",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    from PIL import Image

    image_bytes = io.BytesIO()
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(image_bytes, format="PNG")
    retained = assets._sanitize_raster(image_bytes.getvalue(), "image/png")
    record = assets.AssetArtifactRecord(
        artifact_id="source_map_one",
        plan_asset_id="source_map_one",
        job_key="exact-job",
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        extraction_sha256=manifest.extraction_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        prepared_sha256="d" * 64,
        kind="source_map",
        sha256=assets._sha256(retained),
        media_type="image/png",
        scene_ids=["harbor"],
        relative_file="source_map_one.png",
    )
    version_dir = assets._version_dir("synthetic", manifest.source_sha256, manifest.reviewed_sha256)
    version_dir.mkdir(parents=True)
    (version_dir / record.relative_file).write_bytes(retained)
    assets._atomic_write(
        assets._catalog_path("synthetic", manifest.source_sha256, manifest.reviewed_sha256),
        assets.AssetCatalog(entries={record.artifact_id: record}).model_dump_json().encode(),
    )
    source_ref = {
        "reference_id": "e" * 64,
        "audience": "player",
        "collection": "synthetic",
        "source_sha256": manifest.source_sha256,
        "manifest_sha256": manifest.reviewed_sha256,
        "passage_id": "opening",
    }
    target = StoryRef(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        kind="scene",
        canonical_id="harbor",
    )
    effect = accept_story_effect(
        StoryEffect(kind="visit", target=target, status="visited"),
        turn_id="turn-one",
        source_refs=[source_ref],
        display_name="The harbor",
    )
    fact = StoryFact(**effect.model_dump(), fact_id=story_target_id(target), event_id=1)
    assert assets.authorized_player_assets(
        session_id=8, collection="synthetic", manifest=manifest, facts=[fact]
    ) == []

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PUBLIC_KEY", base64.b64encode(public_bytes).decode())
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PRIVATE_KEY", base64.b64encode(private_bytes).decode())
    candidates = assets.enumerate_private_visual_candidates(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.data == retained
    assert candidate.artifact_sha256 == record.sha256
    assert candidate.scene_ids == ["harbor"]
    assert candidate.passage_ids == []
    assert candidate.entity_id is None

    pending_receipt = AssetCoverageReceipt(
        manifest_sha256=record.manifest_sha256,
        prepared_sha256=record.prepared_sha256,
        coverage=[
            AssetCoverage(
                asset_id=record.plan_asset_id,
                kind=record.kind,
                required=True,
                status="ready",
                artifact_sha256=record.sha256,
                player_review_status="pending",
            )
        ],
        act_coverage=[
            AssetScopeCoverage(scope="act", scope_id="act_one", required=True, status="ready")
        ],
        scene_coverage=[
            AssetScopeCoverage(scope="scene", scope_id="harbor", required=True, status="ready")
        ],
    )
    assert pending_receipt.required_ready is False
    rejected = IndependentVisualReviewDecision(
        candidate_id=candidate.candidate_id,
        artifact_sha256=candidate.artifact_sha256,
        decision="rejected",
        reasons=["A visible map label contradicts the approved scene description."],
    )
    rejected_receipt = assets.apply_rejected_visual_review_to_coverage(
        receipt=pending_receipt,
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        decision=rejected,
    )
    assert rejected_receipt.required_ready is False
    assert rejected_receipt.coverage[0].player_review_status == "rejected"
    assert assets.sign_and_persist_visual_review(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        decision=rejected,
    ) is None

    approved = IndependentVisualReviewDecision(
        candidate_id=candidate.candidate_id,
        artifact_sha256=candidate.artifact_sha256,
        decision="approved",
        reasons=["The complete image was reviewed for scope and visible text."],
    )
    signed = assets.sign_and_persist_visual_review(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        decision=approved,
    )
    assert signed is not None
    assert signed.candidate_id == candidate.candidate_id
    assert signed.scene_ids == ["harbor"]
    assert signed.reviewer_id == "p09_independent_critic"
    assert assets.apply_visual_review_to_coverage(
        receipt=pending_receipt,
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        review_artifact=signed,
    ).required_ready is True

    # A review attaches to the private record; the normal committed-fact gate still decides
    # when the session receives its opaque player asset URL.
    assert assets.authorized_player_assets(
        session_id=8, collection="synthetic", manifest=manifest, facts=[]
    ) == []
    assert len(
        assets.authorized_player_assets(
            session_id=8, collection="synthetic", manifest=manifest, facts=[fact]
        )
    ) == 1

    wrong_digest = approved.model_copy(update={"artifact_sha256": "f" * 64})
    with pytest.raises(assets.AssetServiceError, match="digest"):
        assets.sign_and_persist_visual_review(
            collection="synthetic",
            source_sha256=manifest.source_sha256,
            manifest_sha256=manifest.reviewed_sha256,
            decision=wrong_digest,
        )


def test_asset_coverage_revalidates_inventory_requiredness_signatures_and_bytes(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from PIL import Image

    from edgar_core.assets import (
        AssetCoverageReceipt,
        AssetPlanItem,
        IndependentVisualReviewDecision,
    )
    from tests.test_prepared_campaign import _approved, _fixture as prepared_fixture

    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path)
    raw_manifest, prepared = prepared_fixture()
    manifest = _approved(raw_manifest, prepared)
    base_plan = assets.plan_assets(manifest, [], [])
    visual_item = AssetPlanItem(
        asset_id="source_map_one",
        kind="source_map",
        required=True,
        scene_ids=["dock"],
    )
    plan = base_plan.model_copy(update={"items": [*base_plan.items, visual_item]})

    schematic = assets._record_artifact(
        collection="synthetic",
        manifest=manifest,
        prepared=prepared,
        item_id=base_plan.items[0].asset_id,
        kind="schematic_map",
        content=assets._render_act_map(manifest, "act_one"),
        media_type="image/svg+xml",
        scene_ids=["dock"],
        passage_ids=[],
        entity_id=None,
    )
    image_buffer = io.BytesIO()
    Image.new("RGB", (3, 2), color=(21, 43, 65)).save(image_buffer, format="PNG")
    visual = assets._record_artifact(
        collection="synthetic",
        manifest=manifest,
        prepared=prepared,
        item_id=visual_item.asset_id,
        kind="source_map",
        content=image_buffer.getvalue(),
        media_type="image/png",
        scene_ids=visual_item.scene_ids,
        passage_ids=[],
        entity_id=None,
    )
    coverage = [
        AssetCoverage(
            asset_id=schematic.plan_asset_id,
            kind="schematic_map",
            required=True,
            status="ready",
            artifact_sha256=schematic.sha256,
        ),
        AssetCoverage(
            asset_id=visual_item.asset_id,
            kind="source_map",
            required=True,
            status="ready",
            artifact_sha256=visual.sha256,
            player_review_status="pending",
        ),
    ]
    acts, scenes = assets._scope_coverage(plan, coverage)
    receipt = AssetCoverageReceipt(
        manifest_sha256=manifest.reviewed_sha256,
        prepared_sha256=plan.prepared_sha256,
        coverage=coverage,
        act_coverage=acts,
        scene_coverage=scenes,
    )
    assert assets.validate_asset_coverage("synthetic", manifest, plan, receipt) is False

    forged_approval = receipt.model_copy(
        update={
            "coverage": [
                coverage[0],
                coverage[1].model_copy(update={"player_review_status": "approved"}),
            ]
        }
    )
    assert forged_approval.required_ready is True
    assert assets.validate_asset_coverage("synthetic", manifest, plan, forged_approval) is False

    missing_item = receipt.model_copy(update={"coverage": coverage[:1]})
    assert assets.validate_asset_coverage("synthetic", manifest, plan, missing_item) is False

    changed_required = receipt.model_copy(
        update={
            "coverage": [
                coverage[0],
                coverage[1].model_copy(update={"required": False}),
            ]
        }
    )
    assert assets.validate_asset_coverage("synthetic", manifest, plan, changed_required) is False

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PRIVATE_KEY", base64.b64encode(private_bytes).decode())
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PUBLIC_KEY", base64.b64encode(public_bytes).decode())
    candidate = assets.enumerate_private_visual_candidates(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
    )[0]
    signed_review = assets.sign_and_persist_visual_review(
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        decision=IndependentVisualReviewDecision(
            candidate_id=candidate.candidate_id,
            artifact_sha256=candidate.artifact_sha256,
            decision="approved",
            reasons=["The exact retained visual was reviewed."],
        ),
    )
    assert signed_review is not None
    accepted = assets.apply_visual_review_to_coverage(
        receipt=receipt,
        collection="synthetic",
        source_sha256=manifest.source_sha256,
        review_artifact=signed_review,
    )
    assert assets.validate_asset_coverage("synthetic", manifest, plan, accepted) is True

    catalog_path = assets._catalog_path(
        "synthetic", manifest.source_sha256, manifest.reviewed_sha256
    )
    original_catalog = catalog_path.read_bytes()
    catalog = assets._read_catalog(catalog_path)
    bad_signature = signed_review.model_copy(
        update={"signature": "A" * len(signed_review.signature)}
    )
    catalog.entries[visual.artifact_id] = visual.model_copy(
        update={"visual_review": bad_signature}
    )
    assets._atomic_write(catalog_path, catalog.model_dump_json().encode())
    assert assets.validate_asset_coverage("synthetic", manifest, plan, accepted) is False

    assets._atomic_write(catalog_path, original_catalog)
    image_buffer = io.BytesIO()
    Image.new("RGB", (3, 2), color=(65, 43, 21)).save(image_buffer, format="PNG")
    (assets._version_dir("synthetic", manifest.source_sha256, manifest.reviewed_sha256) / visual.relative_file).write_bytes(
        image_buffer.getvalue()
    )
    assert assets.validate_asset_coverage("synthetic", manifest, plan, accepted) is False


def test_raster_sanitization_removes_exif_and_rejects_invalid_bytes():
    from PIL import Image

    source = Image.new("RGB", (3, 2), color=(40, 50, 60))
    exif = Image.Exif()
    exif[0x010E] = "private scanner annotation"
    raw = io.BytesIO()
    source.save(raw, format="JPEG", exif=exif)
    sanitized = assets._sanitize_raster(raw.getvalue(), "image/jpeg")
    with Image.open(io.BytesIO(sanitized)) as retained:
        assert retained.format == "PNG"
        assert not retained.getexif()
        assert "exif" not in retained.info
    with pytest.raises(assets.AssetServiceError, match="validation"):
        assets._sanitize_raster(b"not an image", "image/png")
