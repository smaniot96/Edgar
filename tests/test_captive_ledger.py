"""EDG-146 (DB-backed): captive outcomes agree across the ledger, NPC rows, combat record and reload.

Runs against disposable PostgreSQL in hosted CI (Docker is unavailable locally, so these skip
here). Caller-owned transactions: the helpers never commit; the tests commit as a turn would.
"""

import pytest
from sqlalchemy import func, select

from apps.api.services.combat import (
    load_combat_captives,
    load_resolved_combat_outcomes,
    persist_captive_outcome,
    persist_combat,
    persist_combat_npcs,
)
from apps.api.services.story_state import (
    StoryStateConflict,
    load_story_state,
    persist_combat_outcome_facts,
    session_story_flags,
)
from db.postgres.models import NPC, Campaign, EventLog, Session, StoryFact as StoryFactRow, User


async def _setup(db):
    user = User(email="captive@example.test")
    db.add(user)
    await db.flush()
    camp = Campaign(title="Captives", system="5e", created_by=user.id)
    db.add(camp)
    await db.flush()
    sess = Session(campaign_id=camp.id)
    db.add(sess)
    await db.flush()
    return camp, sess


async def _event(db, sess) -> EventLog:
    event = EventLog(session_id=sess.id, event_type="narration", payload={})
    db.add(event)
    await db.flush()
    return event


def _foe(name, disposition=None, hp=0, alive=None):
    foe = {"name": name, "display_name": name, "is_player": False, "hp_current": hp}
    if disposition is not None:
        foe["disposition"] = disposition
    if alive is not None:
        foe["alive"] = alive
    return foe


def _ended(extra=()):
    return {
        "initiative_order": [
            "player",
            "Bandit",
            "Wolf",
            "Rat",
            "Cultist",
            *[f["name"] for f in extra],
        ],
        "round": 3,
        "current_turn_index": 0,
        "ended": True,
        "outcome": "victory",
        "combatants": [
            {
                "name": "player",
                "display_name": "Eda",
                "is_player": True,
                "alive": True,
                "disposition": "active",
                "hp_current": 9,
            },
            _foe("Bandit", "captured", alive=True),
            _foe("Wolf", "dead", alive=False),
            _foe("Rat", "defeated", alive=False),
            # Legacy `alive=false` row: no stored disposition, so no intent is invented.
            _foe("Cultist", alive=False),
            *extra,
        ],
    }


async def _persist_ended(db, camp, sess, ended, turn_id="turn-1"):
    combat_id = await persist_combat(db, sess.id, ended)
    await persist_combat_npcs(db, sess.id, camp.id, combat_id, ended)
    event = await _event(db, sess)
    await persist_combat_outcome_facts(
        db, sess, combat_id, ended, event_id=event.id, turn_id=turn_id
    )
    return combat_id


def _captive(captives, name):
    return next(c for c in captives if c["name"] == name)


@pytest.mark.asyncio
async def test_combat_outcomes_agree_across_ledger_npc_rows_and_reload(db_session):
    camp, sess = await _setup(db_session)
    manual = NPC(
        campaign_id=camp.id, session_id=None, name="Bandit", disposition="hostile", stat_block={}
    )
    db_session.add(manual)
    await db_session.flush()
    ended = _ended()
    combat_id = await _persist_ended(db_session, camp, sess, ended)
    # Retry of the same turn: the ledger and NPC writes are idempotent.
    await persist_combat_npcs(db_session, sess.id, camp.id, combat_id, ended)
    event = await _event(db_session, sess)
    await persist_combat_outcome_facts(
        db_session, sess, combat_id, ended, event_id=event.id, turn_id="turn-1"
    )
    await db_session.commit()
    db_session.expunge_all()

    state = await load_story_state(db_session, sess.id, camp.id)
    facts = {f["display_name"]: f for f in state["facts"]}
    assert len(state["facts"]) == 3  # Bandit, Wolf, Rat; the legacy Cultist has no record
    assert facts["Bandit"]["status"] == "captive"
    assert facts["Wolf"]["status"] == "dead"
    assert facts["Rat"]["status"] == "defeated"
    for fact in facts.values():
        assert fact["kind"] == "encounter" and fact["target"]["collection"] == "combat"
        assert fact["source_refs"][0]["combat_id"] == combat_id
        assert fact["source_refs"][0]["combat_round"] == 3
        assert fact["turn_id"] == "turn-1"
    # Engine outcomes never become campaign world flags.
    assert session_story_flags(state, {}, {}) == {}

    rows = {
        n.name: n
        for n in (await db_session.scalars(select(NPC).where(NPC.session_id == sess.id))).all()
    }
    assert set(rows) == {"Bandit", "Wolf"}
    assert rows["Bandit"].disposition == "captive"
    assert rows["Wolf"].disposition == "dead" and rows["Wolf"].stat_block["life_state"] == "dead"
    untouched = await db_session.get(NPC, manual.id)
    assert untouched.disposition == "hostile" and untouched.session_id is None

    outcome = (await load_resolved_combat_outcomes(db_session, sess.id))[0]
    by_name = {c["name"]: c for c in outcome["combatants"]}
    assert by_name["Bandit"]["disposition"] == "captured"
    assert by_name["Wolf"]["disposition"] == "dead"
    assert by_name["Rat"]["disposition"] == "defeated"
    assert "disposition" not in by_name["Cultist"]

    captives = await load_combat_captives(db_session, sess.id)
    assert {c["name"] for c in captives} == {"Bandit", "Wolf"}  # manual Bandit is excluded
    bandit = _captive(captives, "Bandit")
    assert bandit["disposition"] == "captive"
    assert bandit["combat_id"] == combat_id and bandit["combat_round"] == 3


@pytest.mark.asyncio
async def test_release_then_kill_are_recorded_once_and_never_rewritten(db_session):
    camp, sess = await _setup(db_session)
    manual = NPC(
        campaign_id=camp.id, session_id=None, name="Bandit", disposition="hostile", stat_block={}
    )
    db_session.add(manual)
    await db_session.flush()
    await _persist_ended(db_session, camp, sess, _ended([_foe("Goblin", "captured", alive=True)]))
    captives = await load_combat_captives(db_session, sess.id)

    def outcome(name, action):
        captive = _captive(captives, name)
        return {
            "action": action,
            "npc_disposition": "released" if action == "release" else "dead",
            "combat_id": captive["combat_id"],
            "combatant_id": captive["combatant_id"],
            "combat_round": captive["combat_round"],
            "name": name,
        }

    release = outcome("Bandit", "release")
    event = await _event(db_session, sess)
    await persist_captive_outcome(db_session, sess, release, event_id=event.id, turn_id="turn-2")
    # Exact replay of the same committed outcome is a no-op.
    await persist_captive_outcome(db_session, sess, release, event_id=event.id, turn_id="turn-2")
    row = await db_session.scalar(
        select(NPC).where(NPC.session_id == sess.id, NPC.name == "Bandit")
    )
    assert row.disposition == "released" and row.stat_block["life_state"] == "living"

    kill = outcome("Goblin", "kill")
    await persist_captive_outcome(db_session, sess, kill, event_id=event.id, turn_id="turn-3")
    goblin = await db_session.scalar(
        select(NPC).where(NPC.session_id == sess.id, NPC.name == "Goblin")
    )
    assert goblin.disposition == "dead" and goblin.stat_block["life_state"] == "dead"

    # A released foe cannot be killed afterwards, and no row is rewritten.
    with pytest.raises(ValueError, match="cannot be rewritten"):
        await persist_captive_outcome(
            db_session,
            sess,
            {**release, "action": "kill", "npc_disposition": "dead"},
            event_id=event.id,
            turn_id="turn-4",
        )
    await db_session.commit()
    db_session.expunge_all()

    state = await load_story_state(db_session, sess.id, camp.id)
    by_status = {(f["display_name"], f["status"]): f for f in state["facts"]}
    assert ("Bandit", "released") in by_status and ("Goblin", "dead") in by_status
    assert sum(1 for f in state["facts"] if f["status"] == "released") == 1
    released = by_status[("Bandit", "released")]
    assert released["source_refs"][0]["combat_id"] == release["combat_id"]
    assert released["turn_id"] == "turn-2"
    # Bandit captive+released, Goblin captive+dead, Wolf dead, Rat defeated.
    assert len(state["facts"]) == 6

    still_manual = await db_session.get(NPC, manual.id)
    assert still_manual.disposition == "hostile" and still_manual.session_id is None
    # The reviewed record of the fight itself is immutable history: it still reads captured.
    outcomes = await load_resolved_combat_outcomes(db_session, sess.id)
    assert {c["name"]: c["disposition"] for c in outcomes[0]["combatants"] if "disposition" in c}[
        "Bandit"
    ] == "captured"


@pytest.mark.asyncio
async def test_failed_captive_outcome_leaves_no_partial_row_or_fact(db_session):
    camp, sess = await _setup(db_session)
    await _persist_ended(db_session, camp, sess, _ended())
    captive = _captive(await load_combat_captives(db_session, sess.id), "Bandit")
    release = {
        "action": "release",
        "npc_disposition": "released",
        "combat_id": captive["combat_id"],
        "combatant_id": captive["combatant_id"],
        "combat_round": captive["combat_round"],
        "name": "Bandit",
    }
    event = await _event(db_session, sess)
    await persist_captive_outcome(db_session, sess, release, event_id=event.id, turn_id="turn-2")
    row = await db_session.scalar(
        select(NPC).where(NPC.session_id == sess.id, NPC.name == "Bandit")
    )
    # Drift: the row is captive again while the ledger already holds a terminal release.
    row.disposition = "captive"
    await db_session.flush()
    facts_before = await db_session.scalar(
        select(func.count()).select_from(StoryFactRow).where(StoryFactRow.session_id == sess.id)
    )

    kill = {**release, "action": "kill", "npc_disposition": "dead"}
    with pytest.raises(StoryStateConflict):
        async with db_session.begin_nested():
            await persist_captive_outcome(
                db_session, sess, kill, event_id=event.id, turn_id="turn-3"
            )
    await db_session.refresh(row)
    assert row.disposition == "captive"  # the savepoint took the NPC write back
    facts_after = await db_session.scalar(
        select(func.count()).select_from(StoryFactRow).where(StoryFactRow.session_id == sess.id)
    )
    assert facts_after == facts_before
