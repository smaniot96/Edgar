from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from edgar_core.manifests import ManifestError

from apps.api.schemas.campaign import SourceRead
from apps.api.services import campaign_readiness, campaign_sources
from apps.api.services.prepared_play import PreparedPlayError


def _campaign(*, mode="module", status="active", edition="2014"):
    return SimpleNamespace(
        id=7,
        status=status,
        source_mode=mode,
        adventure_collections=["module-a"],
        rules_edition=edition,
    )


def _source(status="ready"):
    return SourceRead(slug="module-a", title="Adventure", status=status)


@pytest.mark.asyncio
async def test_readiness_reports_review_edition_and_unavailable_checks_as_stable_blockers(
    monkeypatch,
):
    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: False)
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: (_ for _ in ()).throw(ManifestError("private validation detail")),
    )
    review = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source()]
    )
    assert review.state == "adventure_review_needed"
    assert review.blockers[0].message == "Review and publish the adventure before starting or continuing play."
    assert "private validation detail" not in review.model_dump_json()

    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: {"module-a": {"rules_edition": "2024"}},
    )
    mismatch = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source()]
    )
    assert [b.code for b in mismatch.blockers] == ["rules_edition_mismatch"]

    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: (_ for _ in ()).throw(OSError("disk detail")),
    )
    unavailable = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source()]
    )
    assert unavailable.state == "readiness_unavailable"
    assert "disk detail" not in unavailable.model_dump_json()


@pytest.mark.asyncio
async def test_readiness_distinguishes_source_and_private_preparation_states(monkeypatch):
    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: False)
    missing = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source("missing")]
    )
    assert missing.state == "source_missing"

    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: True)
    processing = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source("processing")]
    )
    failed = await campaign_readiness.evaluate_play_readiness(
        None, _campaign(), observed_sources=[_source("failed")]
    )
    assert processing.state == "preparation_in_progress"
    assert failed.state == "preparation_failed"


@pytest.mark.asyncio
async def test_session_readiness_checks_assignment_and_current_prepared_hero(monkeypatch):
    assignments = __import__("apps.api.services.character_assignments", fromlist=["*"])
    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: False)
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: {"module-a": {"rules_edition": "2014"}},
    )
    monkeypatch.setattr(
        assignments, "load_active_assignment", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        assignments, "load_active_assignment_any_campaign", AsyncMock(return_value=None)
    )
    character = SimpleNamespace(id=41, level=3)
    db = SimpleNamespace(get=AsyncMock(return_value=character))
    missing = await campaign_readiness.evaluate_play_readiness(
        db, _campaign(), character_id=41, require_character=True,
        observed_sources=[_source()],
    )
    assert missing.state == "choose_character"

    monkeypatch.setattr(
        assignments,
        "load_active_assignment_any_campaign",
        AsyncMock(return_value=SimpleNamespace(campaign_id=99)),
    )
    conflict = await campaign_readiness.evaluate_play_readiness(
        db, _campaign(), character_id=41, require_character=True,
        observed_sources=[_source()],
    )
    assert conflict.state == "character_assignment_conflict"

    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: True)
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play",
        lambda *a, **kw: (_ for _ in ()).throw(
            PreparedPlayError(409, "preparation_failed", "changed hero detail")
        ) if kw.get("character") is None or kw["character"].level != 3 else None,
    )
    character.level = 4
    changed = await campaign_readiness.evaluate_play_readiness(
        db, _campaign(), character_id=41, require_character=True,
        observed_sources=[_source()],
    )
    assert changed.state == "preparation_failed"
    with pytest.raises(HTTPException) as rejected:
        campaign_readiness.require_play_readiness(changed)
    assert rejected.value.detail == {
        "code": "preparation_failed",
        "message": "Campaign preparation needs attention before play can continue.",
    }


@pytest.mark.asyncio
async def test_homebrew_readiness_ignores_bound_modules_and_keeps_history_readable(monkeypatch):
    monkeypatch.setattr(campaign_sources, "source_reads", AsyncMock(side_effect=AssertionError))
    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: True)
    ready = await campaign_readiness.evaluate_play_readiness(
        None,
        _campaign(mode="homebrew"),
        observed_sources=[],
    )
    assert ready.play_allowed and ready.history_allowed

    ended = await campaign_readiness.evaluate_play_readiness(
        None,
        _campaign(mode="homebrew", status="ended"),
        observed_sources=[],
    )
    assert not ended.play_allowed and ended.history_allowed
    assert ended.state == "completed"
