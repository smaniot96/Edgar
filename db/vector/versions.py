"""Atomic source-version routing inside existing Qdrant collections.

An unembedded control point holds all active source versions. Writers stage immutable points,
validate them, then replace this one point. Local processes coordinate with flock files in the
shared data volume; every writer and reader must share EDGAR_INDEX_LOCK_DIR. This is a single
shared-filesystem runtime contract, not a distributed lock for independent hosts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path

from qdrant_client.models import FieldCondition, Filter, HasIdCondition, MatchAny, MatchValue, PointStruct

from edgar_core.config import EDGAR_ROOT, EMBEDDING_MODEL

INDEX_STATE_ID = "04200c09-7339-5db4-9a1e-a0c4fdf6a177"


class SupersededIngestion(RuntimeError):
    """A newer accepted attempt owns this source; the obsolete attempt must stop."""


def _lock_dir() -> Path:
    default = Path("/data/.index-locks") if Path("/data").is_dir() else EDGAR_ROOT / "data/.index-locks"
    return Path(os.environ.get("EDGAR_INDEX_LOCK_DIR", default))


@contextmanager
def index_lock(collection_name: str, *, shared: bool = False):
    directory = _lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(collection_name.encode()).hexdigest()
    with (directory / f"{key}.lock").open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_index_state(client, collection_name: str) -> dict:
    # Minimal legacy test clients may implement only search. Actual Qdrant clients always
    # implement retrieve; transport errors propagate rather than exposing staged points.
    if not hasattr(client, "retrieve"):
        return {"schema": 1, "sources": {}}
    points = client.retrieve(collection_name=collection_name, ids=[INDEX_STATE_ID], with_payload=True, with_vectors=False)
    if not points:
        return {"schema": 1, "sources": {}}
    state = points[0].payload or {}
    if state.get("schema") != 1 or not isinstance(state.get("sources"), dict):
        raise ValueError("Invalid active index routing record")
    return state


def _write_state(client, collection_name: str, state: dict) -> None:
    client.upsert(collection_name=collection_name, points=[PointStruct(id=INDEX_STATE_ID, vector={}, payload=state)], wait=True)


def active_filter(state: dict, query_filter: Filter | None = None) -> Filter | None:
    sources = state["sources"]
    if not sources:
        return query_filter
    allowed = [Filter(must_not=[FieldCondition(key="source", match=MatchAny(any=list(sources)))])]
    for source, entry in sources.items():
        active = entry.get("active")
        if active:
            allowed.append(version_filter(source, active["index_version"]))
        elif entry.get("legacy"):
            # Before the first successful replacement, keep exactly the existing legacy
            # points available. Staged points always carry index_version.
            from qdrant_client.models import IsEmptyCondition, PayloadField

            allowed.append(Filter(must=[FieldCondition(key="source", match=MatchValue(value=source)), IsEmptyCondition(is_empty=PayloadField(key="index_version"))]))
    routed = Filter(should=allowed, must_not=[HasIdCondition(has_id=[INDEX_STATE_ID])])
    return Filter(must=[query_filter, routed]) if query_filter is not None else routed


@contextmanager
def active_index_read(client, collection_name: str, query_filter: Filter | None = None):
    """Hold a consistent active-only route through an entire search/scroll/count operation."""
    with index_lock(collection_name, shared=True):
        yield active_filter(read_index_state(client, collection_name), query_filter)


def version_filter(source: str, index_version: str) -> Filter:
    return Filter(must=[FieldCondition(key="source", match=MatchValue(value=source)), FieldCondition(key="index_version", match=MatchValue(value=index_version))])


def index_version(payloads: list[dict]) -> str:
    """Include full text, chunk boundaries, manifests, editions and embedding model."""
    canonical = json.dumps({"schema": 1, "embedding_model": EMBEDDING_MODEL, "payloads": payloads}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def begin_index(client, collection_name: str, source: str, version: str, *, guard: Callable | None = None) -> str:
    token = uuid.uuid4().hex
    with guard() if guard else nullcontext():
        with index_lock(collection_name):
            state = read_index_state(client, collection_name)
            entry = state["sources"].setdefault(source, {"legacy": True})
            entry["pending"] = {"token": token, "index_version": version}
            _write_state(client, collection_name, state)
    return token


def promote_index(client, collection_name: str, source: str, version: str, token: str, receipt: dict, *, validate: Callable, guard: Callable | None = None, cleanup: Callable | None = None) -> None:
    with guard() if guard else nullcontext():
        with index_lock(collection_name):
            state = read_index_state(client, collection_name)
            entry = state["sources"].get(source, {})
            if (entry.get("pending") or {}).get("token") != token:
                raise SupersededIngestion("This source ingestion was superseded by a newer attempt.")
            validate()
            entry["active"] = {**receipt, "index_version": version}
            entry.pop("pending", None)
            entry.pop("legacy", None)
            _write_state(client, collection_name, state)
            if cleanup is not None:
                cleanup()


def assert_index_attempt(client, collection_name: str, source: str, token: str) -> None:
    with index_lock(collection_name, shared=True):
        state = read_index_state(client, collection_name)
        if (state["sources"].get(source, {}).get("pending") or {}).get("token") != token:
            raise SupersededIngestion("This source ingestion was superseded by a newer attempt.")
