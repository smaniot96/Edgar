from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apps.api.schemas.campaign import SourceRead
from apps.api.services import campaign_readiness, campaign_sources
from apps.api.services.prepared_play import PreparedPlayError
from edgar_core.manifests import ManifestError


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


_GATE_CASES = [
    pytest.param(
        "source_absent",
        "module",
        "active",
        "missing",
        False,
        "2014",
        None,
        "source_missing",
        409,
        "campaign_sources_unavailable",
        id="source-absent",
    ),
    pytest.param(
        "source_service_failure",
        "module",
        "active",
        None,
        False,
        "2014",
        None,
        "readiness_unavailable",
        503,
        "readiness_unavailable",
        id="source-service-failure",
    ),
    pytest.param(
        "source_unreviewed",
        "module",
        "active",
        "partial",
        False,
        "2014",
        None,
        "source_review_needed",
        409,
        "campaign_sources_unavailable",
        id="source-unreviewed",
    ),
    pytest.param(
        "source_unavailable",
        "module",
        "active",
        "unavailable",
        False,
        "2014",
        None,
        "source_unavailable",
        409,
        "campaign_sources_unavailable",
        id="source-unavailable",
    ),
    pytest.param(
        "manifest_missing",
        "module",
        "active",
        "ready",
        False,
        "2014",
        "missing",
        "adventure_review_needed",
        409,
        "adventure_manifest_review_required",
        id="manifest-missing",
    ),
    pytest.param(
        "manifest_stale",
        "module",
        "active",
        "ready",
        False,
        "2014",
        "stale",
        "adventure_review_needed",
        409,
        "adventure_manifest_review_required",
        id="manifest-stale",
    ),
    pytest.param(
        "unknown_manifest_edition",
        "module",
        "active",
        "ready",
        False,
        "2014",
        "unknown",
        "rules_edition_mismatch",
        409,
        "rules_edition_mismatch",
        id="unknown-edition",
    ),
    pytest.param(
        "active_preparation",
        "module",
        "active",
        "processing",
        True,
        "2014",
        None,
        "preparation_in_progress",
        409,
        "preparation_in_progress",
        id="active-preparation",
    ),
    pytest.param(
        "failed_preparation",
        "module",
        "active",
        "failed",
        True,
        "2014",
        None,
        "preparation_failed",
        409,
        "preparation_failed",
        id="failed-preparation",
    ),
    pytest.param(
        "unassigned_character",
        "module",
        "active",
        "ready",
        False,
        "2014",
        None,
        "choose_character",
        409,
        "character_not_assigned",
        id="unassigned-character",
    ),
    pytest.param(
        "character_in_other_campaign",
        "module",
        "active",
        "ready",
        False,
        "2014",
        None,
        "character_assignment_conflict",
        409,
        "character_assignment_conflict",
        id="assignment-conflict",
    ),
    pytest.param(
        "homebrew",
        "homebrew",
        "active",
        None,
        False,
        "2014",
        None,
        "ready",
        None,
        None,
        id="homebrew-ready",
    ),
    pytest.param(
        "completed",
        "homebrew",
        "ended",
        None,
        False,
        "2014",
        None,
        "completed",
        409,
        "completed",
        id="completed-campaign",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,mode,campaign_status,source_status,is_private,edition,manifest_state,expected_state,preflight_status,preflight_code",
    _GATE_CASES,
)
async def test_readiness_gate_matrix_matches_turn_preflight(
    monkeypatch,
    case,
    mode,
    campaign_status,
    source_status,
    is_private,
    edition,
    manifest_state,
    expected_state,
    preflight_status,
    preflight_code,
):
    """Player projection and the turn guard agree without invoking a provider."""
    from apps.api.services import character_assignments

    campaign = _campaign(mode=mode, status=campaign_status, edition=edition)
    monkeypatch.setattr(
        campaign_readiness,
        "is_private_preparation_source",
        lambda _: is_private,
    )
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play",
        lambda *args, **kwargs: None,
    )
    if case == "source_service_failure":
        monkeypatch.setattr(
            campaign_readiness,
            "source_reads",
            AsyncMock(side_effect=OSError("private storage failure")),
        )
    elif mode == "homebrew" or campaign_status == "ended":
        source_reader = AsyncMock(side_effect=AssertionError("should not read module sources"))
        monkeypatch.setattr(campaign_readiness, "source_reads", source_reader)
    else:
        monkeypatch.setattr(
            campaign_readiness,
            "source_reads",
            AsyncMock(return_value=[] if source_status == "missing" else [_source(source_status)]),
        )

    if manifest_state in ("missing", "stale"):
        monkeypatch.setattr(
            "apps.api.services.adventure_manifests.campaign_manifests",
            lambda _: (_ for _ in ()).throw(ManifestError(f"private {manifest_state} details")),
        )
    else:
        manifest_edition = "future-edition" if manifest_state == "unknown" else edition
        monkeypatch.setattr(
            "apps.api.services.adventure_manifests.campaign_manifests",
            lambda _: {"module-a": {"rules_edition": manifest_edition}},
        )

    db = None
    require_character = case in ("unassigned_character", "character_in_other_campaign")
    if require_character:
        db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(id=41)))
        monkeypatch.setattr(
            character_assignments,
            "load_active_assignment",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            character_assignments,
            "load_active_assignment_any_campaign",
            AsyncMock(
                return_value=SimpleNamespace(campaign_id=99)
                if case == "character_in_other_campaign"
                else None
            ),
        )

    readiness = await campaign_readiness.evaluate_play_readiness(
        db,
        campaign,
        character_id=41 if require_character else None,
        require_character=require_character,
    )
    assert readiness.state == expected_state
    if preflight_status is None:
        campaign_readiness.require_play_readiness(readiness)
        assert readiness.play_allowed
    else:
        with pytest.raises(HTTPException) as rejected:
            campaign_readiness.require_play_readiness(readiness)
        assert rejected.value.status_code == preflight_status
        assert rejected.value.detail["code"] == preflight_code
        assert "private" not in str(rejected.value.detail)


@pytest.mark.asyncio
async def test_saved_amendment_conflict_projects_safe_version_blocker_and_matches_preflight(
    monkeypatch,
):
    from apps.api.services import prepared_amendments
    from apps.api.services.prepared_amendments import PreparedAmendmentConflict

    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: False)
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: {"module-a": {"rules_edition": "2014"}},
    )
    replay = AsyncMock(
        side_effect=PreparedAmendmentConflict("private source digest and amendment details")
    )
    monkeypatch.setattr(prepared_amendments, "load_session_prepared_amendments", replay)
    db = SimpleNamespace()

    readiness = await campaign_readiness.evaluate_play_readiness(
        db,
        _campaign(),
        session_id=804,
        observed_sources=[_source()],
    )

    assert readiness.state == "saved_version_conflict"
    assert readiness.primary_action == "review_adventure"
    assert "private" not in readiness.model_dump_json()
    replay.assert_awaited_once_with(db, 804, 7, {"module-a": {"rules_edition": "2014"}})
    with pytest.raises(HTTPException) as rejected:
        campaign_readiness.require_play_readiness(readiness)
    assert rejected.value.status_code == 409
    assert rejected.value.detail == {
        "code": "saved_version_conflict",
        "message": readiness.blockers[0].message,
    }


@pytest.mark.asyncio
async def test_prepared_amendment_replay_rejects_an_unavailable_bound_version_read_only():
    from apps.api.services.prepared_amendments import (
        PreparedAmendmentConflict,
        load_session_prepared_amendments,
    )

    class Rows:
        def __iter__(self):
            return iter([SimpleNamespace(payload={"collection": "module-a"})])

    db = SimpleNamespace(
        scalar=AsyncMock(return_value=7),
        scalars=AsyncMock(return_value=Rows()),
        add=Mock(side_effect=AssertionError("readiness must not write")),
        commit=AsyncMock(side_effect=AssertionError("readiness must not commit")),
    )
    with pytest.raises(PreparedAmendmentConflict):
        await load_session_prepared_amendments(db, 804, 7, {})
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_saved_amendment_replay_fails_closed_without_leaking_payload(monkeypatch):
    from apps.api.services import prepared_amendments

    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: False)
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda _: {"module-a": {"rules_edition": "2014"}},
    )
    monkeypatch.setattr(
        prepared_amendments,
        "load_session_prepared_amendments",
        AsyncMock(side_effect=ValueError("corrupt amendment with private payload")),
    )
    readiness = await campaign_readiness.evaluate_play_readiness(
        SimpleNamespace(),
        _campaign(),
        session_id=805,
        observed_sources=[_source()],
    )

    assert readiness.state == "readiness_unavailable"
    assert not readiness.play_allowed
    assert "corrupt amendment" not in readiness.model_dump_json()
    with pytest.raises(HTTPException) as rejected:
        campaign_readiness.require_play_readiness(readiness)
    assert rejected.value.status_code == 503
    assert rejected.value.detail["code"] == "readiness_unavailable"


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
    assert (
        review.blockers[0].message
        == "Review and publish the adventure before starting or continuing play."
    )
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
    monkeypatch.setattr(assignments, "load_active_assignment", AsyncMock(return_value=None))
    monkeypatch.setattr(
        assignments, "load_active_assignment_any_campaign", AsyncMock(return_value=None)
    )
    character = SimpleNamespace(id=41, level=3)
    db = SimpleNamespace(get=AsyncMock(return_value=character))
    missing = await campaign_readiness.evaluate_play_readiness(
        db,
        _campaign(),
        character_id=41,
        require_character=True,
        observed_sources=[_source()],
    )
    assert missing.state == "choose_character"

    monkeypatch.setattr(
        assignments,
        "load_active_assignment_any_campaign",
        AsyncMock(return_value=SimpleNamespace(campaign_id=99)),
    )
    conflict = await campaign_readiness.evaluate_play_readiness(
        db,
        _campaign(),
        character_id=41,
        require_character=True,
        observed_sources=[_source()],
    )
    assert conflict.state == "character_assignment_conflict"

    monkeypatch.setattr(campaign_readiness, "is_private_preparation_source", lambda _: True)
    monkeypatch.setattr(
        "apps.api.services.prepared_play.require_prepared_play",
        lambda *a, **kw: (
            (_ for _ in ()).throw(
                PreparedPlayError(409, "preparation_failed", "changed hero detail")
            )
            if kw.get("character") is None or kw["character"].level != 3
            else None
        ),
    )
    character.level = 4
    changed = await campaign_readiness.evaluate_play_readiness(
        db,
        _campaign(),
        character_id=41,
        require_character=True,
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
