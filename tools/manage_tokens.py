"""Create, list and revoke hosted-mode API tokens (EDG-065).

Run from the repo root with the project environment::

    .venv/bin/python -m tools.manage_tokens create --user-id 1 --label laptop
    .venv/bin/python -m tools.manage_tokens list [--user-id 1]
    .venv/bin/python -m tools.manage_tokens revoke --token-id 3

``create`` prints the new token on stdout exactly once. Only its SHA-256 digest is stored, so a
lost token cannot be recovered: create a new one and revoke the old one. Nothing here logs tokens.
There is no HTTP endpoint for this; it is an operator action on the database.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from apps.api.auth import generate_token, hash_token
from db.postgres.models import ApiToken, User

LABEL_MAX = 120


def _clean_label(label: str) -> str:
    cleaned = label.strip()
    if len(cleaned) > LABEL_MAX:
        raise ValueError(f"label must be at most {LABEL_MAX} characters")
    return cleaned


async def create_token(session_factory: Any, user_id: int, label: str = "") -> str:
    """Store the digest of a fresh token for an existing user and return the plaintext once."""
    async with session_factory() as session:
        if await session.get(User, user_id) is None:
            raise ValueError(f"user {user_id} does not exist")
        token = generate_token()
        session.add(
            ApiToken(
                user_id=user_id,
                token_hash=hash_token(token),
                label=_clean_label(label),
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return token


async def list_tokens(session_factory: Any, user_id: int | None = None) -> list[dict[str, Any]]:
    """Token metadata only: never the digest and never the token."""
    stmt = select(ApiToken).order_by(ApiToken.id)
    if user_id is not None:
        stmt = stmt.where(ApiToken.user_id == user_id)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "token_id": row.id,
            "user_id": row.user_id,
            "label": row.label,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        }
        for row in rows
    ]


async def revoke_token(session_factory: Any, token_id: int) -> bool:
    """Revoke a token. Idempotent: an already revoked token keeps its original revocation time."""
    async with session_factory() as session:
        row = await session.get(ApiToken, token_id)
        if row is None:
            return False
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await session.commit()
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage_tokens", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="create a token (printed once)")
    create.add_argument("--user-id", type=int, required=True)
    create.add_argument("--label", default="")
    listing = sub.add_parser("list", help="list token metadata")
    listing.add_argument("--user-id", type=int, default=None)
    revoke = sub.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("--token-id", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from db.postgres.session import async_session_factory

    try:
        if args.command == "create":
            token = asyncio.run(create_token(async_session_factory, args.user_id, args.label))
            print(token)
            print("Store this token now. It is not stored and cannot be shown again.", file=sys.stderr)
        elif args.command == "list":
            for row in asyncio.run(list_tokens(async_session_factory, args.user_id)):
                print(row)
        else:
            if not asyncio.run(revoke_token(async_session_factory, args.token_id)):
                print("No such token.", file=sys.stderr)
                return 1
            print(f"Revoked token {args.token_id}.")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
