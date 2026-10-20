"""Archived generation schemas and a preparation compatibility entry point.

Saved generation checkpoints still use the outline schemas. All executable campaign
creation goes through the private preparation runtime.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChapterOutline(BaseModel):
    name: str
    summary: str = Field(
        description="2-3 sentences: what happens, the location, and the goal of this chapter."
    )


class CampaignOutline(BaseModel):
    title: str
    premise: str = Field(
        description="2-4 sentences setting up the whole adventure and its central conflict."
    )
    setting: str = Field(description="Where it takes place; tone and atmosphere.")
    hook: str = Field(description="How the player characters get drawn in at the very start.")
    factions: list[str] = Field(default_factory=list)
    key_npcs: list[str] = Field(default_factory=list, description="'Name — role/motivation' lines.")
    chapters: list[ChapterOutline] = Field(default_factory=list)


async def build_generated_campaign(
    slug: str,
    title_hint: str,
    theme: str,
    size: str,
    *,
    attempt_id: str | None = None,
    model_profiles=None,
) -> None:
    """Delegate saved callers to preparation without reviving archived authoring flows.

    The positional authoring arguments remain for compatibility. Preparation reads its
    frozen commission from campaign metadata and requires server-captured setup.
    """
    from .adventures_meta import load_meta
    from .preparation import PreparationTaskFailure

    meta = load_meta(slug)
    if meta.get("private_preparation") is not True or not isinstance(
        meta.get("preparation_setup"), dict
    ):
        raise PreparationTaskFailure("preparation_setup_required")
    from .preparation_runtime import prepare_adventure

    await prepare_adventure(slug, attempt_token=attempt_id, model_profiles=model_profiles)
