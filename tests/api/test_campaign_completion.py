"""Completion-flag detection used to auto-end a campaign at the adventure's climax."""

from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult, FlagUpdate
from apps.api.services.campaign_completion import _completion_flagged, maybe_complete_campaign
from db.postgres.models import Campaign, CharacterAssignment


def _adj(flags):
    return AdjudicationResult(success=True, flags_set=[FlagUpdate(**f) for f in flags])


def test_none_is_not_complete() -> None:
    assert _completion_flagged(None) is False


def test_no_flags_is_not_complete() -> None:
    assert _completion_flagged(_adj([])) is False


def test_campaign_complete_true_variants() -> None:
    for value in ("true", "True", "yes", "1", "victory", "completed"):
        assert _completion_flagged(_adj([{"key": "campaign_complete", "value": value}])) is True


def test_unrelated_flag_does_not_complete() -> None:
    assert _completion_flagged(_adj([{"key": "door_unlocked", "value": "true"}])) is False


def test_campaign_complete_false_does_not_complete() -> None:
    assert _completion_flagged(_adj([{"key": "campaign_complete", "value": "false"}])) is False


async def test_automatic_completion_keeps_shared_timestamp_and_transaction(api_client, db_session):
    campaign_id = (await api_client.post("/api/seed")).json()["campaign_id"]
    campaign = await db_session.get(Campaign, campaign_id)
    assignment = (
        await db_session.execute(
            select(CharacterAssignment).where(CharacterAssignment.campaign_id == campaign_id)
        )
    ).scalar_one()
    accepted = AdjudicationResult(success=True, completion_status="accepted")
    pending = AdjudicationResult(
        success=True,
        completion_status="pending",
        flags_set=[FlagUpdate(key="campaign_complete", value="true")],
    )

    assert not await maybe_complete_campaign(db_session, campaign_id, pending)
    assert campaign.status == "active"
    assert await maybe_complete_campaign(db_session, campaign_id, accepted)
    await db_session.refresh(assignment)
    assert assignment.ended_at == campaign.ended_at
    ended_at = campaign.ended_at
    assert not await maybe_complete_campaign(db_session, campaign_id, accepted)
    assert campaign.ended_at == ended_at

    await db_session.rollback()
    await db_session.refresh(campaign)
    await db_session.refresh(assignment)
    assert campaign.status == "active"
    assert campaign.ended_at is None
    assert assignment.ended_at is None
