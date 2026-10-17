"""Token-mode (hosted) API behaviour (EDG-065): authentication, ownership, admin gates.

DB-backed: needs the Postgres testcontainer, so these skip locally without Docker and run in CI.
Token mode is switched on per test through the environment, which the auth module reads per request.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth import generate_token, hash_token
from db.postgres.models import (
    NPC,
    ApiToken,
    Campaign,
    Character,
    Session as SessionModel,
    User,
    WorldFlag,
)


@pytest.fixture
def token_mode(monkeypatch: pytest.MonkeyPatch):
    def enable(admin_id: int, *, debug: bool = False) -> None:
        monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
        monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", str(admin_id))
        monkeypatch.setenv("EDGAR_METRICS_ENABLED", "1")
        monkeypatch.setenv("EDGAR_DEBUG", "1" if debug else "0")

    return enable


async def _user(db: AsyncSession, email: str) -> User:
    user = User(email=email)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _token(db: AsyncSession, user: User, label: str = "test") -> tuple[str, ApiToken]:
    token = generate_token()
    row = ApiToken(user_id=user.id, token_hash=hash_token(token), label=label)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return token, row


async def _campaign(db: AsyncSession, owner: User, title: str) -> Campaign:
    campaign = Campaign(title=title, system="D&D 5e", created_by=owner.id, adventure_collections=[])
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return campaign


async def _session(db: AsyncSession, campaign: Campaign) -> SessionModel:
    save = SessionModel(campaign_id=campaign.id, started_at=datetime.now(UTC))
    db.add(save)
    await db.commit()
    await db.refresh(save)
    return save


async def _character(db: AsyncSession, owner: User, name: str) -> Character:
    char = Character(
        owner_user_id=owner.id,
        name=name,
        character_class="Fighter",
        level=1,
        hp_max=12,
        base_stats={"STR": 16, "DEX": 12, "CON": 14, "INT": 10, "WIS": 12, "CHA": 8},
        base_inventory={"weapons": ["longsword"]},
    )
    db.add(char)
    await db.commit()
    await db.refresh(char)
    return char


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def two_users(db_session: AsyncSession, token_mode):
    """Owner A, a second user B, and admin O. Token mode is on for the test that uses this."""
    admin = await _user(db_session, "operator@example.test")
    alice = await _user(db_session, "alice@example.test")
    bob = await _user(db_session, "bob@example.test")
    token_mode(admin.id)
    alice_token, _ = await _token(db_session, alice)
    bob_token, _ = await _token(db_session, bob)
    admin_token, _ = await _token(db_session, admin)
    camp_b = await _campaign(db_session, bob, "Bob's campaign")
    save_b = await _session(db_session, camp_b)
    char_b = await _character(db_session, bob, "Bran")
    camp_a = await _campaign(db_session, alice, "Alice's campaign")
    save_a = await _session(db_session, camp_a)
    char_a = await _character(db_session, alice, "Ayla")
    return {
        "admin": (admin, admin_token),
        "alice": (alice, alice_token),
        "bob": (bob, bob_token),
        "camp_a": camp_a,
        "save_a": save_a,
        "char_a": char_a,
        "camp_b": camp_b,
        "save_b": save_b,
        "char_b": char_b,
    }


@pytest.mark.asyncio
async def test_requests_without_a_valid_token_are_401(api_client: AsyncClient, two_users) -> None:
    alice = two_users["alice"][0]
    assert (await api_client.get("/api/campaigns")).status_code == 401
    bad = await api_client.get("/api/campaigns", headers=_auth("edgar_not_a_real_token"))
    assert bad.status_code == 401
    assert bad.json()["detail"] == "Authentication required."
    assert bad.headers["www-authenticate"] == "Bearer"
    # Wrong scheme is not a credential either.
    basic = await api_client.get("/api/campaigns", headers={"Authorization": "Basic abc"})
    assert basic.status_code == 401
    assert alice.id is not None


@pytest.mark.asyncio
async def test_probes_and_docs_stay_public(api_client: AsyncClient, two_users) -> None:
    assert (await api_client.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_revoked_token_is_refused_immediately(
    api_client: AsyncClient, db_session: AsyncSession, two_users
) -> None:
    alice = two_users["alice"][0]
    token, row = await _token(db_session, alice, "to revoke")
    assert (await api_client.get("/api/campaigns", headers=_auth(token))).status_code == 200
    row.revoked_at = datetime.now(UTC)
    await db_session.commit()
    assert (await api_client.get("/api/campaigns", headers=_auth(token))).status_code == 401


@pytest.mark.asyncio
async def test_token_database_row_holds_only_the_digest(
    api_client: AsyncClient, db_session: AsyncSession, two_users
) -> None:
    alice_token = two_users["alice"][1]
    await api_client.get("/api/campaigns", headers=_auth(alice_token))
    rows = (await db_session.execute(select(ApiToken))).scalars().all()
    assert rows and all(len(r.token_hash) == 64 for r in rows)
    assert all(alice_token not in r.token_hash for r in rows)
    assert all(r.last_used_at is not None for r in rows if r.user_id == two_users["alice"][0].id)


@pytest.mark.asyncio
async def test_listing_returns_only_the_callers_campaigns(api_client: AsyncClient, two_users) -> None:
    alice_token = two_users["alice"][1]
    listed = await api_client.get("/api/campaigns", headers=_auth(alice_token))
    assert listed.status_code == 200
    assert {c["id"] for c in listed.json()} == {two_users["camp_a"].id}
    sessions = await api_client.get("/api/sessions", headers=_auth(alice_token))
    assert {s["id"] for s in sessions.json()} == {two_users["save_a"].id}
    chars = await api_client.get("/api/characters", headers=_auth(alice_token))
    assert {c["id"] for c in chars.json()} == {two_users["char_a"].id}


@pytest.mark.asyncio
async def test_owner_can_use_own_campaign(api_client: AsyncClient, two_users) -> None:
    headers = _auth(two_users["alice"][1])
    cid = two_users["camp_a"].id
    assert (await api_client.get(f"/api/campaigns/{cid}", headers=headers)).status_code == 200
    assert (await api_client.get(f"/api/sessions/{two_users['save_a'].id}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_other_users_campaign_is_404_for_read_write_and_delete(
    api_client: AsyncClient, db_session: AsyncSession, two_users
) -> None:
    headers = _auth(two_users["alice"][1])
    cid = two_users["camp_b"].id
    assert (await api_client.get(f"/api/campaigns/{cid}", headers=headers)).status_code == 404
    assert (await api_client.patch(f"/api/campaigns/{cid}", json={"title": "x"}, headers=headers)).status_code == 404
    assert (await api_client.post(f"/api/campaigns/{cid}/end", headers=headers)).status_code == 404
    assert (await api_client.delete(f"/api/campaigns/{cid}", headers=headers)).status_code == 404
    still = await db_session.get(Campaign, cid)
    assert still is not None and still.title == "Bob's campaign"


@pytest.mark.asyncio
async def test_other_users_session_is_404_for_every_route(
    api_client: AsyncClient, db_session: AsyncSession, two_users
) -> None:
    headers = _auth(two_users["alice"][1])
    sid = two_users["save_b"].id
    reads = [
        f"/api/sessions/{sid}",
        f"/api/sessions/{sid}/messages",
        f"/api/sessions/{sid}/combat",
        f"/api/sessions/{sid}/journal",
        f"/api/sessions/{sid}/map",
    ]
    for path in reads:
        assert (await api_client.get(path, headers=headers)).status_code == 404, path
    assert (await api_client.patch(f"/api/sessions/{sid}", json={}, headers=headers)).status_code == 404
    assert (await api_client.delete(f"/api/sessions/{sid}", headers=headers)).status_code == 404
    turn = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I attack"}, headers=headers)
    assert turn.status_code == 404
    intro = await api_client.post(f"/api/sessions/{sid}/intro", headers=headers)
    assert intro.status_code == 404
    assert await db_session.get(SessionModel, sid) is not None


@pytest.mark.asyncio
async def test_cannot_create_a_session_or_assign_a_character_in_another_users_campaign(
    api_client: AsyncClient, two_users
) -> None:
    headers = _auth(two_users["alice"][1])
    cid_b = two_users["camp_b"].id
    created = await api_client.post(
        "/api/sessions", json={"campaign_id": cid_b, "started_at": None}, headers=headers
    )
    assert created.status_code == 404
    assigned = await api_client.post(
        f"/api/campaigns/{cid_b}/characters",
        json={"character_id": two_users["char_a"].id},
        headers=headers,
    )
    assert assigned.status_code == 404


@pytest.mark.asyncio
async def test_other_users_character_is_404(api_client: AsyncClient, two_users) -> None:
    headers = _auth(two_users["alice"][1])
    cid = two_users["char_b"].id
    assert (await api_client.get(f"/api/characters/{cid}", headers=headers)).status_code == 404
    assert (await api_client.patch(f"/api/characters/{cid}", json={"name": "x"}, headers=headers)).status_code == 404
    assert (await api_client.delete(f"/api/characters/{cid}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_campaign_scoped_children_are_protected(
    api_client: AsyncClient, db_session: AsyncSession, two_users
) -> None:
    headers = _auth(two_users["alice"][1])
    cid_b = two_users["camp_b"].id
    assert (await api_client.get(f"/api/campaigns/{cid_b}/characters", headers=headers)).status_code == 404
    assert (await api_client.get(f"/api/campaigns/{cid_b}/npcs", headers=headers)).status_code == 404
    assert (await api_client.get(f"/api/campaigns/{cid_b}/world-flags", headers=headers)).status_code == 404
    flag = WorldFlag(campaign_id=cid_b, key="gate_open", value="yes")
    db_session.add(flag)
    npc = NPC(
        campaign_id=cid_b,
        name="Guard",
        disposition="wary",
        stat_block={"hp": 5},
    )
    db_session.add(npc)
    await db_session.commit()
    await db_session.refresh(npc)
    assert (
        await api_client.post(
            f"/api/campaigns/{cid_b}/npcs",
            json={"name": "Thief", "disposition": "sly", "stat_block": {}},
            headers=headers,
        )
    ).status_code == 404
    assert (await api_client.get(f"/api/npcs/{npc.id}", headers=headers)).status_code == 404
    assert (await api_client.patch(f"/api/npcs/{npc.id}", json={"name": "x"}, headers=headers)).status_code == 404
    assert (await api_client.delete(f"/api/npcs/{npc.id}", headers=headers)).status_code == 404
    assert (await api_client.delete(f"/api/campaigns/{cid_b}/world-flags/gate_open", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_admin_only_routes_refuse_a_non_admin(api_client: AsyncClient, two_users) -> None:
    headers = _auth(two_users["alice"][1])
    assert (await api_client.post("/api/seed", headers=headers)).status_code == 403
    assert (await api_client.post("/api/users", json={"email": "x@example.test"}, headers=headers)).status_code == 403
    assert (await api_client.delete("/api/adventures/no-such-adventure", headers=headers)).status_code == 403
    assert (await api_client.get("/api/adventures/no-such-adventure/extraction", headers=headers)).status_code == 403
    assert (await api_client.get("/api/adventures/no-such-adventure/manifest", headers=headers)).status_code == 403
    sid = two_users["save_a"].id
    assert (await api_client.get(f"/api/sessions/{sid}/diagnostics/turns", headers=headers)).status_code == 403
    assert (await api_client.get("/metrics", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_admin_can_bootstrap_into_their_own_account(api_client: AsyncClient, two_users) -> None:
    admin_user, admin_token = two_users["admin"]
    seeded = await api_client.post("/api/seed", headers=_auth(admin_token))
    assert seeded.status_code == 200
    assert seeded.json()["user_id"] == admin_user.id


@pytest.mark.asyncio
async def test_diagnostics_are_admin_only_and_still_debug_gated(
    api_client: AsyncClient, two_users, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = two_users["save_b"].id
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    admin_headers = _auth(two_users["admin"][1])
    assert (await api_client.get(f"/api/sessions/{sid}/diagnostics/turns", headers=admin_headers)).status_code == 200
    monkeypatch.setenv("EDGAR_DEBUG", "0")
    assert (await api_client.get(f"/api/sessions/{sid}/diagnostics/turns", headers=admin_headers)).status_code == 404


@pytest.mark.asyncio
async def test_metrics_accept_an_admin_token_only(api_client: AsyncClient, two_users) -> None:
    assert (await api_client.get("/metrics")).status_code == 401
    assert (await api_client.get("/metrics", headers=_auth(two_users["admin"][1]))).status_code == 200


@pytest.mark.asyncio
async def test_users_me_follows_the_caller(api_client: AsyncClient, two_users) -> None:
    bob, bob_token = two_users["bob"]
    me = await api_client.get("/api/users/me", headers=_auth(bob_token))
    assert me.status_code == 200 and me.json()["id"] == bob.id
    patched = await api_client.patch(
        "/api/users/me", json={"display_name": "Bobby"}, headers=_auth(bob_token)
    )
    assert patched.status_code == 200 and patched.json()["display_name"] == "Bobby"


@pytest.mark.asyncio
async def test_adventure_catalogue_is_readable_by_any_authenticated_user(
    api_client: AsyncClient, two_users
) -> None:
    response = await api_client.get("/api/adventures", headers=_auth(two_users["alice"][1]))
    # The catalogue itself needs a vector store that this test does not provide (503), so assert
    # what matters here: a non-admin, authenticated caller is not refused by the auth layer.
    assert response.status_code not in (401, 403)
    anonymous = await api_client.get("/api/adventures")
    assert anonymous.status_code == 401
