"""Assignment reads must retain feature counters for a campaign reopen."""

from datetime import UTC, datetime

from apps.api.schemas.character import CharacterAssignmentHistoryRead
from apps.api.services.character_assignments import resource_view


def test_assignment_response_retains_engine_action_surge_uses():
    character = {
        "class": "Fighter", "level": 4, "hp_current": 36, "hp_max": 36,
        "stats": {"class_features": {"action_surge": {"uses_remaining": 0}}},
    }
    response = CharacterAssignmentHistoryRead(
        id=1, campaign_id=2, campaign_title="Test", hp_current=36, hp_max=36,
        stats=character["stats"], inventory={}, assigned_at=datetime.now(UTC),
        ended_at=None, status="active", **resource_view(character),
    )
    assert response.model_dump()["class_features"] == {
        "action_surge": {"uses_remaining": 0, "uses_max": 1},
    }
