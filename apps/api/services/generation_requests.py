"""Retry-safe request identity for AI campaign generation (EDG-157).

The identity is stored in the adventure's own private metadata (`generation.request_id` and
`generation.request_hash`), because adventures are file-backed rather than database rows. Every
identified submission is checked and reserved under one process-spanning flock, so concurrent
duplicates yield one module. Requests without an identity keep the original, unlocked path.

A replay returns the stored slug without reserving a module or dispatching preparation. Reusing
an identity with a different body is a safe 409 `validation_failed`, never a second module.
"""

import fcntl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from . import adventures_meta as store
from .setup_requests import creation_identity

_LOCK_NAME = "generation-requests.lock"

Identity = tuple[str, str]


def identity_for(body: BaseModel) -> Identity | None:
    """Return (request_id, body digest) when the client supplied an identity."""
    identity = creation_identity(body)
    key = identity["creation_request_id"]
    if key is None:
        return None
    return key, str(identity["creation_request_hash"])


def stamp(meta: dict[str, Any], identity: Identity | None) -> dict[str, Any]:
    """Record the identity inside the generation block of a new reservation."""
    if identity is not None:
        meta["generation"] = {
            **(meta.get("generation") or {}),
            "request_id": identity[0],
            "request_hash": identity[1],
        }
    return meta


@contextmanager
def _identity_lock() -> Iterator[None]:
    store.ADVENTURES_DIR.mkdir(parents=True, exist_ok=True)
    with (store.ADVENTURES_DIR / _LOCK_NAME).open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _find(identity: Identity) -> tuple[str, dict[str, Any]] | None:
    """Caller must hold the identity lock. Raises 409 when the identity holds another body."""
    key, digest = identity
    for slug in sorted(store.metadata_slugs()):
        meta = store.load_meta(slug)
        generation = meta.get("generation") or {}
        if generation.get("request_id") != key:
            continue
        if generation.get("request_hash") != digest:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "validation_failed",
                    "message": "This generation request was already used with different details. Retry with the same details or start a new campaign.",
                },
            )
        return slug, meta
    return None


def find_existing(identity: Identity) -> tuple[str, dict[str, Any]] | None:
    """Read-only replay check, run before any setup is captured. Blocking."""
    with _identity_lock():
        return _find(identity)


def claim(
    identity: Identity | None,
    desired: str,
    meta: dict[str, Any],
    reserve: Callable[[str, dict[str, Any]], str],
) -> tuple[str, dict[str, Any], bool]:
    """Reserve a new module, or return the module an identical earlier request already reserved.

    Returns (slug, stored metadata, created). `created` is False for a replay, and the caller must
    then not dispatch generation. Blocking.
    """
    if identity is None:
        return reserve(desired, meta), meta, True
    with _identity_lock():
        found = _find(identity)
        if found is not None:
            return found[0], found[1], False
        stamped = stamp(meta, identity)
        return reserve(desired, stamped), stamped, True
