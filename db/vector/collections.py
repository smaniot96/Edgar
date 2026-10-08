"""Qdrant collection names: rules (core books) vs adventure modules.

Rules PDFs should be ingested with --collection rules_<name>, e.g. rules_player_handbook.
Legacy names (player_handbook, etc.) are kept for reference when migrating existing Qdrant data.

`existing_collection_names` caches the live collection list briefly so retrieval only queries
collections that actually exist (the legacy aliases are usually absent) without paying a
`get_collections` round trip on every turn.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from typing import Any

logger = logging.getLogger(__name__)

# Canonical rules collections (ingest into these names).
RULES_PLAYER_HANDBOOK = "rules_player_handbook"
RULES_DM_GUIDE = "rules_dm_guide"
RULES_MONSTER_MANUAL = "rules_monster_manual"

RULES_COLLECTION_NAMES: tuple[str, ...] = (
    RULES_PLAYER_HANDBOOK,
    RULES_DM_GUIDE,
    RULES_MONSTER_MANUAL,
)

# If you have not re-ingested yet, search these as well (same PDFs, old collection names).
LEGACY_RULES_COLLECTION_ALIASES: tuple[str, ...] = (
    "player_handbook",
    "dm_guide",
    "monster_manual",
)

# Collections holding monster stat blocks (canonical first, then legacy alias).
MONSTER_MANUAL_COLLECTIONS: tuple[str, ...] = (RULES_MONSTER_MANUAL, "monster_manual")

# Default adventure module when campaign.adventure_collections is empty (solo Chalice).
DEFAULT_ADVENTURE_COLLECTIONS: tuple[str, ...] = ("chalice_of_the_mountain_god",)

# How long a fetched collection list is trusted before re-asking Qdrant.
COLLECTION_CACHE_TTL_SEC = 30.0


def campaign_lore_collection(campaign_id: int) -> str:
    """Qdrant collection name for campaign-specific DM notes (per-campaign RAG)."""
    return f"campaign_lore_{campaign_id}"


def is_rules_collection(name: str) -> bool:
    """True if the Qdrant collection name is a rules book (not an adventure module)."""
    return name in set(RULES_COLLECTION_NAMES) or name in set(LEGACY_RULES_COLLECTION_ALIASES)


def is_campaign_lore_collection(name: str) -> bool:
    """True if the Qdrant collection name is campaign-specific lore."""
    return name.startswith("campaign_lore_")


def effective_rules_collection_names(include_legacy: bool = True) -> list[str]:
    """Return candidate Qdrant collection names for rules RAG.

    Candidates only: retrieval intersects them with `existing_collection_names` so absent
    legacy aliases are never queried.
    """
    names = list(RULES_COLLECTION_NAMES)
    if include_legacy:
        names.extend(LEGACY_RULES_COLLECTION_ALIASES)
    return names


def effective_adventure_collection_names(adventure_collections: list[str] | None) -> list[str]:
    """Return adventure module collection names; default to Chalice if none configured."""
    if adventure_collections:
        return list(adventure_collections)
    return list(DEFAULT_ADVENTURE_COLLECTIONS)


# --- live collection list cache -------------------------------------------------------------

_cache_lock = threading.Lock()
# Keyed weakly by client object so a test's fake client never leaks into another test.
_weak_cache: weakref.WeakKeyDictionary[Any, tuple[float, frozenset[str]]] = weakref.WeakKeyDictionary()


def existing_collection_names(client: Any, *, max_age: float = COLLECTION_CACHE_TTL_SEC) -> frozenset[str] | None:
    """Names of collections that exist on `client`, cached for `max_age` seconds.

    Returns None when the list cannot be fetched (client without `get_collections`, or Qdrant
    error); callers should then fall back to querying the requested names directly so a
    transient listing failure does not blank out retrieval.
    """
    now = time.monotonic()
    with _cache_lock:
        try:
            entry = _weak_cache.get(client)
        except TypeError:  # unhashable / not weak-referenceable client
            entry = None
        if entry is not None and now - entry[0] < max_age:
            return entry[1]

    getter = getattr(client, "get_collections", None)
    if getter is None:
        return None
    try:
        names = frozenset(c.name for c in getter().collections)
    except Exception as e:
        logger.warning("qdrant get_collections failed (%s: %s); querying requested collections directly", type(e).__name__, e)
        return None

    with _cache_lock:
        try:
            _weak_cache[client] = (now, names)
        except TypeError:
            pass
    return names


def invalidate_collection_cache(client: Any | None = None) -> None:
    """Forget cached collection lists (all clients, or just `client`). Call after creating one."""
    with _cache_lock:
        if client is None:
            _weak_cache.clear()
        else:
            try:
                _weak_cache.pop(client, None)
            except TypeError:
                pass
