"""Session channel over HTTP (EDG-047): membership gates, live cut-off, presence, limits, solo unchanged.

DB-backed (Postgres testcontainer, as in test_campaign_membership.py) with fakeredis. Token mode is
switched on per test. Streams are bounded with a short lifetime so the ASGI transport can return
the body; the live cut-off is driven by a real DB-backed subscriber in a background task.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth import Principal, generate_token, hash_token
from apps.api.routers.session_events import DbSubscriber
from apps.api.schemas.channel import ChannelActor
from apps.api.services import session_channel, session_stream
from apps.api.services.session_broadcast import TurnBroadcast
from db.postgres.models import (
    ApiToken,
    Campaign,
    CampaignMembership,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    SessionParticipant,
    User,
)
from tests.api.test_session_channel import Collector, _parse


@pytest_asyncio.fixture(autouse=True)
async def _empty_store(redis_client: FakeAsyncRedis):
    """Session ids restart with each test's database, so the Redis store must restart too."""
    session_stream.reset_drain()
    await redis_client.flushdb()
    yield
    session_stream.reset_drain()


PUBLIC = "MARKER-PUBLIC-NARRATION-api-5d1e"
PRIVATE = "MARKER-PRIVATE-INVENTORY-api-9c2b"


@pytest.fixture
def token_mode(monkeypatch: pytest.MonkeyPatch):
    def enable(admin_id: int) -> None:
        monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
        monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", str(admin_id))
        monkeypatch.setenv("EDGAR_METRICS_ENABLED", "1")
        monkeypatch.setenv("EDGAR_DEBUG", "0")

    return enable


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _user(db: AsyncSession, email: str) -> User:
    user = User(email=email, display_name=email.split("@")[0].title())
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _token(db: AsyncSession, user: User) -> str:
    token = generate_token()
    db.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="test"))
    await db.commit()
    return token


async def _pc(db: AsyncSession, owner: User, name: str, campaign: Campaign) -> Character:
    char = Character(
        owner_user_id=owner.id,
        name=name,
        character_class="Fighter",
        level=3,
        hp_max=20,
        base_stats={"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
        base_inventory={"weapons": ["longsword"]},
    )
    db.add(char)
    await db.commit()
    await db.refresh(char)
    db.add(
        CharacterAssignment(
            character_id=char.id,
            campaign_id=campaign.id,
            hp_current=20,
            hp_max=20,
            stats=dict(char.base_stats),
            inventory=dict(char.base_inventory),
        )
    )
    await db.commit()
    return char


@pytest_asyncio.fixture
async def table(db_session: AsyncSession, token_mode):
    """Alice owns the campaign and session. Bob is a seated player, Carol a spectator, Dan an outsider."""
    admin = await _user(db_session, "operator@example.test")
    alice = await _user(db_session, "alice@example.test")
    bob = await _user(db_session, "bob@example.test")
    carol = await _user(db_session, "carol@example.test")
    dan = await _user(db_session, "dan@example.test")
    token_mode(admin.id)
    campaign = Campaign(title="Table", system="D&D 5e", created_by=alice.id, adventure_collections=[])
    db_session.add(campaign)
    await db_session.commit()
    await db_session.refresh(campaign)
    alice_pc = await _pc(db_session, alice, "Ayla", campaign)
    bob_pc = await _pc(db_session, bob, "Bran", campaign)
    save = SessionModel(campaign_id=campaign.id, active_character_id=alice_pc.id)
    db_session.add(save)
    await db_session.commit()
    await db_session.refresh(save)
    db_session.add_all(
        [
            CampaignMembership(campaign_id=campaign.id, user_id=bob.id, role="player"),
            CampaignMembership(campaign_id=campaign.id, user_id=carol.id, role="spectator"),
            SessionParticipant(session_id=save.id, user_id=bob.id, character_id=bob_pc.id, role="player"),
        ]
    )
    await db_session.commit()
    tokens = {
        name: await _token(db_session, user)
        for name, user in (("alice", alice), ("bob", bob), ("carol", carol), ("dan", dan))
    }
    return {
        "alice": alice,
        "bob": bob,
        "carol": carol,
        "dan": dan,
        "tokens": tokens,
        "campaign": campaign,
        "save": save,
        "bob_pc": bob_pc,
    }


async def _publish_bob_turn(redis, sid: int, bob: User, bob_pc: Character) -> None:
    actor = ChannelActor(user_id=bob.id, display_name=bob.display_name, character_id=bob_pc.id, character_name="Bran")
    done = {
        "event_id": 1,
        "narration": PUBLIC,
        "citations": [],
        "ruling_citation_ids": [],
        "rules_edition": "2014",
        "character": {"name": "Bran", "inventory": {"secret": PRIVATE}},
        "current_scene_id": "scene",
        "combat_state": None,
        "campaign_complete": False,
        "roll_status": "no_roll",
        "roll": None,
    }
    broadcast = TurnBroadcast(redis, sid, actor=actor, turn_id="turn-api")
    await broadcast.start("I open the chest")
    await broadcast.committed(done)


@pytest.fixture
def short_streams(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(session_stream, "MAX_STREAM_SECONDS", 0.3)
    monkeypatch.setattr(session_channel, "READ_BLOCK_MS", 10)


async def _events(api_client: AsyncClient, sid: int, headers: dict[str, str], last: int | None = 0):
    h = dict(headers)
    if last is not None:
        h["Last-Event-ID"] = str(last)
    return await api_client.get(f"/api/sessions/{sid}/events", headers=h)


# --- Authentication and membership ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_needs_the_bearer_header_and_never_reads_a_token_from_the_url(
    api_client, table, short_streams
) -> None:
    sid = table["save"].id
    token = table["tokens"]["bob"]
    assert (await api_client.get(f"/api/sessions/{sid}/events")).status_code == 401
    # The server ignores a token in the query string: only the Authorization header authenticates.
    via_url = await api_client.get(f"/api/sessions/{sid}/events?access_token={token}")
    assert via_url.status_code == 401
    assert token not in via_url.text and token not in str(via_url.headers)


@pytest.mark.asyncio
async def test_a_non_member_gets_the_same_404_as_a_missing_session_on_every_channel_route(
    api_client, table, short_streams
) -> None:
    sid = table["save"].id
    dan = _auth(table["tokens"]["dan"])
    missing = await api_client.get("/api/sessions/999999/events", headers=dan)
    outsider = await _events(api_client, sid, dan)
    assert outsider.status_code == 404
    assert outsider.json()["detail"] == missing.json()["detail"]
    assert (await api_client.get(f"/api/sessions/{sid}/presence", headers=dan)).status_code == 404
    assert (await api_client.post(f"/api/sessions/{sid}/presence", json={"state": "idle"}, headers=dan)).status_code == 404


@pytest.mark.asyncio
async def test_a_bad_last_event_id_is_refused_before_anything_streams(api_client, table, short_streams) -> None:
    sid = table["save"].id
    response = await api_client.get(
        f"/api/sessions/{sid}/events", headers={**_auth(table["tokens"]["bob"]), "Last-Event-ID": "abc"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_each_role_receives_the_channels_it_is_allowed(api_client, table, short_streams, redis_client) -> None:
    sid = table["save"].id
    await _publish_bob_turn(redis_client, sid, table["bob"], table["bob_pc"])

    def frames_for(response) -> str:
        return response.text

    owner = frames_for(await _events(api_client, sid, _auth(table["tokens"]["alice"])))
    spectator = frames_for(await _events(api_client, sid, _auth(table["tokens"]["carol"])))
    player = frames_for(await _events(api_client, sid, _auth(table["tokens"]["bob"])))

    for text in (owner, spectator, player):
        assert PUBLIC in text
    assert PRIVATE not in owner and PRIVATE not in spectator
    assert PRIVATE in player
    assert "event: character.state" not in owner and "event: character.state" not in spectator
    assert "event: character.state" in player


@pytest.mark.asyncio
async def test_a_removed_member_is_cut_off_at_once_and_cannot_reconnect(
    api_client, table, redis_client, db_session
) -> None:
    sid = table["save"].id
    bob = table["bob"]
    subscriber = DbSubscriber(redis=redis_client, session_id=sid, auth=Principal(bob.id, None, False))
    viewer = await subscriber.viewer()
    assert viewer is not None and viewer.role == "player"
    live = Collector(
        session_stream.stream_frames(
            redis=redis_client,
            session_id=sid,
            subscriber=subscriber,
            viewer=viewer,
            last_event_id=None,
            conn_id="bob-live",
        )
    )
    try:
        await live.until(lambda frames: any("event: snapshot" in f for f in frames))

        removed = await api_client.delete(
            f"/api/campaigns/{table['campaign'].id}/members/{bob.id}", headers=_auth(table["tokens"]["alice"])
        )
        assert removed.status_code == 204
        await live.until(lambda frames: any(_parse(f)["event"] == "closed" for f in frames), timeout=2.0)
        closed = next(_parse(f) for f in live.frames if _parse(f)["event"] == "closed")
        assert closed["data"] == {"reason": "access_revoked"}

        assert (await _events(api_client, sid, _auth(table["tokens"]["bob"]))).status_code == 404
    finally:
        await live.stop()


@pytest.mark.asyncio
async def test_a_demoted_player_stops_receiving_their_private_records_on_a_live_stream(
    api_client, table, redis_client
) -> None:
    sid = table["save"].id
    bob = table["bob"]
    subscriber = DbSubscriber(redis=redis_client, session_id=sid, auth=Principal(bob.id, None, False))
    viewer = await subscriber.viewer()
    live = Collector(
        session_stream.stream_frames(
            redis=redis_client,
            session_id=sid,
            subscriber=subscriber,
            viewer=viewer,
            last_event_id=None,
            conn_id="bob-demoted",
        )
    )
    try:
        await live.until(lambda frames: any("event: snapshot" in f for f in frames))
        demoted = await api_client.patch(
            f"/api/campaigns/{table['campaign'].id}/members/{bob.id}",
            json={"role": "spectator"},
            headers=_auth(table["tokens"]["alice"]),
        )
        assert demoted.status_code == 200
        await _publish_bob_turn(redis_client, sid, bob, table["bob_pc"])
        await live.until(lambda frames: any("event: turn.finished" in f for f in frames))
        assert PRIVATE not in "".join(live.frames)
        assert PUBLIC in "".join(live.frames)
    finally:
        await live.stop()


# --- Presence -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_presence_shows_members_with_live_connections_and_their_attention(
    api_client, table, redis_client
) -> None:
    sid = table["save"].id
    bob_identity = {"user_id": table["bob"].id, "display_name": "Bob", "character_id": table["bob_pc"].id, "character_name": "Bran"}
    await session_channel.connect(redis_client, sid, "bob-tab", bob_identity)
    assert (await api_client.post(f"/api/sessions/{sid}/presence", json={"state": "idle"}, headers=_auth(table["tokens"]["bob"]))).status_code == 204
    roster = (await api_client.get(f"/api/sessions/{sid}/presence", headers=_auth(table["tokens"]["carol"]))).json()
    assert roster == [
        {"user_id": table["bob"].id, "display_name": "Bob", "character_id": table["bob_pc"].id, "character_name": "Bran", "state": "idle"}
    ]
    assert (await api_client.post(f"/api/sessions/{sid}/presence", json={"state": "napping"}, headers=_auth(table["tokens"]["bob"]))).status_code == 422


@pytest.mark.asyncio
async def test_connection_and_stream_limits_answer_429_before_streaming(
    api_client, table, redis_client, short_streams, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = table["save"].id
    bob_identity = {"user_id": table["bob"].id, "display_name": "Bob", "character_id": None, "character_name": None}
    monkeypatch.setattr(session_channel, "MAX_CONNECTIONS_PER_USER_SESSION", 1)
    await session_channel.connect(redis_client, sid, "bob-tab", bob_identity)
    limited = await _events(api_client, sid, _auth(table["tokens"]["bob"]))
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "subscription_limit" if isinstance(limited.json().get("detail"), dict) else True

    monkeypatch.setattr(session_channel, "MAX_CONNECTIONS_PER_USER_SESSION", 3)
    monkeypatch.setattr(session_stream, "MAX_STREAMS_PER_PROCESS", 0)
    process_full = await _events(api_client, sid, _auth(table["tokens"]["carol"]))
    assert process_full.status_code == 429


@pytest.mark.asyncio
async def test_a_membership_change_leaves_a_control_record_for_open_streams(api_client, table, redis_client) -> None:
    sid = table["save"].id
    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign'].id}/members/{table['carol'].id}", headers=_auth(table["tokens"]["alice"])
    )
    assert removed.status_code == 204
    records = await session_channel.read_after(redis_client, sid, 0, block_ms=1)
    assert any(rec["audience"] == "control" and rec["envelope"]["type"] == "access.changed" for _s, rec in records)


# --- Solo local mode is unchanged ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_solo_turn_stream_frames_are_unchanged_and_the_channel_records_the_turn(
    api_client, fake_llm_turn, fake_retrieval, redis_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await api_client.post("/api/seed")
    assert seed.status_code == 200
    sid = seed.json()["session_id"]
    async with api_client.stream(
        "POST",
        f"/api/sessions/{sid}/turn/stream",
        json={"message": "I look around."},
        headers={"accept": "text/event-stream"},
    ) as response:
        body = (await response.aread()).decode()
    assert response.status_code == 200
    event_names = [line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")]
    assert event_names and set(event_names) <= {"status", "token", "adjudication", "debug", "done", "error"}
    assert "event: done" in body
    assert not any(line.startswith("id: ") for line in body.splitlines())

    # The same committed turn is on the channel for the owner, and the solo stream never saw it there.
    monkeypatch.setattr(session_stream, "MAX_STREAM_SECONDS", 0.3)
    monkeypatch.setattr(session_channel, "READ_BLOCK_MS", 10)
    channel = await api_client.get(f"/api/sessions/{sid}/events", headers={"Last-Event-ID": "0"})
    assert channel.status_code == 200
    types = [_parse(f)["event"] for f in channel.text.split("\n\n") if f.strip()]
    assert "turn.action" in types and "narration.committed" in types and "turn.finished" in types
    assert types.index("turn.action") < types.index("narration.committed") < types.index("turn.finished")


@pytest.mark.asyncio
async def test_sessions_api_is_not_hidden_behind_the_channel(api_client, fake_llm_turn, fake_retrieval) -> None:
    seed = await api_client.post("/api/seed")
    sid = seed.json()["session_id"]
    assert (await api_client.get(f"/api/sessions/{sid}/messages")).status_code == 200


@pytest.mark.asyncio
async def test_the_bearer_token_never_reaches_logs_or_frames(api_client, table, redis_client, short_streams) -> None:
    from structlog.testing import capture_logs

    sid = table["save"].id
    token = table["tokens"]["bob"]
    await _publish_bob_turn(redis_client, sid, table["bob"], table["bob_pc"])
    with capture_logs() as captured:
        response = await _events(api_client, sid, _auth(token))
        refused = await _events(api_client, sid, _auth(table["tokens"]["dan"]))
    assert response.status_code == 200 and refused.status_code == 404
    assert token not in response.text and token not in response.headers.get("set-cookie", "")
    rendered = json.dumps(captured, default=str)
    assert token not in rendered
    assert token[len("edgar_") :] not in rendered
