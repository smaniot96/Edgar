"""Operator token CLI (tools/manage_tokens.py): digest-only storage, one-time plaintext (EDG-065)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.api.auth import hash_token
from tools import manage_tokens


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class FakeStore:
    def __init__(self, users: set[int] | None = None) -> None:
        self.users = users if users is not None else {1}
        self.tokens: list[Any] = []
        self.commits = 0

    def factory(self) -> FakeSession:
        return FakeSession(self)


class FakeSession:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get(self, model: type, pk: int) -> Any:
        if model is manage_tokens.User:
            return SimpleNamespace(id=pk) if pk in self.store.users else None
        return next((t for t in self.store.tokens if t.id == pk), None)

    def add(self, obj: Any) -> None:
        obj.id = len(self.store.tokens) + 1
        self.store.tokens.append(obj)

    async def commit(self) -> None:
        self.store.commits += 1

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(list(self.store.tokens))


@pytest.mark.asyncio
async def test_create_stores_only_the_digest_and_returns_plaintext_once() -> None:
    store = FakeStore()
    token = await manage_tokens.create_token(store.factory, 1, label="  laptop ")
    (stored,) = store.tokens
    assert stored.token_hash == hash_token(token)
    assert token not in stored.token_hash
    assert stored.label == "laptop"
    assert stored.revoked_at is None
    assert store.commits == 1


@pytest.mark.asyncio
async def test_create_refuses_unknown_user_and_long_label() -> None:
    store = FakeStore(users={1})
    with pytest.raises(ValueError, match="does not exist"):
        await manage_tokens.create_token(store.factory, 9)
    with pytest.raises(ValueError, match="at most 120"):
        await manage_tokens.create_token(store.factory, 1, label="x" * 121)
    assert store.tokens == []


@pytest.mark.asyncio
async def test_list_shows_metadata_without_digest_or_token() -> None:
    store = FakeStore()
    token = await manage_tokens.create_token(store.factory, 1, label="ops")
    rows = await manage_tokens.list_tokens(store.factory, 1)
    assert len(rows) == 1
    assert set(rows[0]) == {"token_id", "user_id", "label", "created_at", "revoked_at", "last_used_at"}
    assert token not in repr(rows)
    assert hash_token(token) not in repr(rows)


@pytest.mark.asyncio
async def test_revoke_is_idempotent_and_keeps_first_timestamp() -> None:
    store = FakeStore()
    await manage_tokens.create_token(store.factory, 1)
    assert await manage_tokens.revoke_token(store.factory, 1) is True
    first = store.tokens[0].revoked_at
    assert first is not None
    assert await manage_tokens.revoke_token(store.factory, 1) is True
    assert store.tokens[0].revoked_at == first
    assert await manage_tokens.revoke_token(store.factory, 99) is False


def test_cli_parser_shapes() -> None:
    parser = manage_tokens._parser()
    args = parser.parse_args(["create", "--user-id", "4", "--label", "phone"])
    assert (args.command, args.user_id, args.label) == ("create", 4, "phone")
    assert parser.parse_args(["revoke", "--token-id", "2"]).token_id == 2
    assert parser.parse_args(["list"]).user_id is None
    with pytest.raises(SystemExit):
        parser.parse_args(["create"])
