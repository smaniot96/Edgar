# ruff: noqa: F811  (pytest fixtures are requested by argument name)
"""Table pacing policy and queued intended actions (EDG-051).

Same harness as the EDG-048 spotlight tests (Postgres testcontainer, in-process Redis fake, model
stubbed). Time is passed as ``now`` so every timeout is controlled.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

import db.postgres.session as db_session_module
from apps.api.schemas.channel import AUDIENCE_TABLE, EVENT_TYPES
from apps.api.services import pacing, queued_actions, session_channel, spotlight
from tests.api.test_campaign_membership import _auth, table, token_mode  # noqa: F401
from tests.api.test_spotlight_arbitration import (  # noqa: F401
    _captured_ids,
    _commit,
    _open,
    _patch_spotlight,
    _present,
    _seat_bob_and_eve,
    _spotlight,
    _state,
    _turn,
    model_calls,
)


async def _present_at(redis, session_id: int, user_id: int, conn: str, at: datetime) -> None:
    """Show a member on the channel as of ``at`` (presence expires against the same controlled clock)."""
    await session_channel.connect(
        redis,
        session_id,
        conn,
        {"user_id": user_id, "display_name": None, "character_id": None, "character_name": None},
        now=at.timestamp(),
    )


async def _queue(client: AsyncClient, table, who: str, text="I search the wagon.", **extra):
    return await client.post(
        f"/api/sessions/{table['save_id']}/queue",
        json={"text": text, **extra},
        headers=_auth(table["tokens"][who]),
    )


async def _read_queue(client: AsyncClient, table, who: str) -> dict:
    response = await client.get(
        f"/api/sessions/{table['save_id']}/queue", headers=_auth(table["tokens"][who])
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _hand_to(client, table, character_key: str):
    handed = await _patch_spotlight(client, table, {"holder_character_id": table[character_key]})
    assert handed.status_code == 200, handed.text
    return handed.json()


# --- Policy ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_member_reads_the_pacing_policy_and_only_the_owner_changes_it(
    api_client, table, db_session
):
    await _seat_bob_and_eve(api_client, table, db_session)
    default = (await _spotlight(api_client, table, "bob"))["pacing"]
    assert default["policy"] == "standard" and default["timeout_seconds"] == 300
    assert default["auto_pass"] is False

    changed = await _patch_spotlight(api_client, table, {"pacing": "fast"})
    assert changed.status_code == 200
    for who in ("alice", "bob", "eve"):
        view = (await _spotlight(api_client, table, who))["pacing"]
        assert (view["policy"], view["timeout_seconds"], view["auto_pass"]) == ("fast", 60, True)
        assert view["summary"] and view["queue_ttl_seconds"] == 600

    relaxed = (await _patch_spotlight(api_client, table, {"pacing": "relaxed"})).json()["pacing"]
    assert (relaxed["timeout_seconds"], relaxed["auto_pass"], relaxed["queue_ttl_seconds"]) == (3600, False, 86400)

    # A non-owner cannot change it (same 404 as every owner-only spotlight change).
    denied = await api_client.patch(
        f"/api/sessions/{table['save_id']}/spotlight",
        json={"pacing": "fast"},
        headers=_auth(table["tokens"]["bob"]),
    )
    assert denied.status_code == 404
    # A policy and an explicit timeout together are ambiguous.
    both = await _patch_spotlight(api_client, table, {"pacing": "fast", "timeout_seconds": 90})
    assert both.status_code == 422
    # An explicit timeout makes the policy custom, and re-selecting a named policy restores it.
    custom = (await _patch_spotlight(api_client, table, {"timeout_seconds": 90})).json()["pacing"]
    assert custom["policy"] == "custom" and custom["timeout_seconds"] == 90
    restored = (await _patch_spotlight(api_client, table, {"pacing": "standard"})).json()["pacing"]
    assert restored["policy"] == "standard" and restored["timeout_seconds"] == 300


@pytest.mark.asyncio
async def test_a_pacing_change_bumps_the_version_and_is_announced_to_the_table(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    before = (await _spotlight(api_client, table))["version"]
    changed = (await _patch_spotlight(api_client, table, {"pacing": "relaxed"})).json()
    assert changed["version"] == before + 1
    # Choosing the policy already in force changes nothing and announces nothing.
    same = (await _patch_spotlight(api_client, table, {"pacing": "relaxed"})).json()
    assert same["version"] == before + 1
    records = await session_channel.read_after(redis_client, table["save_id"], 0, count=500, block_ms=1)
    pacing_events = [
        rec for _, rec in records
        if rec["envelope"]["type"] == "turn.spotlight_changed"
        and rec["envelope"]["data"]["reason"] == "pacing_changed"
    ]
    assert len(pacing_events) == 1
    assert pacing_events[0]["audience"] == AUDIENCE_TABLE
    assert pacing_events[0]["envelope"]["data"]["pacing"]["policy"] == "relaxed"


@pytest.mark.asyncio
async def test_fast_policy_passes_a_holder_who_left_the_table_but_standard_does_not(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    start = datetime.now(UTC)
    session = await _state(db_session, sid)
    session.spotlight_since = start
    await db_session.commit()
    await _present(redis_client, sid, table["bob_id"], "bob-1")  # Alice (holder) is not on the channel

    # Standard: absence alone never passes the spotlight inside the 300 second timeout.
    with pytest.raises(spotlight.SpotlightRefused) as held:
        await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=45))
    assert held.value.code == "not_your_spotlight"

    assert (await _patch_spotlight(api_client, table, {"pacing": "fast"})).status_code == 200
    session = await _state(db_session, sid)
    session.spotlight_since = start
    await db_session.commit()
    # Fast: a short reconnect grace, then the absent holder is passed at once (not after a minute).
    with pytest.raises(spotlight.SpotlightRefused):
        await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=10))
    view = await spotlight.public_view(db_session, redis_client, sid, now=start + timedelta(seconds=25))
    assert view["handover_due"] is True
    opened = await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=25))
    assert opened.change is not None and opened.change.reason == "auto_pass"
    assert opened.plan.holder == table["bob_pc_id"]


@pytest.mark.asyncio
async def test_a_present_but_idle_holder_keeps_the_spotlight_until_the_timeout(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    await _patch_spotlight(api_client, table, {"pacing": "fast"})
    start = datetime.now(UTC)
    session = await _state(db_session, sid)
    session.spotlight_since = start
    await db_session.commit()
    at40, at70 = start + timedelta(seconds=40), start + timedelta(seconds=70)
    for who in ("alice", "bob"):
        await _present_at(redis_client, sid, table[f"{who}_id"], f"{who}-1", at40)
    with pytest.raises(spotlight.SpotlightRefused):
        await _open(db_session, redis_client, sid, table["bob_id"], now=at40)
    view = await spotlight.public_view(db_session, redis_client, sid, now=start)
    assert view["turn_deadline"] == (start + timedelta(seconds=60)).isoformat()
    # Alice stays on the channel past the timeout (idle, not gone): the timeout, not auto-pass, ends it.
    await _present_at(redis_client, sid, table["alice_id"], "alice-1", at70)
    opened = await _open(db_session, redis_client, sid, table["bob_id"], now=start + timedelta(seconds=70))
    assert opened.change.reason == "timeout"


# --- Queued action lifecycle -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_submit_edit_cancel_and_visibility(api_client, table, db_session):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    version_before = (await _spotlight(api_client, table))["version"]

    created = await _queue(api_client, table, "bob", request_id="try-1")
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["status"] == "queued" and item["version"] == 1
    assert item["text"] == "I search the wagon." and item["character_id"] == table["bob_pc_id"]

    # A retry of the same submission is the same item. A second, different one is refused.
    retry = await _queue(api_client, table, "bob", request_id="try-1")
    assert retry.status_code == 200 and retry.json()["id"] == item["id"]
    other = await _queue(api_client, table, "bob", text="Something else", request_id="try-2")
    assert other.status_code == 409 and other.json()["code"] == "queued_action_exists"

    # The author reads the text. The owner, another player and the table see only that it is queued.
    mine = (await _read_queue(api_client, table, "bob"))["items"][0]
    assert mine["text"] == "I search the wagon."
    for who in ("alice", "eve"):
        seen = (await _read_queue(api_client, table, who))["items"]
        assert [i["user_id"] for i in seen] == [table["bob_id"]] and seen[0]["text"] is None
        assert "wagon" not in json.dumps(seen)
        assert seen[0]["status"] == "queued"
    view = await _spotlight(api_client, table, "eve")
    assert [m["queued"] for m in view["next_up"]] == [True, False]
    assert "wagon" not in json.dumps(view)

    # Queueing never touches the spotlight.
    assert (await _spotlight(api_client, table))["version"] == version_before

    edited = await api_client.patch(
        f"/api/sessions/{sid}/queue/{item['id']}",
        json={"text": "I search the wagon carefully.", "expected_version": 1},
        headers=_auth(table["tokens"]["bob"]),
    )
    assert edited.status_code == 200 and edited.json()["version"] == 2
    again = await api_client.patch(
        f"/api/sessions/{sid}/queue/{item['id']}",
        json={"text": "I search the wagon carefully.", "expected_version": 1},
        headers=_auth(table["tokens"]["bob"]),
    )
    assert again.status_code == 200 and again.json()["version"] == 2  # idempotent retry
    lost = await api_client.patch(
        f"/api/sessions/{sid}/queue/{item['id']}",
        json={"text": "Different", "expected_version": 1},
        headers=_auth(table["tokens"]["bob"]),
    )
    assert lost.status_code == 409 and lost.json()["code"] == "queued_action_changed"
    # Another player cannot edit it.
    foreign = await api_client.patch(
        f"/api/sessions/{sid}/queue/{item['id']}",
        json={"text": "Hijack", "expected_version": 2},
        headers=_auth(table["tokens"]["eve"]),
    )
    assert foreign.status_code == 409 and foreign.json()["code"] == "queued_action_missing"
    # Nor cancel it; the author and the owner can, and cancelling twice is fine.
    path = f"/api/sessions/{sid}/queue/{item['id']}"
    assert (await api_client.delete(path, headers=_auth(table["tokens"]["eve"]))).status_code == 409
    assert (await api_client.delete(path, headers=_auth(table["tokens"]["bob"]))).status_code == 204
    assert (await api_client.delete(path, headers=_auth(table["tokens"]["bob"]))).status_code == 204
    assert (await _read_queue(api_client, table, "bob"))["items"] == []

    again = await _queue(api_client, table, "eve")
    assert again.status_code == 201
    owner_cancel = await api_client.delete(
        f"/api/sessions/{sid}/queue/{again.json()['id']}", headers=_auth(table["tokens"]["alice"])
    )
    assert owner_cancel.status_code == 204


@pytest.mark.asyncio
async def test_the_holder_cannot_queue_and_solo_or_single_pc_tables_cannot_at_all(
    api_client, table, db_session, model_calls
):
    # Solo: no table to wait at.
    solo = await _queue(api_client, table, "alice")
    assert solo.status_code == 409 and solo.json()["code"] == "queue_unavailable"
    played = await api_client.post(
        f"/api/sessions/{table['save_id']}/turn",
        json={"message": "Hi", "queued_action_id": "a" * 32},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert played.status_code == 409 and played.json()["code"] == "queue_unavailable"
    assert model_calls == []
    view = await _spotlight(api_client, table)
    assert view["engaged"] is False and view["queue"]["items"] == []
    assert view["pacing"]["policy"] == "standard"

    await _seat_bob_and_eve(api_client, table, db_session)
    holder = await _queue(api_client, table, "alice")
    assert holder.status_code == 409 and holder.json()["code"] == "queue_not_needed"
    stale = await _queue(api_client, table, "bob", spotlight_version=0)
    assert stale.status_code == 409 and stale.json()["code"] == "spotlight_stale"
    # Spectators have no seat and no queue.
    assert (await _read_queue(api_client, table, "bob"))["items"] == []


@pytest.mark.asyncio
async def test_a_queued_action_is_validated_again_when_its_turn_comes(
    api_client, table, db_session, redis_client, model_calls
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    queued = (await _queue(api_client, table, "bob")).json()
    claim = spotlight.QueuedClaim(queued["id"], queued["version"], queued["text"])
    start = datetime.now(UTC)

    # Not Bob's turn yet: the normal gate refuses first, and the queue is untouched.
    with pytest.raises(spotlight.SpotlightRefused) as early:
        await _open(db_session, redis_client, sid, table["bob_id"], now=start, queued=claim)
    assert early.value.code == "not_your_spotlight"
    await _hand_to(api_client, table, "bob_pc_id")

    # The text must be what was queued, and the version current.
    for bad, code in (
        (spotlight.QueuedClaim(queued["id"], queued["version"], "Something else"), "queued_action_changed"),
        (spotlight.QueuedClaim(queued["id"], 7, queued["text"]), "queued_action_changed"),
        (spotlight.QueuedClaim("f" * 32, None, queued["text"]), "queued_action_missing"),
    ):
        with pytest.raises(spotlight.SpotlightRefused) as refused:
            await _open(db_session, redis_client, sid, table["bob_id"], now=start, queued=bad)
        assert refused.value.code == code

    # Expired: past the policy's lifetime (an hour on the standard policy).
    with pytest.raises(spotlight.SpotlightRefused) as expired:
        await _open(
            db_session, redis_client, sid, table["bob_id"], now=start + timedelta(hours=2), queued=claim
        )
    assert expired.value.code == "queued_action_expired"

    # Stale: the scene moved on while Bob waited. Refused, never silently run, and still there.
    session = await _state(db_session, sid)
    session.current_scene_id = "somewhere-else"
    await db_session.commit()
    with pytest.raises(spotlight.SpotlightRefused) as stale:
        await _open(db_session, redis_client, sid, table["bob_id"], now=start, queued=claim)
    assert stale.value.code == "queued_action_stale"
    mine = (await _read_queue(api_client, table, "bob"))["items"][0]
    assert mine["status"] == "stale" and mine["stale_reason"] == "scene_changed"
    assert (await _read_queue(api_client, table, "eve"))["items"][0]["status"] == "stale"

    # Editing re-baselines it on the situation as it is now, and it can then be played.
    edited = await api_client.patch(
        f"/api/sessions/{sid}/queue/{queued['id']}",
        json={"text": "I search the wagon in the new place.", "expected_version": 1},
        headers=_auth(table["tokens"]["bob"]),
    )
    assert edited.status_code == 200 and edited.json()["status"] == "queued"
    fresh = spotlight.QueuedClaim(queued["id"], 2, "I search the wagon in the new place.")
    opened = await _open(db_session, redis_client, sid, table["bob_id"], now=start, queued=fresh)
    assert opened.plan.queued_id == queued["id"]
    await _commit(db_session, sid, opened.plan, start)
    assert opened.plan.queue_consumed is True
    assert (await _read_queue(api_client, table, "bob"))["items"] == []
    # Played once: a replay of the same claim finds nothing.
    with pytest.raises(spotlight.SpotlightRefused) as gone:
        await _open(db_session, redis_client, sid, table["bob_id"], now=start, queued=fresh)
    assert gone.value.code in {"queued_action_missing", "not_your_spotlight"}


@pytest.mark.asyncio
async def test_playing_a_queued_action_through_the_turn_route(
    api_client, table, db_session, model_calls
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    queued = (await _queue(api_client, table, "bob", text="I open the chest.")).json()

    async def play(message="I open the chest.", version=1):
        return await api_client.post(
            f"/api/sessions/{sid}/turn",
            json={"message": message, "queued_action_id": queued["id"], "queued_action_version": version},
            headers=_auth(table["tokens"]["bob"]),
        )

    refused = await play()
    assert refused.status_code == 409 and refused.json()["code"] == "not_your_spotlight"
    assert model_calls == []
    await _hand_to(api_client, table, "bob_pc_id")
    session = await _state(db_session, sid)
    session.current_scene_id = "elsewhere"
    await db_session.commit()
    stale = await play()
    assert stale.status_code == 409 and stale.json()["code"] == "queued_action_stale"
    assert model_calls == []
    assert "chest" not in json.dumps(stale.json())

    session = await _state(db_session, sid)
    session.current_scene_id = None
    await db_session.commit()
    ok = await play()
    assert ok.status_code != 409  # the gate passed; the stubbed model then fails
    assert len(model_calls) == 1


@pytest.mark.asyncio
async def test_hero_state_and_combat_changes_make_a_queued_action_stale(
    api_client, table, db_session
):
    await _seat_bob_and_eve(api_client, table, db_session)
    queued = (await _queue(api_client, table, "bob")).json()
    session = await _state(db_session, table["save_id"])
    item = pacing.get_item(session, table["bob_id"])
    now = datetime.now(UTC)
    assert pacing.stale_reason(item, {**item["basis"]}) is None
    assert pacing.stale_reason(item, {**item["basis"], "combat": True}) == "combat_changed"
    assert pacing.stale_reason(item, {**item["basis"], "life": "dead"}) == "hero_state_changed"
    assert pacing.item_status(item, {**item["basis"]}, now)[0] == "queued"
    assert queued["status"] == "queued"


# --- No reordering or duplication --------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_submissions_create_one_item_and_never_move_the_spotlight(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    before = await _state(db_session, sid)
    version, holder = before.spotlight_version, before.active_character_id

    async def submit(user_id: int, request_id: str):
        async with db_session_module.async_session_factory() as db:
            try:
                item, changed = await queued_actions.submit(
                    db, redis_client, sid, user_id, text="Go", request_id=request_id, spotlight_version=None
                )
                return ("ok", item["id"], changed)
            except spotlight.SpotlightRefused as exc:
                return ("refused", exc.code, False)

    same_user = await asyncio.gather(*(submit(table["bob_id"], f"r{n}") for n in range(4)))
    assert [r[0] for r in same_user].count("ok") == 1, same_user
    assert {r[1] for r in same_user if r[0] == "refused"} == {"queued_action_exists"}

    # The same retried request is idempotent across connections.
    retried = await asyncio.gather(*(submit(table["eve_id"], "same") for _ in range(4)))
    assert {r[1] for r in retried} == {retried[0][1]} and all(r[0] == "ok" for r in retried)
    assert [r[2] for r in retried].count(True) == 1

    after = await _state(db_session, sid)
    assert (after.spotlight_version, after.active_character_id) == (version, holder)
    assert len((await _read_queue(api_client, table, "alice"))["items"]) == 2


@pytest.mark.asyncio
async def test_two_concurrent_plays_of_one_queued_action_commit_exactly_once(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    queued = (await _queue(api_client, table, "bob")).json()
    await _hand_to(api_client, table, "bob_pc_id")
    claim = spotlight.QueuedClaim(queued["id"], 1, queued["text"])
    now = datetime.now(UTC)

    async def admit():
        async with db_session_module.async_session_factory() as db:
            return await _open(db, redis_client, sid, table["bob_id"], now=now, queued=claim)

    plans = await asyncio.gather(admit(), admit())

    async def commit(plan):
        async with db_session_module.async_session_factory() as db:
            try:
                session = await _state(db, sid)
                reason = spotlight.apply_turn_commit(session, plan.plan, now=now)
                await db.commit()
                return ("committed", reason)
            except spotlight.SpotlightRefused as exc:
                await db.rollback()
                return ("refused", exc.code)

    results = [await commit(plans[0]), await commit(plans[1])]
    assert [r[0] for r in results].count("committed") == 1, results
    assert [r[1] for r in results if r[0] == "refused"] == ["queued_action_missing"]
    db_session.expire_all()  # the commits came from other connections
    assert (await _read_queue(api_client, table, "bob"))["items"] == []


@pytest.mark.asyncio
async def test_a_player_who_loses_the_seat_loses_the_queued_action(api_client, table, db_session):
    await _seat_bob_and_eve(api_client, table, db_session)
    await _queue(api_client, table, "eve")
    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign_id']}/members/{table['eve_id']}",
        headers=_auth(table["tokens"]["alice"]),
    )
    assert removed.status_code == 204
    assert (await _read_queue(api_client, table, "alice"))["items"] == []


@pytest.mark.asyncio
async def test_expired_actions_disappear_for_the_table_and_are_purged_by_the_next_write(
    api_client, table, db_session, redis_client
):
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    queued = (await _queue(api_client, table, "bob")).json()
    later = datetime.fromisoformat(queued["expires_at"]) + timedelta(seconds=1)
    view = await spotlight.public_view(db_session, redis_client, sid, now=later, viewer_user_id=table["bob_id"])
    assert [i["status"] for i in view["queue"]["items"]] == ["expired"]  # the author is told
    table_view = await spotlight.public_view(db_session, redis_client, sid, now=later)
    assert table_view["queue"]["items"] == [] and table_view["next_up"][0]["queued"] is False
    item, changed = await queued_actions.submit(
        db_session, redis_client, sid, table["bob_id"], text="Again", request_id=None,
        spotlight_version=None, now=later,
    )
    assert changed and item["text"] == "Again" and item["id"] != queued["id"]


# --- Channel -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_events_reach_the_table_without_text_and_carry_a_revision(
    api_client, table, db_session, redis_client
):
    assert EVENT_TYPES["turn.queue_changed"] == ("system", AUDIENCE_TABLE)
    await _seat_bob_and_eve(api_client, table, db_session)
    sid = table["save_id"]
    item = (await _queue(api_client, table, "bob", text="Secret plan")).json()
    await api_client.patch(
        f"/api/sessions/{sid}/queue/{item['id']}",
        json={"text": "Other secret", "expected_version": 1},
        headers=_auth(table["tokens"]["bob"]),
    )
    await api_client.delete(f"/api/sessions/{sid}/queue/{item['id']}", headers=_auth(table["tokens"]["bob"]))
    records = await session_channel.read_after(redis_client, sid, 0, count=500, block_ms=1)
    events = [rec for _, rec in records if rec["envelope"]["type"] == "turn.queue_changed"]
    assert [e["envelope"]["data"]["reason"] for e in events] == ["queued", "edited", "cancelled"]
    revisions = [e["envelope"]["data"]["revision"] for e in events]
    assert revisions == sorted(revisions) and len(set(revisions)) == 3
    for event in events:
        assert event["audience"] == AUDIENCE_TABLE
        assert "secret" not in json.dumps(event).lower()
    assert events[0]["envelope"]["actor"]["user_id"] == table["bob_id"]
    assert [i["user_id"] for i in events[0]["envelope"]["data"]["items"]] == [table["bob_id"]]
    assert events[2]["envelope"]["data"]["items"] == []


@pytest.mark.asyncio
async def test_policy_presets_are_consistent_with_the_spotlight_limits():
    for name in pacing.POLICIES:
        timeout = pacing.preset(name).timeout_seconds
        assert spotlight.MIN_TIMEOUT_SECONDS <= timeout <= spotlight.MAX_TIMEOUT_SECONDS
    assert pacing.preset("standard").timeout_seconds == spotlight.DEFAULT_TIMEOUT_SECONDS
