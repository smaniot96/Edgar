"""Completion-flag detection used to auto-end a campaign at the adventure's climax."""

from agent.models.adjudication import AdjudicationResult, FlagUpdate
from apps.api.services.campaign_completion import _completion_flagged


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
