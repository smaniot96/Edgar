"""HTTP boundaries for privately prepared sources.

The DM reads retained sources through internal tools. Player reference endpoints must
never return the manuscript, definition inventory, or review findings for these sources.
"""

from fastapi import HTTPException

from . import adventures_meta as store


def is_private_preparation(meta: dict) -> bool:
    return meta.get("private_preparation") is True


def require_public_source(slug: str) -> None:
    if is_private_preparation(store.load_meta(slug)):
        # Unknown and private source details have the same response.
        raise HTTPException(status_code=404, detail="Not found")


def require_mutable_source(meta: dict) -> None:
    if is_private_preparation(meta) and meta.get("prepared_bundle"):
        raise HTTPException(
            status_code=409,
            detail="This prepared version is retained for its saved games. Create a new adventure version to change it.",
        )
