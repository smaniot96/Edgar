"""Token resolution, bearer parsing, admin and ownership helpers (EDG-065).

Pure: a fake session stands in for the database. The DB-backed behaviour is in tests/api.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from apps.api import auth
from apps.api.auth import (
    Principal,
    bearer_token,
    hash_token,
    owned_campaign,
    principal_for,
    require_admin,
    require_authentication,
    resolve_token,
)

PLAINTEXT = "edgar_test_plaintext_value_0123456789abcdefghijklmnopqrstuvwxyz"


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class FakeDB:
    """Looks rows up by the 64-character digest found in the compiled statement's parameters."""

    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.statements: list[Any] = []
        self.commits = 0

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        params = stmt.compile().params
        digest = next((v for v in params.values() if isinstance(v, str) and len(v) == 64), None)
        return _Result(self.rows.get(digest))

    async def commit(self) -> None:
        self.commits += 1


def _row(token: str = PLAINTEXT, **overrides: Any) -> SimpleNamespace:
    base = {
        "id": 3,
        "user_id": 11,
        "token_hash": hash_token(token),
        "revoked_at": None,
        "last_used_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _request(authorization: str | None) -> Request:
    headers = [(b"authorization", authorization.encode())] if authorization is not None else []
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/", "query_string": b""})


@pytest.mark.asyncio
async def test_valid_token_resolves_to_its_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", "99")
    db = FakeDB({hash_token(PLAINTEXT): _row()})
    principal = await resolve_token(db, PLAINTEXT)
    assert principal == Principal(user_id=11, token_id=3, is_admin=False)


@pytest.mark.asyncio
async def test_admin_flag_comes_from_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", "11")
    principal = await resolve_token(FakeDB({hash_token(PLAINTEXT): _row()}), PLAINTEXT)
    assert principal.is_admin is True


@pytest.mark.asyncio
async def test_unknown_token_is_generic_401() -> None:
    with pytest.raises(HTTPException) as exc:
        await resolve_token(FakeDB(), PLAINTEXT)
    assert exc.value.status_code == 401
    assert exc.value.detail == "Authentication required."
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.asyncio
async def test_revoked_token_is_rejected_like_an_unknown_one() -> None:
    revoked = FakeDB({hash_token(PLAINTEXT): _row(revoked_at=datetime.now(UTC))})
    with pytest.raises(HTTPException) as exc:
        await resolve_token(revoked, PLAINTEXT)
    unknown = FakeDB()
    with pytest.raises(HTTPException) as exc2:
        await resolve_token(unknown, PLAINTEXT)
    assert (exc.value.status_code, exc.value.detail) == (exc2.value.status_code, exc2.value.detail)


@pytest.mark.asyncio
async def test_plaintext_token_never_reaches_the_database() -> None:
    db = FakeDB({hash_token(PLAINTEXT): _row()})
    await resolve_token(db, PLAINTEXT)
    for stmt in db.statements:
        compiled = stmt.compile()
        assert PLAINTEXT not in str(compiled)
        assert PLAINTEXT not in [str(v) for v in compiled.params.values()]


@pytest.mark.asyncio
async def test_last_used_is_written_at_most_once_per_interval() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    row = _row(last_used_at=None)
    db = FakeDB({hash_token(PLAINTEXT): row})
    await resolve_token(db, PLAINTEXT, now=now)
    assert row.last_used_at == now and db.commits == 1
    await resolve_token(db, PLAINTEXT, now=now + timedelta(minutes=1))
    assert db.commits == 1
    await resolve_token(db, PLAINTEXT, now=now + auth.LAST_USED_WRITE_INTERVAL)
    assert db.commits == 2


def test_bearer_parsing() -> None:
    assert bearer_token(_request(None)) is None
    assert bearer_token(_request("Basic abc")) is None
    assert bearer_token(_request("Bearer ")) is None
    assert bearer_token(_request("bearer  tok123 ")) == "tok123"


@pytest.mark.asyncio
async def test_local_mode_guard_does_not_touch_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDGAR_AUTH_MODE", raising=False)

    class ExplodingDB:
        async def execute(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("local mode must not query tokens")

    assert await require_authentication(_request(None), ExplodingDB()) is None


@pytest.mark.asyncio
async def test_token_mode_guard_requires_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", "1")
    with pytest.raises(HTTPException) as exc:
        await require_authentication(_request(None), FakeDB())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_token_mode_guard_records_principal_for_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
    monkeypatch.setenv("EDGAR_ADMIN_USER_IDS", "1")
    request = _request(f"Bearer {PLAINTEXT}")
    principal = await require_authentication(request, FakeDB({hash_token(PLAINTEXT): _row()}))
    assert principal is not None and principal.user_id == 11
    assert principal_for(request) == principal


@pytest.mark.asyncio
async def test_admin_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EDGAR_AUTH_MODE", raising=False)
    assert await require_admin(None) is None
    monkeypatch.setenv("EDGAR_AUTH_MODE", "token")
    await require_admin(Principal(user_id=1, token_id=1, is_admin=True))
    with pytest.raises(HTTPException) as exc:
        await require_admin(Principal(user_id=2, token_id=2, is_admin=False))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_ownership_lookup_is_scoped_only_in_token_mode() -> None:
    db = FakeDB()
    with pytest.raises(HTTPException) as exc:
        await owned_campaign(db, 5, Principal(user_id=2, token_id=1, is_admin=False))
    assert exc.value.status_code == 404
    assert exc.value.detail == "Campaign not found"
    assert "AND campaigns.created_by = " in str(db.statements[-1].compile())

    with pytest.raises(HTTPException):
        await owned_campaign(db, 5, None)
    assert "created_by = " not in str(db.statements[-1].compile())
