"""Character preset endpoint — Phase 2."""

from fastapi import APIRouter, Query

from ..services.character_presets import presets_at_level

router = APIRouter(tags=["characters"])


@router.get("/character-presets")
def list_character_presets(level: int = Query(1, ge=1, le=5)) -> list[dict]:
    """Return coherent supported kits at a chosen starting level."""
    return presets_at_level(level)
