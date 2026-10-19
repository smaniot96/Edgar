# ruff: noqa: F811  (pytest fixtures are requested by argument name)
"""Server-side spotlight arbitration for multi-PC tables (EDG-048).

DB-backed (Postgres testcontainer) with the in-process Redis fake. The turn routes are exercised up to
and past the spotlight gate, but the model is never called: ``_load_turn_initial_state`` and
``run_turn`` are replaced, so each test can count exactly which requests reached the model.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import db.postgres.session as db_session_module
from apps.api.auth import Principal
from apps.api.dependencies import acquire_turn_lock
from apps.api.routers import session as session_router
from apps.api.schemas.channel import AUDIENCE_TABLE, EVENT_TYPES
from apps.api.services import session_channel, spotlight
from db.postgres.models import Session as SessionModel, SessionParticipant
from tests.api.test_campaign_membership import (  # noqa: F401  (fixtures and helpers)
    _accept,
    _auth,
    _character,
    _invite,
    _join,
    _token,
    _user,
    table,
    token_mode,
)


def _principal(user_id: int) -> Principal:
    return Principal(user_id=user_id, token_id=None, is_admin=False)


@pytest_asyncio.fixture(autouse=True)
async def _captured_ids(table):
    """Plain ids for the fixture's rows, read before any commit can expire them."""
    for key in ("alice", "bob", "carol", "dan", "alice_pc", "bob_pc", "save", "campaign"):
        table[f"{key}_id"] = table[key].id


@pytest.fixture
def model_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the state load and the model call. Records each turn that reached the model."""
    calls: list[str] = []

    async def load_state(session_id: int, message: str, db: AsyncSession) -> dict[str, Any]:
        session = await db.get(SessionModel, session_id)
        return {"campaign_id": session.campaign_id, "turn_id": uuid.uuid4().hex}

    async def run_turn(state: dict[str, Any], *, diagnostics: Any = None) -> dict[str, Any]:
        calls.append(state.get("turn_id", ""))
        raise RuntimeError("model is stubbed in this test")

    monkeypatch.setattr(session_router, "_load_turn_initial_state", load_state)
    monkeypatch.setattr(session_router, "run_turn", run_turn)
    return calls


async def _turn(client: AsyncClient, table, who: str, *, version: int | None = None):
    body: dict[str, Any] = {"message": "I look around the clearing."}
    if version is not None:
        body["spotlight_version"] = version
    return await client.post(
        f"/api/sessions/{table['save_id']}/turn", json=body, headers=_auth(table["tokens"][who])
    )


async def _spotlight(client: AsyncClient, table, who: str = "alice") -> dict:
    response = await client.get(
        f"/api/sessions/{table['save_id']}/spotlight", headers=_auth(table["tokens"][who])
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _patch_spotlight(client: AsyncClient, table, body: dict):
    return await client.patch(
        f"/api/sessions/{table['save_id']}/spotlight",
        json=body,
        headers=_auth(table["tokens"]["alice"]),
    )


async def _seat_bob_and_eve(client: AsyncClient, table, db: AsyncSession) -> None:
    """Bob seats Bran and Eve seats her own hero. Alice's Ayla holds the spotlight first."""
    eve = await _user(db, "eve@example.test")
    eve_pc = await _character(db, eve, "Eve's Ilse", table["campaign"])
    table["eve"] = eve
    table["eve_pc"] = eve_pc
    table["eve_id"], table["eve_pc_id"] = eve.id, eve_pc.id
    table["tokens"]["eve"] = await _token(db, eve)
    await _join(client, table, "bob", character=table["bob_pc_id"])
    invite = await _invite(client, table)
    accepted = await _accept(client, "eve", table, invite["token"], eve_pc.id)
    assert accepted.status_code == 200, accepted.text


async def _present(redis: FakeAsyncRedis, session_id: int, user_id: int, conn: str) -> None:
    """Show a member on the channel, as an open subscription would (EDG-047 presence)."""
    await session_channel.connect(
        redis,
        session_id,
        conn,
        {"user_id": user_id, "display_name": None, "character_id": None, "character_name": None},
        now=time.time(),
    )


async def _state(db: AsyncSession, session_id: int) -> SessionModel:
    session = await db.get(SessionModel, session_id)
    await db.refresh(session)
    return session


async def _open(db, redis, session_id, user_id: int, *, consumes=True, now=None, version=None):
    return await spotlight.open_turn(
        db,
        redis,
        session_id,
        _principal(user_id),
        expected_version=version,
        consumes=consumes,
        now=now,
    )


async def _commit(db, session_id, plan, now) -> str | None:
    session = await _state(db, session_id)
    reason = spotlight.apply_turn_commit(session, plan, now=now)
    await db.commit()
    return reason


# --- Gate: who may act, and what a refusal costs ------------------------------------------------


@pytest.mark.asyncio
async def test_non_holder_gets_a_safe_409_and_the_model_is_never_called(
    api_client, table, db_session, model_calls
):
    await _seat_bob_and_eve(api_client, table, db_session)
    response = await _turn(api_client, table, "bob")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "not_your_spotlight"
    # The refusal names no holder, email, token or internal detail.
    text = json.dumps(body)
    assert "@" not in text and "edgar_" not in text and "Ayla" not in text
    assert model_calls == []


@pytest.mark.asyncio
async def test_holder_reaches_the_model_and_non_holder_does_not(
    api_client, table, db_session, model_calls
):
    await _seat_bob_and_eve(api_client, table, db_session)
    assert (await _turn(api_client, table, "bob")).status_code == 409
    accepted = await _turn(api_client, table, "alice")
    assert accepted.status_code != 409  # the gate passed; the stubbed model then fails
    assert len(model_calls) == 1


@pytest.mark.asyncio
async def test_stale_spotlight_version_is_refused_before_any_work(
    api_client, table, db_session, model_calls
):
    await _seat_bob_and_eve(api_client, table, db_session)
    changed = await _patch_spotlight(api_client, table, {"mode": "owner_directed"})
    assert changed.status_code == 200
    version = changed.json()["version"]

    stale = await _turn(api_client, table, "alice", version=version - 1)
    assert stale.status_code == 409
    assert stale.json()["code"] == "spotlight_stale"
    assert model_calls == []

    current = await _turn(api_client, table, "alice", version=version)
    assert current.status_code != 409
    assert len(model_calls) == 1


@pytest.mark.asyncio
async def test_gate_race_across_two_database_connections_admits_exactly_one(
    api_client, table, db_session, redis_client
):
    """The cross-connection invariant without the Redis lock: the row lock alone decides the winner."""
    await _seat_bob_and_eve(api_client, table, db_session)
    released = await _patch_spotlight(
        api_client, table, {"mode": "free_for_all", "holder_character_id": None}
    )
    assert released.status_code == 200
    sid = table["save_id"]

    async def claim(user_id: int):
        async with db_session_module.async_session_factory() as db:
            try:
                opened = await _open(db, redis_client, sid, user_id)
                return ("won", opened.plan.holder)
            except spotlight.SpotlightRefused as exc:
                return ("refused", exc.code)

    results = await asyncio.gather(claim(table["bob_id"]), claim(table["eve_id"]))
    winners = [r for r in results if r[0] == "won"]
    refused = [r for r in results if r[0] == "refused"]
    assert len(winners) == 1 and len(refused) == 1, results
    assert refused[0][1] in {"not_your_spotlight", "spotlight_unavailable"}


@pytest.mark.asyncio
async def test_spectators_and_outsiders_cannot_take_turns_or_read_as_members(
    api_client, table, db_session, model_calls
):
    invite = await _invite(api_client, table, role="spectator")
    assert (await _accept(api_client, "carol", table, invite["token"])).status_code == 200
    assert (await _turn(api_client, table, "carol")).status_code == 403
    assert (await _turn(api_client, table, "dan")).status_code == 404
    assert (
        await api_client.get(
            f"/api/sessions/{table['save_id']}/spotlight", headers=_auth(table["tokens"]["dan"])
        )
    ).status_code == 404
    # A spectator reads the table's spotlight, but cannot change it.
    assert (await _spotlight(api_client, table, "carol"))["engaged"] is False
    assert (
        await api_client.patch(
            f"/api/sessions/{table['save_id']}/spotlight",
            json={"mode": "free_for_all"},
            headers=_auth(table["tokens"]["carol"]),
        )
    ).status_code == 404
    assert model_calls == []


# --- Rotation, passing and skipping -------------------------------------------------------------


@pytest.mark.asyncio
async def test_round_robin_passes_on_commit_and_skips_absent_members(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    # Alice (owner) and Bob are present; Eve has no live connection.
    await _present(redis_client, sid, table["alice_id"], "alice-1")
    await _present(redis_client, sid, table["bob_id"], "bob-1")
    now = datetime.now(UTC)

    first = await _open(db_session, redis_client, sid, table["alice_id"], now=now)
    assert first.plan.holder == table["alice_pc_id"]
    assert await _commit(db_session, sid, first.plan, now) == "turn_passed"
    after_first = await _state(db_session, sid)
    assert after_first.active_character_id == table["bob_pc_id"]
    base = after_first.spotlight_version - 1

    second = await _open(db_session, redis_client, sid, table["bob_id"], now=now)
    assert await _commit(db_session, sid, second.plan, now) == "turn_passed"
    after_second = await _state(db_session, sid)
    # Eve is absent, so the spotlight skips her and returns to Alice.
    assert after_second.active_character_id == table["alice_pc_id"]
    assert after_second.spotlight_version == base + 2


@pytest.mark.asyncio
async def test_round_robin_keeps_the_spotlight_when_nobody_else_is_present(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    now = datetime.now(UTC)
    before_version = (await _state(db_session, sid)).spotlight_version
    plan = await _open(db_session, redis_client, sid, table["alice_id"], now=now)
    assert await _commit(db_session, sid, plan.plan, now) is None
    session = await _state(db_session, sid)
    assert session.active_character_id == table["alice_pc_id"]
    assert session.spotlight_version == before_version


@pytest.mark.asyncio
async def test_a_removed_member_leaves_the_rotation_and_is_never_handed_the_spotlight(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    for member in ("alice", "bob", "eve"):
        await _present(redis_client, sid, table[f'{member}_id'], f"{member}-1")
    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign_id']}/members/{table['eve_id']}",
        headers=_auth(table["tokens"]["alice"]),
    )
    assert removed.status_code == 204
    view = await _spotlight(api_client, table)
    assert [m["character_id"] for m in view["next_up"]] == [table["bob_pc_id"]]


# --- Timeout, owner control, modes -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_idle_holder_loses_the_spotlight_to_the_next_present_member(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    start = datetime.now(UTC)
    session = await _state(db_session, sid)
    session.spotlight_timeout_seconds = 60
    session.spotlight_since = start
    await db_session.commit()
    before_timeout_version = (await _state(db_session, sid)).spotlight_version
    await _present(redis_client, sid, table["bob_id"], "bob-1")

    # Within the timeout the idle holder keeps it, so Bob is refused.
    with pytest.raises(spotlight.SpotlightRefused) as early:
        await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=30))
    assert early.value.code == "not_your_spotlight"

    # Past the timeout Alice is still idle, and Bob is the next present member.
    opened = await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=120))
    assert opened.change is not None and opened.change.reason == "timeout"
    assert opened.plan.holder == table["bob_pc_id"]
    after = await _state(db_session, sid)
    assert after.active_character_id == table["bob_pc_id"]
    assert after.spotlight_version == before_timeout_version + 1


@pytest.mark.asyncio
async def test_a_holder_who_acts_while_timed_out_keeps_the_spotlight(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    start = datetime.now(UTC)
    session = await _state(db_session, sid)
    session.spotlight_timeout_seconds = 60
    session.spotlight_since = start
    await db_session.commit()
    await _present(redis_client, sid, table["bob_id"], "bob-1")
    # Alice acts after the timeout: her own action is activity, so nothing is handed over.
    opened = await _open(db_session, redis_client, sid, table["alice_id"], now=start + timedelta(seconds=120))
    assert opened.change is None
    assert opened.plan.holder == table["alice_pc_id"]


@pytest.mark.asyncio
async def test_owner_directed_mode_never_times_out_and_only_the_owner_hands_over(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    assert (await _patch_spotlight(api_client, table, {"mode": "owner_directed"})).status_code == 200
    session = await _state(db_session, sid)
    far = (session.spotlight_since or datetime.now(UTC)) + timedelta(days=3)
    await _present(redis_client, sid, table["bob_id"], "bob-1")
    with pytest.raises(spotlight.SpotlightRefused) as refused:
        await _open(db_session, redis_client, sid, table["bob_id"], now=far)
    assert refused.value.code == "not_your_spotlight"
    handed = await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    assert handed.status_code == 200
    assert handed.json()["holder"]["character_id"] == table["bob_pc_id"]


@pytest.mark.asyncio
async def test_owner_override_hands_over_and_refuses_bad_holders(api_client, table, db_session):
    await _seat_bob_and_eve(api_client, table, db_session)
    before = (await _spotlight(api_client, table))["version"]
    handed = await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    assert handed.status_code == 200
    assert handed.json()["holder"]["character_id"] == table["bob_pc_id"]
    assert handed.json()["version"] == before + 1

    # A hero that is neither seated nor the owner's cannot hold the spotlight.
    stranger = await _user(db_session, "stranger@example.test")
    stranger_pc = await _character(db_session, stranger, "Stranger", table["campaign"])
    stranger_pc_id = stranger_pc.id
    refused = await _patch_spotlight(api_client, table, {"holder_character_id": stranger_pc_id})
    assert refused.status_code == 422
    assert refused.json()["code"] == "spotlight_holder_invalid"


@pytest.mark.asyncio
async def test_mode_and_timeout_changes_are_validated_and_applied(api_client, table, db_session):
    await _seat_bob_and_eve(api_client, table, db_session)
    released = await _patch_spotlight(api_client, table, {"holder_character_id": None})
    assert released.status_code == 200
    # Round robin never leaves a free spotlight behind: the owner's hero, present, takes it.
    assert released.json()["holder"]["character_id"] == table["alice_pc_id"]

    switched = await _patch_spotlight(api_client, table, {"mode": "free_for_all"})
    assert switched.status_code == 200 and switched.json()["mode"] == "free_for_all"
    back = await _patch_spotlight(api_client, table, {"mode": "round_robin", "timeout_seconds": 90})
    assert back.status_code == 200
    assert back.json()["mode"] == "round_robin" and back.json()["timeout_seconds"] == 90
    assert (await _patch_spotlight(api_client, table, {"mode": "sometimes"})).status_code == 422
    assert (await _patch_spotlight(api_client, table, {"timeout_seconds": 5})).status_code == 422


@pytest.mark.asyncio
async def test_free_for_all_claims_first_then_cools_down_per_player(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    freed = await _patch_spotlight(
        api_client, table, {"mode": "free_for_all", "holder_character_id": None}
    )
    assert freed.status_code == 200
    await _present(redis_client, sid, table["bob_id"], "bob-1")
    await _present(redis_client, sid, table["eve_id"], "eve-1")
    now = datetime.now(UTC)

    claim = await _open(db_session, redis_client, sid, table["bob_id"], now=now)
    assert claim.change is not None and claim.change.reason == "claimed"
    assert await _commit(db_session, sid, claim.plan, now) == "turn_released"
    assert (await _state(db_session, sid)).active_character_id is None

    # Bob waits out his cooldown; Eve may take the free spotlight at once.
    with pytest.raises(spotlight.SpotlightRefused) as cooling:
        await _open(db_session, redis_client, sid, table["bob_id"], now=now + timedelta(seconds=10))
    assert cooling.value.code == "spotlight_cooldown"
    eve_turn = await _open(db_session, redis_client, sid, table["eve_id"], now=now + timedelta(seconds=10))
    assert eve_turn.plan.holder == table["eve_pc_id"]
    await _commit(db_session, sid, eve_turn.plan, now + timedelta(seconds=10))
    back = await _open(db_session, redis_client, sid, table["bob_id"], now=now + timedelta(seconds=120))
    assert back.plan.holder == table["bob_pc_id"]


@pytest.mark.asyncio
async def test_intro_may_claim_but_never_passes_the_spotlight(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    now = datetime.now(UTC)
    intro = await _open(db_session, redis_client, sid, table["alice_id"], consumes=False, now=now)
    assert intro.plan is not None and intro.plan.consumes is False
    assert await _commit(db_session, sid, intro.plan, now) is None
    assert (await _state(db_session, sid)).active_character_id == table["alice_pc_id"]


@pytest.mark.asyncio
async def test_turn_commit_refuses_when_the_spotlight_moved_while_the_model_thought(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    now = datetime.now(UTC)
    plan = await _open(db_session, redis_client, sid, table["alice_id"], now=now)
    # The owner hands the spotlight elsewhere before the holder's turn commits.
    handed = await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    assert handed.status_code == 200
    with pytest.raises(spotlight.SpotlightRefused) as moved:
        await _commit(db_session, sid, plan.plan, now)
    assert moved.value.code == "spotlight_stale"
    assert (await _state(db_session, sid)).active_character_id == table["bob_pc_id"]


# --- Membership changes release the spotlight; events -------------------------------------------


@pytest.mark.asyncio
async def test_removed_or_demoted_member_releases_the_spotlight_at_once(
    api_client, table, db_session
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    assert (
        await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    ).status_code == 200
    before = (await _spotlight(api_client, table))["version"]

    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign_id']}/members/{table['bob_id']}",
        headers=_auth(table["tokens"]["alice"]),
    )
    assert removed.status_code == 204
    view = await _spotlight(api_client, table)
    assert view["holder"]["character_id"] != table["bob_pc_id"]
    assert view["version"] == before + 1

    assert (
        await _patch_spotlight(api_client, table, {"holder_character_id": table["eve_pc_id"]})
    ).status_code == 200
    demoted = await api_client.patch(
        f"/api/campaigns/{table['campaign_id']}/members/{table['eve_id']}",
        json={"role": "spectator"},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert demoted.status_code == 200
    view = await _spotlight(api_client, table)
    assert view["holder"]["character_id"] != table["eve_pc_id"]
    # Removing Bob freed his seat in the same commit, and demoting Eve freed hers.
    seats = await db_session.scalars(
        select(SessionParticipant.user_id).where(SessionParticipant.session_id == sid)
    )
    assert table["bob_id"] not in list(seats) and table["eve_id"] not in list(seats)


@pytest.mark.asyncio
async def test_every_spotlight_change_is_announced_on_the_table_channel_in_order(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    await _patch_spotlight(api_client, table, {"mode": "free_for_all"})
    await _patch_spotlight(api_client, table, {"timeout_seconds": 120})

    records = await session_channel.read_after(redis_client, sid, 0, count=500, block_ms=1)
    spot = [(seq, rec) for seq, rec in records if rec["envelope"]["type"] == "turn.spotlight_changed"]
    assert len(spot) >= 4  # two seats joined, then three owner changes
    assert [seq for seq, _ in spot] == sorted(seq for seq, _ in spot)
    versions = [rec["envelope"]["data"]["version"] for _, rec in spot]
    assert versions == sorted(versions) and len(set(versions)) == len(versions)
    reasons = {rec["envelope"]["data"]["reason"] for _, rec in spot}
    assert {"seat_joined", "host_set", "mode_changed", "settings_changed"} <= reasons
    for _, rec in spot:
        assert rec["audience"] == AUDIENCE_TABLE
        data = rec["envelope"]["data"]
        assert {"holder", "next_up", "version", "mode", "engaged"} <= set(data)
        assert "@" not in json.dumps(data)


@pytest.mark.asyncio
async def test_solo_table_is_not_engaged_and_emits_nothing(
    api_client, table, db_session, redis_client, model_calls
):
    sid = table["save_id"]
    view = await _spotlight(api_client, table)
    assert view["engaged"] is False
    assert view["holder"]["character_id"] == table["alice_pc_id"]
    assert (await _turn(api_client, table, "alice")).status_code != 409
    assert len(model_calls) == 1
    records = await session_channel.read_after(redis_client, sid, 0, count=500, block_ms=1)
    assert not [r for _s, r in records if r["envelope"]["type"] == "turn.spotlight_changed"]


@pytest.mark.asyncio
async def test_single_pc_shared_table_lets_its_seated_holder_act(
    api_client, table, db_session, redis_client, model_calls
):
    """One seated player whose hero holds the spotlight is the whole table: no new refusals."""
    session = await _state(db_session, table["save_id"])
    session.active_character_id = None
    await db_session.commit()
    invite = await _invite(api_client, table)
    assert (await _accept(api_client, "bob", table, invite["token"], table["bob_pc_id"])).status_code == 200
    handed = await api_client.patch(
        f"/api/sessions/{table['save_id']}",
        json={"active_character_id": table["bob_pc_id"]},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert handed.status_code == 200
    view = await _spotlight(api_client, table, "bob")
    assert view["engaged"] is False
    assert (await _turn(api_client, table, "bob")).status_code != 409
    assert len(model_calls) == 1
    records = await session_channel.read_after(redis_client, table["save_id"], 0, count=500, block_ms=1)
    assert not [r for _s, r in records if r["envelope"]["type"] == "turn.spotlight_changed"]


@pytest.mark.asyncio
async def test_a_busy_table_refuses_a_spotlight_change_without_changing_anything(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    before = await _state(db_session, sid)
    version, holder = before.spotlight_version, before.active_character_id
    # A turn is in flight: someone else holds the table lock.
    in_flight = await acquire_turn_lock(sid, redis_client)
    assert in_flight is not None
    try:
        response = await _patch_spotlight(api_client, table, {"holder_character_id": table["bob_pc_id"]})
    finally:
        await in_flight.release()
    assert response.status_code == 409
    after = await _state(db_session, sid)
    assert after.spotlight_version == version and after.active_character_id == holder


def test_the_spotlight_event_is_registered_on_the_table_audience() -> None:
    assert EVENT_TYPES[spotlight.EVENT_TYPE] == ("system", AUDIENCE_TABLE)


@pytest.mark.asyncio
async def test_a_due_handover_is_reported_by_reads_and_applied_only_under_the_lock(
    api_client, table, db_session, redis_client
):
    """Lazy timeout: GET reports handover_due and mutates nothing; the next locked request applies it."""
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    start = datetime.now(UTC)
    session = await _state(db_session, sid)
    session.spotlight_timeout_seconds = 60
    session.spotlight_since = start
    await db_session.commit()
    await _present(redis_client, sid, table["bob_id"], "bob-1")
    later = start + timedelta(seconds=120)
    before = await _state(db_session, sid)
    version, holder = before.spotlight_version, before.active_character_id

    view = await spotlight.public_view(db_session, redis_client, sid, now=later)
    assert view["handover_due"] is True
    assert view["holder"]["character_id"] == table["alice_pc_id"]
    after_read = await _state(db_session, sid)
    assert after_read.spotlight_version == version and after_read.active_character_id == holder

    opened = await _open(db_session, redis_client, sid, table["bob_id"], now=later)
    assert opened.plan.holder == table["bob_pc_id"]
    applied = await _state(db_session, sid)
    assert applied.spotlight_version == version + 1
    late_view = await spotlight.public_view(db_session, redis_client, sid, now=later)
    assert late_view["handover_due"] is False
