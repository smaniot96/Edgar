"""Character preset endpoint — Phase 2."""

from fastapi import APIRouter

from ..services.character_presets import CHARACTER_PRESETS

router = APIRouter(tags=["characters"])


@router.get("/character-presets")
def list_character_presets() -> list[dict]:
    """Return the five starter class presets for the new-adventure wizard."""
    return CHARACTER_PRESETS
