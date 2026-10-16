"""Offline safety checks for the private preparation runtime projections."""

import sys
from types import SimpleNamespace

import pytest

from apps.api.services import adventures_meta, campaign_sources, preparation_runtime as runtime
from edgar_core.preparation import PreparationJobStatus, PublicPreparationStatus


@pytest.fixture
def private_meta_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    return tmp_path / "adventures"


def test_missing_setup_has_safe_restart_projection(private_meta_dir):
    adventures_meta.save_meta("private", {
        "private_preparation": True,
        "failure_code": "preparation_setup_required",
        "error": "private request details",
        "job": {"job_id": "a" * 32, "kind": "generation", "state": "failed"},
    })

    projected = runtime.safe_preparation_status(adventures_meta.load_meta("private"), "private")

    assert projected is not None
    assert projected.run_id == "prep-" + "a" * 32
    assert projected.status == PreparationJobStatus.BLOCKED
    assert projected.stage == "Preparation needs attention"
    assert projected.action == "restart"
    assert "private request details" not in projected.model_dump_json()


def test_private_source_read_hides_every_unready_artifact(private_meta_dir, monkeypatch):
    adventures_meta.save_meta("private", {
        "private_preparation": True,
        "status": "ready",
        "manifest": {"secret": "ending"},
        "prepared_campaign": {"secret": "branch"},
        "ingestion": {"source_sha256": "b" * 64},
        "source_filename": "private-book.pdf",
        "job": {"job_id": "c" * 32, "kind": "generation", "state": "running"},
    })
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
        runtime._make_extraction("private", binding, {
            "source": "private",
            "source_sha256": "e" * 64,
            "rules_edition": "2014",
            "pages": [{"page": 1, "text": "one exact page", "method": "generated"}],
        })


@pytest.mark.parametrize("receipt", [None, {}, {
    "manifest_sha256": "b" * 64,
    "prepared_sha256": "c" * 64,
    "coverage": [],
}])
def test_asset_coverage_requires_present_exact_receipt(receipt, monkeypatch):
    class Receipt:
        @classmethod
        def model_validate(cls, value):
            if not isinstance(value, dict) or not {
                "manifest_sha256", "prepared_sha256", "coverage"
            } <= set(value):
                raise ValueError("invalid receipt")
            return SimpleNamespace(
                manifest_sha256=value["manifest_sha256"],
                prepared_sha256=value["prepared_sha256"],
                required_ready=not value.get("findings"),
            )

    monkeypatch.setitem(
        sys.modules,
        "edgar_core.assets",
        SimpleNamespace(AssetCoverageReceipt=Receipt),
    )
    with pytest.raises(runtime.PreparationError):
        runtime._validated_asset_coverage(
            receipt,
            manifest_sha256="a" * 64,
            prepared_sha256="c" * 64,
        )
