"""Inert access to saved v1 records. No dispatch, scheduler, writes or conversion."""

import json
import re

from pydantic import BaseModel, Field

from edgar_core.manifests import load_published_manifest

from .. import adventures_meta


class ChapterOutline(BaseModel):
    name: str
    summary: str


class CampaignOutline(BaseModel):
    title: str
    premise: str
    setting: str
    hook: str
    factions: list[str] = Field(default_factory=list)
    key_npcs: list[str] = Field(default_factory=list)
    chapters: list[ChapterOutline] = Field(default_factory=list)


def checkpoint(slug, meta):
    run_id = meta.get("preparation_run_id", "")
    if not re.fullmatch(r"prep-[0-9a-f]{32}", run_id):
        return None
    path = adventures_meta.job_dir(slug, run_id[5:]) / "preparation.json"
    return json.loads(path.read_text()) if path.is_file() else None


def ready(slug, meta):
    """Published archived modules remain playable; unfinished work stays held forever."""
    if meta.get("status") != "ready" or not meta.get("prepared_bundle"):
        return False
    try:
        state = checkpoint(slug, meta)
        receipt = meta["preparation_publication_receipt"]
        manifest = load_published_manifest(meta)
        return bool(
            state
            and state.get("status") == "ready"
            and state.get("published_bundle_sha256")
            == receipt.get("bundle_sha256")
            == meta["prepared_bundle"].get("bundle_sha256")
            and receipt.get("run_id") == meta.get("preparation_run_id")
            and manifest.reviewed_sha256
        )
    except (KeyError, OSError, ValueError):
        return False
