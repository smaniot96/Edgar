"""Bind prepared play to the edition and actual character commissioned for its bundle."""

from fastapi import HTTPException

from . import adventures_meta


class PreparedPlayError(HTTPException):
    """A prepared-play rejection with a stable internal readiness category."""

    def __init__(self, status_code: int, readiness_code: str, detail: str):
        super().__init__(status_code=status_code, detail=detail)
        self.readiness_code = readiness_code


def require_prepared_play(slugs, edition, *, character_id=None, character=None, require_hero=False):
    from edgar_core.grounding import HeroPartySnapshot, HeroSnapshot

    from .preparation_runtime import private_preparation_ready
    from .preparation_store import PreparationStore

    for slug in slugs or []:
        meta = adventures_meta.load_meta(slug)
        if meta.get("private_preparation") is not True:
            continue
        if not private_preparation_ready(slug, meta):
            raise PreparedPlayError(
                409, "preparation_in_progress", "This adventure is still being prepared."
            )
        try:
            binding = PreparationStore().load_run(slug, meta["preparation_run_id"]).binding
            party = HeroPartySnapshot.model_validate(binding.hero_party_snapshot)
        except (ValueError, KeyError) as exc:
            raise PreparedPlayError(
                409,
                "preparation_failed",
                "The prepared adventure binding needs attention.",
            ) from exc
        if edition != binding.rules_edition:
            raise PreparedPlayError(
                422,
                "rules_edition_mismatch",
                "Use the rules edition selected for this prepared adventure.",
            )
        identifier = character.id if character is not None else character_id
        if identifier is None and not require_hero:
            continue
        hero = next((h for h in party.heroes if h.character_id == identifier), None)
        if hero is None:
            raise PreparedPlayError(
                422,
                "choose_character",
                "Choose the hero selected for this prepared adventure.",
            )
        if character is not None:
            actual = HeroSnapshot(
                character_id=character.id,
                name=character.name,
                character_class=character.character_class,
                level=character.level,
                hp_max=character.hp_max,
                stats=character.base_stats or {},
                inventory=character.base_inventory or {},
            )
            if actual != hero:
                raise PreparedPlayError(
                    409,
                    "preparation_failed",
                    "The hero changed after preparation. Prepare a new adventure for the updated sheet.",
                )


def protect_prepared_campaign_binding(campaign, changes):
    """A saved campaign must continue to name the same immutable prepared source."""
    if not any(
        adventures_meta.load_meta(s).get("prepared_bundle")
        for s in campaign.adventure_collections or []
    ):
        return
    if any(
        key in changes and changes[key] != getattr(campaign, key)
        for key in ("adventure_collections", "rules_edition", "source_mode")
    ):
        raise HTTPException(
            status_code=409,
            detail="This campaign retains its prepared version. Start a new campaign to change its source or edition.",
        )
