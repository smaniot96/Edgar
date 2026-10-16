"""Discover exact active rulebook identities without embeddings, model calls or writes."""
from db.vector.client import get_qdrant_client
from db.vector.versions import active_index_read
from edgar_core.grounding import BookBinding


def discover_book_bindings(edition: str, *, client=None) -> tuple[BookBinding, ...]:
    from .preparation_grounding import GroundingUnavailable

    qdrant = client if client is not None else get_qdrant_client()
    roles = {
        "phb": ("rules_player_handbook", "player_handbook"),
        "dmg": ("rules_dm_guide", "dm_guide"),
        "mm": ("rules_monster_manual", "monster_manual"),
    }
    result = []
    for role, collections in roles.items():
        candidates = []
        for collection in collections:
            if not qdrant.collection_exists(collection):
                continue
            state = {}
            with active_index_read(qdrant, collection, state_out=state):
                for source, entry in state.get("sources", {}).items():
                    receipt = entry.get("active") or {}
                    if receipt.get("rules_edition") != edition:
                        continue
                    try:
                        candidates.append(BookBinding(role=role, collection=collection, source=source,
                            source_sha256=receipt["source_sha256"], extraction_sha256=receipt["extraction_sha256"],
                            rules_edition=edition))
                    except (KeyError, ValueError):
                        continue
            if candidates:
                break  # Canonical collection takes precedence over its legacy alias.
        if len(candidates) != 1:
            raise GroundingUnavailable("required_books_unavailable", "An exact required rulebook version is unavailable or ambiguous.")
        result.append(candidates[0])
    return tuple(result)
