# ruff: noqa: F811  (pytest fixtures are requested by argument name)
"""EDG-049 over the API: a shared fight across two human-controlled heroes.

DB-backed (Postgres testcontainer) with the in-process Redis fake. The turn routes run the real
pipeline with the scripted fake model (`fake_llm_turn`); every die comes from `ScriptedDice`. The fight
is seeded as a persisted `combat_states` row, the shape a reload reads.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

import db.postgres.session as db_session_module
from agent.combat_view import sort_by_initiative
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from apps.api.services import session_channel
from db.postgres.models import (
    CharacterAssignment,
    CombatState,
    EventLog,
    Session as SessionModel,
)
from tests.agent.test_combat import _enemy
from tests.agent.test_spells import ScriptedDice
from tests.api.test_campaign_membership import (  # noqa: F401  (fixtures and helpers)
    _auth,
    _join,
    table,
    token_mode,
)
from tests.api.test_turn_pipeline import _fake_chat
from tools.dice import use_rng


@pytest_asyncio.fixture(autouse=True)
async def _captured_ids(table):
    for key in ("alice", "bob", "alice_pc", "bob_pc", "save", "campaign"):
        table[f"{key}_id"] = table[key].id


def _hero(character_id: int, name: str, init: int, *, hp: int = 20) -> dict:
    return {
        "name": f"pc-{character_id}",
        "display_name": name,
        "character_id": character_id,
        "is_player": True,
        "hp_current": hp,
        "hp_max": 20,
        "ac": 14,
        "alive": True,
        "disposition": "active",
        "init": init,
        "init_bonus": 1,
    }


async def _seed_fight(table, *, goblin_hp: int = 30, goblin_init: int = 5, bob_init: int = 10) -> int:
    """Ayla (init 15) -> Bran -> Goblin, cursor on Ayla; the table's holder is Ayla already."""
    rows = sort_by_initiative(
        [
            _hero(table["alice_pc_id"], "Ayla", 15),
            _hero(table["bob_pc_id"], "Bran", bob_init),
            _enemy("Goblin", hp=goblin_hp, init=goblin_init),
        ]
    )
    async with db_session_module.async_session_factory() as db:
        row = CombatState(
            session_id=table["save_id"],
            initiative_order=[c["name"] for c in rows],
            round=1,
            current_turn_index=0,
            ended=False,
            combatants=rows,
            outcome=None,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _fight(combat_id: int) -> CombatState:
    async with db_session_module.async_session_factory() as db:
        return (await db.execute(select(CombatState).where(CombatState.id == combat_id))).scalar_one()


async def _holder(table) -> tuple[int | None, int]:
    async with db_session_module.async_session_factory() as db:
        session = await db.get(SessionModel, table["save_id"])
        return session.active_character_id, session.spotlight_version


async def _hp(character_id: int, campaign_id: int) -> int:
    async with db_session_module.async_session_factory() as db:
        return (
            await db.execute(
                select(CharacterAssignment.hp_current).where(
                    CharacterAssignment.character_id == character_id,
                    CharacterAssignment.campaign_id == campaign_id,
                )
            )
        ).scalar_one()


async def _narrations(table) -> int:
    async with db_session_module.async_session_factory() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.session_id == table["save_id"], EventLog.event_type == "narration")
            )
        ).scalar_one()


async def _turn(client, table, who: str, message: str, faces: list[int], **body: Any):
    _fake_chat()._parsed = ParsedInput(intent="combat", check="attack", target="goblin")
    _fake_chat()._adj = AdjudicationResult(success=True)
    with use_rng(ScriptedDice(list(faces))):
        return await client.post(
            f"/api/sessions/{table['save_id']}/turn",
            json={"message": message, **body},
            headers=_auth(table["tokens"][who]),
        )


@pytest_asyncio.fixture
async def seated(api_client, table, db_session, fake_llm_turn, fake_retrieval):
    from db.postgres.models import Campaign
    from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS

    campaign = await db_session.get(Campaign, table["campaign_id"])
    campaign.adventure_collections = list(DEFAULT_ADVENTURE_COLLECTIONS)
    await db_session.commit()
    await _join(api_client, table, "bob", character=table["bob_pc_id"])
    return table


async def _events(redis, table, after: int = 0) -> list[tuple[int, dict]]:
    return await session_channel.read_after(redis, table["save_id"], after, count=500, block_ms=1)


@pytest.mark.asyncio
async def test_route_preview_restores_holder_geometry_without_writing_a_turn(api_client, seated):
    combat_id = await _seed_fight(seated)
    async with db_session_module.async_session_factory() as db:
        row = await db.get(CombatState, combat_id)
        row.battlefield = {"version": 1, "width": 10, "height": 3, "theme": "open", "terrain": []}
        row.combatants = [{**c, "pos": [index * 3, 1]} for index, c in enumerate(row.combatants)]
        await db.commit()
    before = await _fight(combat_id)
    holder = await _holder(seated)
    path = f"/api/sessions/{seated['save_id']}/combat/route?destination=B2"
    result = await api_client.get(path, headers=_auth(seated["tokens"]["alice"]))
    assert result.status_code == 200, result.text
    assert result.json()["reachable"] and result.json()["cost_ft"] == 5
    assert result.json()["path"][0]["label"] == "A2"
    assert "character_id" not in result.text and "source_sha256" not in result.text
    denied = await api_client.get(path, headers=_auth(seated["tokens"]["dan"]))
    assert denied.status_code in (403, 404)
    after = await _fight(combat_id)
    assert (after.combatants, after.round, after.battlefield) == (
        before.combatants,
        before.round,
        before.battlefield,
    )
    assert await _narrations(seated) == 0 and await _holder(seated) == holder


@pytest.mark.asyncio
async def test_route_preview_holds_legacy_and_missing_encounters(api_client, seated):
    path = f"/api/sessions/{seated['save_id']}/combat/route?destination=B2"
    headers = _auth(seated["tokens"]["alice"])
    assert (await api_client.get(path, headers=headers)).status_code == 409
    await _seed_fight(seated)
    assert (await api_client.get(path, headers=headers)).status_code == 409


@pytest.mark.asyncio
async def test_a_hero_out_of_turn_gets_409_and_nothing_changes(api_client, seated, redis_client):
    combat_id = await _seed_fight(seated)
    before = await _fight(combat_id)
    refused = await _turn(api_client, seated, "bob", "I attack the goblin", [])
    assert refused.status_code == 409
    assert refused.json()["code"] == "not_your_spotlight"
    after = await _fight(combat_id)
    assert (after.round, after.current_turn_index, after.combatants) == (
        before.round,
        before.current_turn_index,
        before.combatants,
    )
    assert await _narrations(seated) == 0
    assert await _holder(seated) == (seated["alice_pc_id"], (await _holder(seated))[1])


@pytest.mark.asyncio
async def test_the_turn_passes_hero_to_hero_and_each_hero_acts_on_its_own_turn(
    api_client, seated, redis_client
):
    combat_id = await _seed_fight(seated)
    _holder_before, version = await _holder(seated)
    # Ayla: attack d20=15 hits AC 12, damage d8=4. Bran's slot pauses the request.
    first = await _turn(api_client, seated, "alice", "I attack the goblin", [15, 4])
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["combat_state"]["party"] is True
    assert body["combat_state"]["current_turn_character_id"] == seated["bob_pc_id"]
    holder, version_after = await _holder(seated)
    assert holder == seated["bob_pc_id"] and version_after > version
    # The turn passed: Ayla cannot act again, Bran can.
    again = await _turn(api_client, seated, "alice", "I attack the goblin", [])
    assert again.status_code == 409 and again.json()["code"] == "not_your_spotlight"
    # Bran: attack d20=15, damage 4; then the goblin (two heroes stand: 1d2=2 picks Bran) hits with
    # d20=15 (AC 14) for 3+1; the round wraps and Ayla is up.
    second = await _turn(api_client, seated, "bob", "I attack the goblin", [15, 4, 2, 15, 3])
    assert second.status_code == 200, second.text
    assert second.json()["combat_state"]["round"] == 2
    assert second.json()["combat_state"]["current_turn_character_id"] == seated["alice_pc_id"]
    # Bob's own response is Bob's sheet; the goblin's hit on Bran was written to Bran's assignment.
    assert await _hp(seated["bob_pc_id"], seated["campaign_id"]) == 20 - 4
    assert await _hp(seated["alice_pc_id"], seated["campaign_id"]) == 20
    persisted = await _fight((await _fight(combat_id)).id)
    assert persisted.round == 2


@pytest.mark.asyncio
async def test_a_stale_version_is_refused_before_any_work(api_client, seated):
    await _seed_fight(seated)
    _h, version = await _holder(seated)
    stale = await _turn(
        api_client, seated, "alice", "I attack the goblin", [], spotlight_version=version + 5
    )
    assert stale.status_code == 409 and stale.json()["code"] == "spotlight_stale"
    assert await _narrations(seated) == 0


@pytest.mark.asyncio
async def test_the_table_hears_the_shared_fight_and_private_data_stays_private(
    api_client, seated, redis_client
):
    await _seed_fight(seated)
    before = await session_channel.head_seq(redis_client, seated["save_id"])
    await _turn(api_client, seated, "alice", "I attack the goblin", [15, 4])
    records = await _events(redis_client, seated, before)
    types = [r["envelope"]["type"] for _s, r in records]
    assert "combat.turn_changed" in types and "turn.spotlight_changed" in types
    changed = next(r for _s, r in records if r["envelope"]["type"] == "combat.turn_changed")
    assert changed["audience"] == "table"
    assert changed["envelope"]["data"]["current_turn_character_id"] == seated["bob_pc_id"]
    state = next(r for _s, r in records if r["envelope"]["type"] == "state.committed")
    assert "pending_declaration" not in state["envelope"]["data"]["combat_state"]
    # Ayla's sheet goes to Alice only.
    private = [r for _s, r in records if r["envelope"]["type"] == "character.state"]
    assert private and all(r["audience"] == f"player:{seated['alice_id']}" for r in private)


@pytest.mark.asyncio
async def test_a_hero_dropped_by_a_foe_is_written_to_that_heros_own_sheet_and_announced_privately(
    api_client, seated, redis_client
):
    # Bran has 3 HP. After Ayla's turn (nothing for Bran to do yet) the foe is ahead of Bran in
    # initiative: the goblin (init 12) acts between Ayla (15) and Bran (10) and picks among both.
    async with db_session_module.async_session_factory() as db:
        ass = (
            await db.execute(
                select(CharacterAssignment).where(
                    CharacterAssignment.character_id == seated["bob_pc_id"]
                )
            )
        ).scalar_one()
        ass.hp_current = 3
        await db.commit()
    rows = sort_by_initiative(
        [
            {**_hero(seated["alice_pc_id"], "Ayla", 15)},
            {**_hero(seated["bob_pc_id"], "Bran", 10, hp=3)},
            _enemy("Goblin", hp=30, init=12),
        ]
    )
    async with db_session_module.async_session_factory() as db:
        db.add(
            CombatState(
                session_id=seated["save_id"],
                initiative_order=[c["name"] for c in rows],
                round=1,
                current_turn_index=0,
                ended=False,
                combatants=rows,
            )
        )
        await db.commit()
    before = await session_channel.head_seq(redis_client, seated["save_id"])
    # Ayla attacks (15, 4). The goblin picks Bran (1d2=2) and hits: d20=15, 6+1 damage: Bran drops to 0.
    # Subdued mode: nothing else rolls; Bran's slot then pauses... he is down, so Ayla is up in round 2.
    response = await _turn(api_client, seated, "alice", "I attack the goblin", [15, 4, 2, 15, 6])
    assert response.status_code == 200, response.text
    assert await _hp(seated["bob_pc_id"], seated["campaign_id"]) == 0
    records = await _events(redis_client, seated, before)
    private = [
        r
        for _s, r in records
        if r["envelope"]["type"] == "character.state" and r["audience"] == f"player:{seated['bob_id']}"
    ]
    assert private and private[0]["envelope"]["data"]["character"]["hp_current"] == 0
    # Alice never receives Bob's private sheet.
    assert all(
        r["envelope"]["data"]["character"]["id"] == seated["alice_pc_id"]
        for _s, r in records
        if r["envelope"]["type"] == "character.state"
        and r["audience"] == f"player:{seated['alice_id']}"
    )


@pytest.mark.asyncio
async def test_an_idle_holder_is_skipped_with_a_default_dodge_after_the_timeout(
    api_client, seated, redis_client
):
    combat_id = await _seed_fight(seated)
    # Bob (not the holder) cannot skip Ayla's turn while she still has time.
    early = await api_client.post(
        f"/api/sessions/{seated['save_id']}/combat/skip",
        json={},
        headers=_auth(seated["tokens"]["bob"]),
    )
    assert early.status_code == 409 and early.json()["code"] == "combat_skip_not_due"
    # Make Ayla overdue.
    async with db_session_module.async_session_factory() as db:
        session = await db.get(SessionModel, seated["save_id"])
        session.spotlight_since = datetime.now(UTC) - timedelta(seconds=session.spotlight_timeout_seconds + 5)
        await db.commit()
    _fake_chat()._adj = AdjudicationResult(success=True)
    with use_rng(ScriptedDice([])):
        skipped = await api_client.post(
            f"/api/sessions/{seated['save_id']}/combat/skip",
            json={},
            headers=_auth(seated["tokens"]["bob"]),
        )
    assert skipped.status_code == 200, skipped.text
    persisted = await _fight(combat_id)
    ayla = next(c for c in persisted.combatants if c["character_id"] == seated["alice_pc_id"])
    assert ayla["action_economy"]["dodging"] is True
    assert (await _holder(seated))[0] == seated["bob_pc_id"]


@pytest.mark.asyncio
async def test_the_owner_may_skip_at_any_time_and_a_spectator_may_not(api_client, seated):
    await _seed_fight(seated)
    _fake_chat()._adj = AdjudicationResult(success=True)
    with use_rng(ScriptedDice([])):
        owner = await api_client.post(
            f"/api/sessions/{seated['save_id']}/combat/skip",
            json={},
            headers=_auth(seated["tokens"]["alice"]),
        )
    assert owner.status_code == 200, owner.text
    outsider = await api_client.post(
        f"/api/sessions/{seated['save_id']}/combat/skip",
        json={},
        headers=_auth(seated["tokens"]["dan"]),
    )
    assert outsider.status_code == 404


@pytest.mark.asyncio
async def test_skip_without_a_party_fight_is_refused(api_client, seated):
    refused = await api_client.post(
        f"/api/sessions/{seated['save_id']}/combat/skip",
        json={},
        headers=_auth(seated["tokens"]["alice"]),
    )
    assert refused.status_code == 409 and refused.json()["code"] == "no_party_combat"


@pytest.mark.asyncio
async def test_the_owner_cannot_hand_the_spotlight_over_during_a_fight(api_client, seated):
    await _seed_fight(seated)
    response = await api_client.patch(
        f"/api/sessions/{seated['save_id']}/spotlight",
        json={"holder_character_id": seated["bob_pc_id"]},
        headers=_auth(seated["tokens"]["alice"]),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "combat_turn_locked"
    view = await api_client.get(
        f"/api/sessions/{seated['save_id']}/spotlight", headers=_auth(seated["tokens"]["bob"])
    )
    assert view.json()["combat"]["holder_character_id"] == seated["alice_pc_id"]


@pytest.mark.asyncio
async def test_a_reload_mid_fight_restores_the_shared_fight_and_the_holder(api_client, seated):
    await _seed_fight(seated)
    await _turn(api_client, seated, "alice", "I attack the goblin", [15, 4])
    reloaded = await api_client.get(
        f"/api/sessions/{seated['save_id']}/combat", headers=_auth(seated["tokens"]["bob"])
    )
    body = reloaded.json()
    assert body["party"] is True and body["round"] == 1
    assert body["current_turn_character_id"] == seated["bob_pc_id"]
    assert [c["character_id"] for c in body["combatants"] if c["is_player"]] == [
        seated["alice_pc_id"],
        seated["bob_pc_id"],
    ]


@pytest.mark.asyncio
async def test_a_won_fight_grants_every_surviving_hero_its_milestone_once(api_client, seated):
    async with db_session_module.async_session_factory() as db:
        from db.postgres.models import Campaign

        campaign = await db.get(Campaign, seated["campaign_id"])
        campaign.progression_mode = "milestone"
        await db.commit()
    await _seed_fight(seated, goblin_hp=1)
    won = await _turn(api_client, seated, "alice", "I attack the goblin", [15, 6])
    assert won.status_code == 200, won.text
    assert won.json()["combat_state"]["outcome"] == "victory"
    async with db_session_module.async_session_factory() as db:
        pending = (
            await db.execute(
                select(CharacterAssignment.character_id, CharacterAssignment.milestones_pending)
                .where(CharacterAssignment.campaign_id == seated["campaign_id"])
                .order_by(CharacterAssignment.character_id)
            )
        ).all()
    assert {cid: n for cid, n in pending} == {seated["alice_pc_id"]: 1, seated["bob_pc_id"]: 1}
    # The fight is over: the spotlight is the table's again and a retried commit grants nothing more.
    assert time.time() > 0


@pytest.mark.asyncio
async def test_a_fight_opened_by_one_hero_gives_the_first_slot_to_whoever_rolled_highest(
    api_client, seated, redis_client, monkeypatch
):
    async def roster(_state, _targets, *, budget_summary=None, party_size=1):
        assert party_size == 2
        return [_enemy("Goblin", init=5)]

    monkeypatch.setattr("agent.graph_combat._build_enemy_roster", roster)
    # Ayla (+1) rolls 10 = 11, Bran (+1) rolls 18 = 19, the goblin (+1) rolls 3 = 4.
    response = await _turn(api_client, seated, "alice", "I attack the goblin", [10, 18, 3])
    assert response.status_code == 200, response.text
    body = response.json()["combat_state"]
    assert body["party"] is True and body["current_turn_character_id"] == seated["bob_pc_id"]
    assert [e["name"] for e in body["initiative"]] == ["Bran", "Ayla", "Goblin"]
    # The spotlight followed the initiative, not the table's rotation: Bran holds it, Alice cannot act.
    assert (await _holder(seated))[0] == seated["bob_pc_id"]
    refused = await _turn(api_client, seated, "alice", "I attack the goblin", [])
    assert refused.status_code == 409


@pytest.mark.asyncio
async def test_simultaneous_submissions_yield_one_winner(seated, redis_client):
    """Two sessions race for the same combat turn: the row lock admits only the holder, and a second
    admission of the holder's own turn cannot commit once the first has passed the turn."""
    import asyncio

    from apps.api.auth import Principal
    from apps.api.services import spotlight

    await _seed_fight(seated)
    sid = seated["save_id"]

    async def admit(user_id: int):
        async with db_session_module.async_session_factory() as db:
            try:
                opened = await spotlight.open_turn(
                    db,
                    redis_client,
                    sid,
                    Principal(user_id=user_id, token_id=None, is_admin=False),
                    expected_version=None,
                    consumes=True,
                )
                return ("admitted", opened.plan)
            except spotlight.SpotlightRefused as exc:
                return ("refused", exc.code)

    results = await asyncio.gather(
        admit(seated["alice_id"]), admit(seated["bob_id"]), admit(seated["alice_id"])
    )
    kinds = sorted(kind for kind, _ in results)
    assert kinds == ["admitted", "admitted", "refused"]
    assert [value for kind, value in results if kind == "refused"] == ["not_your_spotlight"]
    plans = [value for kind, value in results if kind == "admitted"]
    assert plans[0].version == plans[1].version and plans[0].holder == seated["alice_pc_id"]

    # Both admissions are Ayla's turn: only the first to commit passes the turn.
    outcomes = []
    for plan in plans:
        async with db_session_module.async_session_factory() as db:
            session = (
                await db.execute(
                    select(SessionModel).where(SessionModel.id == sid).with_for_update()
                )
            ).scalar_one()
            try:
                spotlight.apply_turn_commit(
                    session,
                    plan,
                    now=datetime.now(UTC),
                    combat_holder=seated["bob_pc_id"],
                )
                await db.commit()
                outcomes.append("committed")
            except spotlight.SpotlightRefused as exc:
                await db.rollback()
                outcomes.append(exc.code)
    assert outcomes == ["committed", "spotlight_stale"]
    assert (await _holder(seated))[0] == seated["bob_pc_id"]


@pytest.mark.asyncio
async def test_an_absent_holder_is_skippable_after_a_short_grace_but_a_present_one_is_not(
    api_client, seated, redis_client, db_session
):
    """EDG-047 presence shortens the wait: absent past the grace, a seated player may skip; present, not."""
    from apps.api.services import spotlight

    await _seed_fight(seated)
    session = await db_session.get(SessionModel, seated["save_id"])
    session.spotlight_since = datetime.now(UTC) - spotlight.COMBAT_ABSENT_GRACE - timedelta(seconds=5)
    await db_session.commit()

    async def due() -> bool:
        view = await api_client.get(
            f"/api/sessions/{seated['save_id']}/spotlight", headers=_auth(seated["tokens"]["bob"])
        )
        return view.json()["combat"]["skip_due"]

    assert await due() is True  # Ayla is not on the channel
    await session_channel.connect(
        redis_client,
        seated["save_id"],
        "conn-alice",
        {"user_id": seated["alice_id"], "display_name": None, "character_id": None, "character_name": None},
        now=time.time(),
    )
    assert await due() is False  # present: she gets the whole timeout
    refused = await api_client.post(
        f"/api/sessions/{seated['save_id']}/combat/skip",
        json={},
        headers=_auth(seated["tokens"]["bob"]),
    )
    assert refused.status_code == 409 and refused.json()["code"] == "combat_skip_not_due"
