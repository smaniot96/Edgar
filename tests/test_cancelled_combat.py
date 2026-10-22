"""An unstarted encounter proposal closes durably without becoming an earned outcome."""

from apps.api.services.combat import (
    load_active_combat,
    load_resolved_combat_outcomes,
    persist_combat,
    resolved_combat_outcomes_public,
)
from tests.test_story_state import setup


async def test_cancelled_proposal_survives_reload_without_active_combat_or_outcome(db_session):
    _campaign, sessions, _ = await setup(db_session)
    sid = sessions[0].id
    staged = {
        "initiative_order": ["player", "Guard"], "round": 1,
        "current_turn_index": 0, "ended": False, "outcome": None,
        "combatants": [
            {"name": "player", "is_player": True, "initiative_pending": True, "hp_current": 36},
            {"name": "Guard", "is_player": False, "hp_current": 55},
        ],
    }
    staged["id"] = await persist_combat(db_session, sid, staged)
    await db_session.commit()
    loaded = await load_active_combat(db_session, sid)
    assert loaded["id"] == staged["id"]
    loaded.update(ended=True, outcome="cancelled")
    loaded["combatants"][0].pop("initiative_pending")
    await persist_combat(db_session, sid, loaded)
    await db_session.commit()
    db_session.expunge_all()
    assert await load_active_combat(db_session, sid) is None
    assert await load_resolved_combat_outcomes(db_session, sid) == []
    assert resolved_combat_outcomes_public([loaded]) == []
