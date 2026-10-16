"""Durable image reservation and fencing tests use only temporary local files."""

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


def test_image_budget_requires_frozen_total_and_per_call_cost_caps():
    with pytest.raises(ValueError, match="per-call image cost cap"):
        assets.AssetImageBudget(max_image_calls=1, max_image_cost_microusd=10)
    with pytest.raises(ValueError, match="exceeds the total"):
        assets.AssetImageBudget(
            max_image_calls=1,
            max_image_cost_microusd=10,
            max_image_call_cost_microusd=20,
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
    version_dir = assets._version_dir("synthetic", manifest.source_sha256, manifest.reviewed_sha256)
    version_dir.mkdir(parents=True)
    (version_dir / record.relative_file).write_bytes(content)
    assets._atomic_write(
        assets._catalog_path("synthetic", manifest.source_sha256, manifest.reviewed_sha256),
        assets.AssetCatalog(entries={record.artifact_id: record}).model_dump_json().encode(),
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
