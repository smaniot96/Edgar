"""Campaign membership, invites, session roster and party-aware state (EDG-046).

DB-backed (Postgres testcontainer; skipped locally without Docker, required in CI). Token mode is
switched on per test. Turn and intro routes are only exercised up to their authorization gate, which
runs before any model call, so no provider is contacted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth import generate_token, hash_token
from apps.api.services.character_assignments import character_play_state
from apps.api.services.session_party import session_party
from apps.api.services.world_writes import apply_adjudication
from db.postgres.models import (
    ApiToken,
    Campaign,
    CampaignInvite,
    CampaignMembership,
    Character,
    CharacterAssignment,
    Session as SessionModel,
    SessionParticipant,
    User,
)


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


def _public_body(response) -> dict:
    """An error body without its per-request support reference, for equality checks."""
    body = dict(response.json())
    body.pop("support_ref", None)
    return body


async def _user(db: AsyncSession, email: str) -> User:
    user = User(email=email, display_name=email.split("@")[0])
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _token(db: AsyncSession, user: User) -> str:
    token = generate_token()
    db.add(ApiToken(user_id=user.id, token_hash=hash_token(token), label="test"))
    await db.commit()
    return token


async def _character(
    db: AsyncSession, owner: User, name: str, campaign: Campaign | None
) -> Character:
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
    if campaign is not None:
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
    """Alice owns a campaign and session; Bob and Carol are invited players/spectators; Dan is outsider."""
    admin = await _user(db_session, "operator@example.test")
    alice = await _user(db_session, "alice@example.test")
    bob = await _user(db_session, "bob@example.test")
    carol = await _user(db_session, "carol@example.test")
    dan = await _user(db_session, "dan@example.test")
    token_mode(admin.id)
    campaign = Campaign(
        title="Table", system="D&D 5e", created_by=alice.id, adventure_collections=[]
    )
    db_session.add(campaign)
    await db_session.commit()
    await db_session.refresh(campaign)
    alice_pc = await _character(db_session, alice, "Ayla", campaign)
    save = SessionModel(campaign_id=campaign.id, active_character_id=alice_pc.id)
    db_session.add(save)
    await db_session.commit()
    await db_session.refresh(save)
    bob_pc = await _character(db_session, bob, "Bran", campaign)
    alice_only_pc = await _character(db_session, alice, "Unassigned", None)
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
        "alice_pc": alice_pc,
        "bob_pc": bob_pc,
        "alice_only_pc": alice_only_pc,
    }


async def _invite(client: AsyncClient, table, *, role="player", session=True, email=None, hours=72):
    body = {"role": role, "expires_in_hours": hours}
    if session:
        body["session_id"] = table["save"].id
    if email is not None:
        body["invited_email"] = email
    cid = table["campaign"].id
    response = await client.post(
        f"/api/campaigns/{cid}/invites", json=body, headers=_auth(table["tokens"]["alice"])
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _accept(client: AsyncClient, who: str, table, token: str, character_id=None):
    body: dict = {"token": token}
    if character_id is not None:
        body["character_id"] = character_id
    return await client.post("/api/invites/accept", json=body, headers=_auth(table["tokens"][who]))


# Invites: creation, listing, the stored form, and who may manage them.


@pytest.mark.asyncio
async def test_owner_creates_invite_and_token_is_shown_only_once(api_client, table, db_session):
    created = await _invite(api_client, table)
    assert created["token"].startswith("edginv_")
    assert created["status"] == "active" and created["role"] == "player"
    listed = await api_client.get(
        f"/api/campaigns/{table['campaign'].id}/invites", headers=_auth(table["tokens"]["alice"])
    )
    assert listed.status_code == 200
    assert all("token" not in row for row in listed.json())
    stored = (await db_session.execute(select(CampaignInvite))).scalar_one()
    assert stored.token_hash == hash_token(created["token"])
    assert created["token"] not in stored.token_hash


@pytest.mark.asyncio
async def test_only_the_owner_manages_invites(api_client, table, db_session):
    cid = table["campaign"].id
    created = await _invite(api_client, table)
    bob_token = table["tokens"]["bob"]
    assert (
        await api_client.post(
            f"/api/campaigns/{cid}/invites", json={"role": "player"}, headers=_auth(bob_token)
        )
    ).status_code == 404
    assert (
        await api_client.get(f"/api/campaigns/{cid}/invites", headers=_auth(bob_token))
    ).status_code == 404
    assert (
        await api_client.delete(
            f"/api/campaigns/{cid}/invites/{created['id']}", headers=_auth(bob_token)
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_invite_role_is_limited_to_player_or_spectator(api_client, table):
    response = await api_client.post(
        f"/api/campaigns/{table['campaign'].id}/invites",
        json={"role": "owner"},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert response.status_code == 422


# Acceptance: seating the owned character, and the refusals.


@pytest.mark.asyncio
async def test_accept_seats_the_accepting_users_own_character(api_client, table, db_session):
    invite = await _invite(api_client, table)
    response = await _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "campaign_id": table["campaign"].id,
        "role": "player",
        "session_id": table["save"].id,
        "character_id": table["bob_pc"].id,
    }
    seat = (await db_session.execute(select(SessionParticipant))).scalar_one()
    assert (seat.user_id, seat.character_id, seat.session_id) == (
        table["bob"].id,
        table["bob_pc"].id,
        table["save"].id,
    )
    membership = (await db_session.execute(select(CampaignMembership))).scalar_one()
    assert (membership.user_id, membership.role) == (table["bob"].id, "player")
    roster = await api_client.get(
        f"/api/sessions/{table['save'].id}/participants", headers=_auth(table["tokens"]["alice"])
    )
    assert roster.status_code == 200
    assert [(r["character_name"], r["user_id"]) for r in roster.json()] == [
        ("Bran", table["bob"].id)
    ]


@pytest.mark.asyncio
async def test_accept_refuses_a_character_the_user_does_not_own(api_client, table, db_session):
    invite = await _invite(api_client, table)
    response = await _accept(api_client, "bob", table, invite["token"], table["alice_pc"].id)
    assert response.status_code == 404
    assert (await db_session.execute(select(SessionParticipant))).first() is None
    assert (await db_session.execute(select(CampaignMembership))).first() is None
    # The refusal does not consume the invite: Bob can still accept with his own character.
    retry = await _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id)
    assert retry.status_code == 200


@pytest.mark.asyncio
async def test_accept_refuses_an_unassigned_character(api_client, table):
    invite = await _invite(api_client, table)
    response = await _accept(api_client, "alice", table, invite["token"], table["alice_only_pc"].id)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_accept_requires_a_character_for_a_session_player_invite(api_client, table):
    invite = await _invite(api_client, table)
    response = await _accept(api_client, "bob", table, invite["token"])
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_accept_requires_an_authenticated_user(api_client, table):
    invite = await _invite(api_client, table)
    response = await api_client.post("/api/invites/accept", json={"token": invite["token"]})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_reused_invite_is_refused(api_client, table):
    invite = await _invite(api_client, table, session=False)
    assert (await _accept(api_client, "bob", table, invite["token"])).status_code == 200
    again = await _accept(api_client, "carol", table, invite["token"])
    assert again.status_code == 404


@pytest.mark.asyncio
async def test_revoked_invite_is_refused_and_listed_as_revoked(api_client, table):
    cid = table["campaign"].id
    invite = await _invite(api_client, table)
    revoked = await api_client.delete(
        f"/api/campaigns/{cid}/invites/{invite['id']}", headers=_auth(table["tokens"]["alice"])
    )
    assert revoked.status_code == 204
    response = await _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id)
    assert response.status_code == 404
    listed = await api_client.get(
        f"/api/campaigns/{cid}/invites", headers=_auth(table["tokens"]["alice"])
    )
    assert listed.json()[0]["status"] == "revoked"


@pytest.mark.asyncio
async def test_expired_invite_is_refused(api_client, table, db_session):
    invite = await _invite(api_client, table, session=False)
    row = (await db_session.execute(select(CampaignInvite))).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    response = await _accept(api_client, "bob", table, invite["token"])
    assert response.status_code == 404
    assert (await db_session.execute(select(CampaignMembership))).first() is None


@pytest.mark.asyncio
async def test_invite_bound_to_another_account_is_refused(api_client, table, db_session):
    invite = await _invite(api_client, table, session=False, email="carol@example.test")
    wrong = await _accept(api_client, "bob", table, invite["token"])
    assert wrong.status_code == 404
    right = await _accept(api_client, "carol", table, invite["token"])
    assert right.status_code == 200


@pytest.mark.asyncio
async def test_unknown_token_gets_the_same_refusal_as_an_expired_one(api_client, table):
    response = await _accept(api_client, "bob", table, "edginv_" + "x" * 43)
    assert response.status_code == 404
    assert response.json()["detail"] == "Invite not found or no longer valid."


# Roles and access on the routes this ticket touches.


@pytest.mark.asyncio
async def test_spectator_reads_but_cannot_act_or_change_anything(api_client, table, db_session):
    invite = await _invite(api_client, table, role="spectator")
    response = await _accept(api_client, "carol", table, invite["token"])
    assert response.status_code == 200
    assert response.json()["character_id"] is None
    assert (await db_session.execute(select(SessionParticipant))).first() is None
    carol = table["tokens"]["carol"]
    cid, sid = table["campaign"].id, table["save"].id
    assert (await api_client.get(f"/api/campaigns/{cid}", headers=_auth(carol))).status_code == 200
    assert (await api_client.get(f"/api/sessions/{sid}", headers=_auth(carol))).status_code == 200
    assert (
        await api_client.get(f"/api/sessions/{sid}/messages", headers=_auth(carol))
    ).status_code == 200
    # Spectators cannot take the spotlight or intro: the gate refuses before any model call.
    assert (
        await api_client.post(f"/api/sessions/{sid}/intro", headers=_auth(carol))
    ).status_code == 403
    assert (
        await api_client.patch(f"/api/campaigns/{cid}", json={"title": "x"}, headers=_auth(carol))
    ).status_code == 404
    assert (
        await api_client.patch(f"/api/sessions/{sid}", json={}, headers=_auth(carol))
    ).status_code == 404


@pytest.mark.asyncio
async def test_seated_player_without_the_spotlight_is_refused_turns(api_client, table):
    invite = await _invite(api_client, table)
    assert (
        await _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id)
    ).status_code == 200
    sid = table["save"].id
    # Alice's character holds the spotlight at this two-PC table (EDG-048): Bob is refused with the
    # spotlight conflict, before any model call and with nothing spent.
    response = await api_client.post(
        f"/api/sessions/{sid}/intro", headers=_auth(table["tokens"]["bob"])
    )
    assert response.status_code == 409
    assert response.json()["code"] == "not_your_spotlight"


@pytest.mark.asyncio
async def test_non_members_get_404_on_session_and_campaign(api_client, table):
    dan = table["tokens"]["dan"]
    cid, sid = table["campaign"].id, table["save"].id
    assert (await api_client.get(f"/api/campaigns/{cid}", headers=_auth(dan))).status_code == 404
    assert (await api_client.get(f"/api/sessions/{sid}", headers=_auth(dan))).status_code == 404
    assert (
        await api_client.get(f"/api/sessions/{sid}/participants", headers=_auth(dan))
    ).status_code == 404
    assert (
        await api_client.post(f"/api/sessions/{sid}/intro", headers=_auth(dan))
    ).status_code == 404
    assert (await api_client.get("/api/campaigns", headers=_auth(dan))).json() == []


@pytest.mark.asyncio
async def test_membership_lists_the_joined_campaign_and_session(api_client, table):
    invite = await _invite(api_client, table, role="spectator")
    await _accept(api_client, "carol", table, invite["token"])
    campaigns = await api_client.get("/api/campaigns", headers=_auth(table["tokens"]["carol"]))
    assert [c["id"] for c in campaigns.json()] == [table["campaign"].id]
    sessions = await api_client.get("/api/sessions", headers=_auth(table["tokens"]["carol"]))
    assert [s["id"] for s in sessions.json()] == [table["save"].id]


@pytest.mark.asyncio
async def test_owner_turn_gate_is_unchanged_for_a_single_owner_session(
    api_client, table, db_session
):
    alice = table["alice"]
    sid = table["save"].id
    session = await db_session.get(SessionModel, sid)
    from apps.api.auth import Principal, turn_session

    gate = await turn_session(
        db_session, sid, Principal(user_id=alice.id, token_id=None, is_admin=False)
    )
    assert gate.id == session.id
    # Local mode (no principal) is unrestricted, as before this ticket.
    assert (await turn_session(db_session, sid, None)).id == session.id
    assert (await session_party(db_session, session))[0]["name"] == "Ayla"


# Party-aware agent state.


@pytest.mark.asyncio
async def test_party_is_spotlight_first_then_seat_order(api_client, table, db_session):
    invite = await _invite(api_client, table)
    await _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id)
    session = await db_session.get(SessionModel, table["save"].id)
    party = await session_party(db_session, session)
    assert [m["name"] for m in party] == ["Ayla", "Bran"]
    assert party[0] == await character_play_state(
        db_session, table["alice_pc"].id, table["campaign"].id
    )


@pytest.mark.asyncio
async def test_single_pc_party_equals_the_acting_character(db_session, table):
    session = await db_session.get(SessionModel, table["save"].id)
    party = await session_party(db_session, session)
    assert party == [
        await character_play_state(db_session, table["alice_pc"].id, table["campaign"].id)
    ]


@pytest.mark.asyncio
async def test_narrator_prompt_addresses_the_party_only_for_multi_pc(table):
    from agent.nodes.narrator import build_narrator_prompt

    solo = {
        "player_input": "I look around",
        "character": {
            "name": "Ayla",
            "class": "Fighter",
            "level": 3,
            "hp_current": 20,
            "hp_max": 20,
            "inventory": {},
        },
    }
    solo["party"] = [solo["character"]]
    solo_text = "\n".join(str(m.content) for m in build_narrator_prompt(solo))
    assert "Party at the table" not in solo_text
    duo = dict(solo)
    duo["party"] = [solo["character"], {"name": "Bran", "class": "Fighter", "level": 3}]
    duo_text = "\n".join(str(m.content) for m in build_narrator_prompt(duo))
    assert "Party at the table: Ayla (Fighter, level 3); Bran (Fighter, level 3)" in duo_text
    assert "acting player character is Ayla" in duo_text


@pytest.mark.asyncio
async def test_adjudicated_writes_land_on_the_acting_pc_only(db_session, table):
    # Seat Bob's PC so the party has two members; Alice's PC holds the spotlight.
    db_session.add(
        SessionParticipant(
            session_id=table["save"].id,
            user_id=table["bob"].id,
            character_id=table["bob_pc"].id,
            role="player",
        )
    )
    await db_session.commit()
    session = await db_session.get(SessionModel, table["save"].id)
    # Spotlight moves to Bob's character; the adjudicated HP loss must hit Bob's assignment only.
    session.active_character_id = table["bob_pc"].id
    await db_session.commit()

    class _Update(SimpleNamespace):
        def model_copy(self, deep=True):
            return _Update(**vars(self))

    update = _Update(
        hp_delta=-5,
        second_wind_uses_remaining=None,
        class_feature_uses={},
        hit_dice_spent=None,
        add_conditions=[],
        remove_conditions=[],
        spell_slots_expended=None,
        inventory_add=[],
        inventory_remove=[],
    )
    adj = SimpleNamespace(
        scene_id=None,
        character_update=update,
        accepted_story_effects=[],
        flags_set=[],
        flags_cleared=[],
        completion_status=None,
    )
    await apply_adjudication(db_session, session, adj)
    await db_session.commit()
    alice_row = (
        await db_session.execute(
            select(CharacterAssignment).where(
                CharacterAssignment.character_id == table["alice_pc"].id
            )
        )
    ).scalar_one()
    bob_row = (
        await db_session.execute(
            select(CharacterAssignment).where(
                CharacterAssignment.character_id == table["bob_pc"].id
            )
        )
    ).scalar_one()
    assert alice_row.hp_current == 20
    assert bob_row.hp_current == 15


# Member lifecycle (EDG-046): removal, role change, leaving, and re-admission.


async def _join(client: AsyncClient, table, who: str, *, role="player", character=None, session=True):
    invite = await _invite(client, table, role=role, session=session)
    response = await _accept(client, who, table, invite["token"], character)
    assert response.status_code == 200, response.text
    return invite


async def _members(client: AsyncClient, table):
    response = await client.get(
        f"/api/campaigns/{table['campaign'].id}/members", headers=_auth(table["tokens"]["alice"])
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _assert_no_access(client: AsyncClient, table, who: str):
    token = _auth(table["tokens"][who])
    cid, sid = table["campaign"].id, table["save"].id
    assert (await client.get(f"/api/campaigns/{cid}", headers=token)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}", headers=token)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}/messages", headers=token)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}/journal", headers=token)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}/map", headers=token)).status_code == 404
    assert (await client.get(f"/api/sessions/{sid}/participants", headers=token)).status_code == 404
    assert (await client.post(f"/api/sessions/{sid}/intro", headers=token)).status_code == 404
    assert (await client.get("/api/campaigns", headers=token)).json() == []


@pytest.mark.asyncio
async def test_owner_removes_a_player_who_loses_reads_turns_and_seat(api_client, table, db_session):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    alice = _auth(table["tokens"]["alice"])
    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign'].id}/members/{table['bob'].id}", headers=alice
    )
    assert removed.status_code == 204
    await _assert_no_access(api_client, table, "bob")
    assert (await db_session.execute(select(SessionParticipant))).first() is None
    assert (await db_session.execute(select(CampaignMembership))).first() is None
    # The character is never deleted, and the owner keeps playing the table.
    assert await db_session.get(Character, table["bob_pc"].id) is not None
    assert (await api_client.get(f"/api/sessions/{table['save'].id}", headers=alice)).status_code == 200


@pytest.mark.asyncio
async def test_owner_removes_a_spectator(api_client, table):
    await _join(api_client, table, "carol", role="spectator", session=False)
    removed = await api_client.delete(
        f"/api/campaigns/{table['campaign'].id}/members/{table['carol'].id}",
        headers=_auth(table["tokens"]["alice"]),
    )
    assert removed.status_code == 204
    await _assert_no_access(api_client, table, "carol")


@pytest.mark.asyncio
async def test_demoting_a_player_to_spectator_frees_the_seat(api_client, table, db_session):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    cid, bob = table["campaign"].id, table["bob"].id
    patched = await api_client.patch(
        f"/api/campaigns/{cid}/members/{bob}",
        json={"role": "spectator"},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["role"] == "spectator" and patched.json()["seats"] == []
    assert (await db_session.execute(select(SessionParticipant))).first() is None
    bob_token = _auth(table["tokens"]["bob"])
    # Still a member for reads, but a spectator cannot take the spotlight.
    assert (await api_client.get(f"/api/sessions/{table['save'].id}", headers=bob_token)).status_code == 200
    assert (
        await api_client.post(f"/api/sessions/{table['save'].id}/intro", headers=bob_token)
    ).status_code == 403
    promoted = await api_client.patch(
        f"/api/campaigns/{cid}/members/{bob}",
        json={"role": "player"},
        headers=_auth(table["tokens"]["alice"]),
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "player"


@pytest.mark.asyncio
async def test_owner_cannot_be_removed_or_demoted(api_client, table):
    cid, alice_id = table["campaign"].id, table["alice"].id
    owner = _auth(table["tokens"]["alice"])
    assert (
        await api_client.delete(f"/api/campaigns/{cid}/members/{alice_id}", headers=owner)
    ).status_code == 409
    assert (
        await api_client.patch(
            f"/api/campaigns/{cid}/members/{alice_id}", json={"role": "spectator"}, headers=owner
        )
    ).status_code == 409
    # The owner still has full access after both refusals.
    assert (await api_client.get(f"/api/campaigns/{cid}", headers=owner)).status_code == 200


@pytest.mark.asyncio
async def test_only_the_owner_manages_membership(api_client, table):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    await _join(api_client, table, "carol", role="spectator", session=False)
    cid = table["campaign"].id
    bob = _auth(table["tokens"]["bob"])
    assert (
        await api_client.delete(f"/api/campaigns/{cid}/members/{table['carol'].id}", headers=bob)
    ).status_code == 404
    assert (
        await api_client.patch(
            f"/api/campaigns/{cid}/members/{table['carol'].id}", json={"role": "player"}, headers=bob
        )
    ).status_code == 404
    assert (await api_client.get(f"/api/campaigns/{cid}/members", headers=bob)).status_code == 404


@pytest.mark.asyncio
async def test_player_leaves_and_frees_only_their_seat(api_client, table, db_session):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    left = await api_client.post(
        f"/api/campaigns/{table['campaign'].id}/leave", headers=_auth(table["tokens"]["bob"])
    )
    assert left.status_code == 204
    await _assert_no_access(api_client, table, "bob")
    assert (await db_session.execute(select(SessionParticipant))).first() is None
    assert await db_session.get(Character, table["bob_pc"].id) is not None


@pytest.mark.asyncio
async def test_owner_cannot_leave_their_campaign(api_client, table):
    response = await api_client.post(
        f"/api/campaigns/{table['campaign'].id}/leave", headers=_auth(table["tokens"]["alice"])
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_removed_member_needs_a_new_invite_and_the_old_token_stays_consumed(api_client, table):
    cid = table["campaign"].id
    old = await _join(api_client, table, "bob", character=table["bob_pc"].id)
    old_token = old and (await _accept_again_token(api_client, table, "bob"))
    await api_client.delete(
        f"/api/campaigns/{cid}/members/{table['bob'].id}", headers=_auth(table["tokens"]["alice"])
    )
    # The consumed token cannot be replayed to get back in.
    replay = await _accept(api_client, "bob", table, old_token, table["bob_pc"].id)
    assert replay.status_code == 404
    fresh = await _invite(api_client, table)
    back = await _accept(api_client, "bob", table, fresh["token"], table["bob_pc"].id)
    assert back.status_code == 200
    assert back.json()["character_id"] == table["bob_pc"].id


async def _accept_again_token(client: AsyncClient, table, who: str) -> str:
    """Helper for the replay test: the first token is never kept in plaintext by the API, so
    the test mints its own invite and accepts it, then returns that token."""
    invite = await _invite(client, table, session=False)
    assert (await _accept(client, who, table, invite["token"])).status_code == 200
    return invite["token"]


@pytest.mark.asyncio
async def test_members_list_shows_roles_and_seats_without_emails(api_client, table):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    rows = await _members(api_client, table)
    bob_row = next(r for r in rows if r["user_id"] == table["bob"].id)
    assert bob_row["role"] == "player"
    assert bob_row["seats"] == [
        {"session_id": table["save"].id, "character_id": table["bob_pc"].id, "character_name": "Bran"}
    ]
    assert all("email" not in r for r in rows)


# Concurrency: two simultaneous accepts of one invite (EDG-046).


@pytest.mark.asyncio
async def test_concurrent_accepts_of_one_invite_admit_exactly_one(api_client, table, db_session):
    import asyncio

    import db.postgres.session as sm
    from apps.api.dependencies import get_session
    from apps.api.main import app

    carol_pc = await _character(db_session, table["carol"], "Cora", table["campaign"])
    invite = await _invite(api_client, table)

    # api_client routes every request through one shared session. The race is only meaningful
    # with independent connections, so give each request its own session for this test.
    async def independent_session():
        async with sm.async_session_factory() as session:
            yield session

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = independent_session
    try:
        first, second = await asyncio.gather(
            _accept(api_client, "bob", table, invite["token"], table["bob_pc"].id),
            _accept(api_client, "carol", table, invite["token"], carol_pc.id),
        )
    finally:
        app.dependency_overrides[get_session] = previous
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 404], (first.text, second.text)
    loser = first if first.status_code == 404 else second
    assert loser.json()["detail"] == "Invite not found or no longer valid."
    seats = (await db_session.execute(select(SessionParticipant))).scalars().all()
    members = (await db_session.execute(select(CampaignMembership))).scalars().all()
    assert len(seats) == 1 and len(members) == 1
    assert seats[0].user_id == members[0].user_id


# Item 3: player-projection routes are readable by members; secret-bearing routes stay owner-only.


@pytest.mark.asyncio
async def test_members_read_the_player_journal_and_map(api_client, table):
    await _join(api_client, table, "carol", role="spectator", session=False)
    token = _auth(table["tokens"]["carol"])
    sid = table["save"].id
    assert (await api_client.get(f"/api/sessions/{sid}/journal", headers=token)).status_code == 200
    assert (await api_client.get(f"/api/sessions/{sid}/journal/export", headers=token)).status_code == 200
    # An unknown entry id is the same 404 the owner gets; it is not a session-access refusal.
    entry = await api_client.get(f"/api/sessions/{sid}/journal/no-such-entry", headers=token)
    owner_entry = await api_client.get(
        f"/api/sessions/{sid}/journal/no-such-entry", headers=_auth(table["tokens"]["alice"])
    )
    assert entry.status_code == owner_entry.status_code == 404
    assert _public_body(entry) == _public_body(owner_entry)
    assert (await api_client.get(f"/api/sessions/{sid}/map", headers=token)).status_code == 200
    svg = await api_client.get(f"/api/sessions/{sid}/map.svg", headers=token)
    owner_svg = await api_client.get(f"/api/sessions/{sid}/map.svg", headers=_auth(table["tokens"]["alice"]))
    assert svg.status_code == owner_svg.status_code
    asset = await api_client.get(f"/api/sessions/{sid}/assets/no-such-asset", headers=token)
    owner_asset = await api_client.get(
        f"/api/sessions/{sid}/assets/no-such-asset", headers=_auth(table["tokens"]["alice"])
    )
    assert asset.status_code == owner_asset.status_code == 404
    assert _public_body(asset) == _public_body(owner_asset)


@pytest.mark.asyncio
async def test_non_members_get_session_not_found_on_projection_routes(api_client, table):
    dan = _auth(table["tokens"]["dan"])
    sid = table["save"].id
    for path in (
        "/api/sessions/{sid}/journal",
        "/api/sessions/{sid}/journal/export",
        "/api/sessions/{sid}/journal/any",
        "/api/sessions/{sid}/map",
        "/api/sessions/{sid}/map.svg",
        "/api/sessions/{sid}/assets/any",
    ):
        response = await api_client.get(path.format(sid=sid), headers=dan)
        missing = await api_client.get(path.format(sid=999999), headers=dan)
        # A non-member gets exactly what a nonexistent session gets: no existence leak.
        assert response.status_code == missing.status_code == 404, path
        assert _public_body(response) == _public_body(missing), path


@pytest.mark.asyncio
async def test_secret_bearing_and_owner_only_routes_stay_owner_only(api_client, table):
    await _join(api_client, table, "bob", character=table["bob_pc"].id)
    await _join(api_client, table, "carol", role="spectator", session=False)
    cid = table["campaign"].id
    for who in ("bob", "carol"):
        token = _auth(table["tokens"][who])
        for path in (
            f"/api/campaigns/{cid}/npcs",
            f"/api/campaigns/{cid}/world-flags",
            f"/api/campaigns/{cid}/characters",
            f"/api/characters/{table['alice_pc'].id}",
            f"/api/campaigns/{cid}/invites",
        ):
            assert (await api_client.get(path, headers=token)).status_code == 404, (who, path)
        # Mutations are owner-only too.
        assert (
            await api_client.post(
                f"/api/campaigns/{cid}/world-flags",
                json={"key": "x", "value": "y"},
                headers=token,
            )
        ).status_code == 404
        # Session diagnostics are admin-only: a member is refused by the admin gate.
        assert (
            await api_client.get(
                f"/api/sessions/{table['save'].id}/diagnostics/turns", headers=token
            )
        ).status_code == 403


# The solo spoiler-safe review (EDG-176) applies only to a campaign nobody else has joined.


@pytest.mark.asyncio
async def test_solo_review_applies_only_until_someone_joins(table, db_session):
    from apps.api.services.campaign_membership import is_solo_campaign, other_member_count

    campaign = table["campaign"]
    assert await other_member_count(db_session, campaign) == 0
    assert await is_solo_campaign(db_session, campaign)

    membership = CampaignMembership(
        campaign_id=campaign.id, user_id=table["bob"].id, role="spectator"
    )
    db_session.add(membership)
    await db_session.commit()
    assert await other_member_count(db_session, campaign) == 1
    assert not await is_solo_campaign(db_session, campaign)

    await db_session.delete(membership)
    await db_session.commit()
    assert await is_solo_campaign(db_session, campaign)
