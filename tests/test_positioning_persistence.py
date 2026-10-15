"""EDG-044 (second slice): foe bands and spent reactions persist in the combat row and reload.

The combatants column is JSONB, so `band` and `reaction_used` are additive keys on existing rows.
A row written before positioning has neither key and reads as melee. Runs only where the
Postgres testcontainer is available (CI), following tests/test_story_state.py.
"""

import pytest

from apps.api.services.combat import load_active_combat
from db.postgres.models import Campaign, CombatState, Session, User


@pytest.mark.asyncio
async def test_foe_bands_and_spent_reactions_survive_a_reload(db_session) -> None:
    import db.postgres.session as sm

    user = User(email="positioning@example.test")
    db_session.add(user)
    await db_session.flush()
    campaign = Campaign(title="Positioning", system="5e", created_by=user.id)
    db_session.add(campaign)
    await db_session.flush()
    session = Session(campaign_id=campaign.id)
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        CombatState(
            session_id=session.id,
            initiative_order=["player", "Goblin", "Archer", "Wolf"],
            round=2,
            current_turn_index=0,
            ended=False,
            combatants=[
                {"name": "player", "is_player": True, "alive": True, "hp_current": 9},
                {
                    "name": "Goblin",
                    "is_player": False,
                    "alive": True,
                    "hp_current": 7,
                    "band": "melee",
                    "reaction_used": True,
                },
                {
                    "name": "Archer",
                    "is_player": False,
                    "alive": True,
                    "hp_current": 5,
                    "band": "ranged",
                },
                # A legacy row: written before positioning, so no band and no reaction key.
                {"name": "Wolf", "is_player": False, "alive": True, "hp_current": 11},
            ],
        )
    )
    await db_session.commit()
    session_id = session.id
    db_session.expunge_all()

    async with sm.async_session_factory() as fresh:
        active = await load_active_combat(fresh, session_id)
    rows = {c["name"]: c for c in active["combatants"]}
    assert rows["Goblin"]["band"] == "melee" and rows["Goblin"]["reaction_used"] is True
    assert rows["Archer"]["band"] == "ranged" and "reaction_used" not in rows["Archer"]
    # The legacy row keeps exactly its stored keys; nothing was rewritten to add a band.
    assert "band" not in rows["Wolf"] and "reaction_used" not in rows["Wolf"]
