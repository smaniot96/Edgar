"""Exercise real session creation/turn gates without provider calls or persisted writes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from apps.api.routers import session as router
from apps.api.schemas.campaign import SourceRead
from apps.api.schemas.session import SessionCreate
from apps.api.services import campaign_readiness


@pytest.mark.parametrize("entrypoint", ["create", "turn"])
async def test_real_session_entrypoints_reject_known_blocker_before_writes(monkeypatch, entrypoint):
    campaign = SimpleNamespace(id=7, status="active", source_mode="module", adventure_collections=["missing-source"], rules_edition="2014")
    saved = SimpleNamespace(id=12, campaign_id=7, active_character_id=None)
    db = SimpleNamespace(
        get=AsyncMock(return_value=campaign),
        execute=AsyncMock(side_effect=[SimpleNamespace(scalar_one_or_none=lambda: saved), SimpleNamespace(scalar_one_or_none=lambda: campaign)]),
        add=Mock(side_effect=AssertionError("blocked gate wrote a session")),
        commit=AsyncMock(side_effect=AssertionError("blocked gate committed")),
    )
    source = SourceRead(slug="missing-source",title="Fixture",status="missing")
    monkeypatch.setattr(router,"source_reads",AsyncMock(return_value=[source]))
    monkeypatch.setattr(campaign_readiness,"is_private_preparation_source",lambda _: False)
    # All actual admission decisions run; no graph, model or persistence entrypoint is reached.
    with pytest.raises(HTTPException) as rejected:
        if entrypoint == "create":
            await router.create_session(SessionCreate(campaign_id=7), None, db)
        else:
            await router._load_turn_initial_state(12, "Look", db)
    assert rejected.value.detail["code"] == "campaign_sources_unavailable"
    assert rejected.value.status_code == 409
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
