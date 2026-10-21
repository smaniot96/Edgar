"""Capture character-independent module preferences before dispatching DM work."""

from ..schemas.preparation_setup import PreparationSetup


async def capture_setup(setup: PreparationSetup | None) -> dict | None:
    if setup is None:
        return None
    # Legacy clients may still send character IDs. They are compatibility metadata,
    # never a preparation prerequisite or a source of authored campaign content.
    request = setup.model_dump(exclude={"character_ids"})
    return {"request": request, "heroes": []}
