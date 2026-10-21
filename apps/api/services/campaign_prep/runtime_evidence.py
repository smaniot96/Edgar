"""Private operator receipts for replaying rejected source answers without new calls.

These files are neither adventure assets nor metadata and have no HTTP reader.
Raw model proposals and private source passages never enter a player projection.
"""

from __future__ import annotations

import hashlib
import json

from ..adventures_meta import adventure_path
from .store import atomic_json


def retain_observation_rejection(state, adjudication, evidence):
    collections = {chunk.get("collection") for chunk in evidence.values()}
    if len(collections) != 1:
        return
    collection = collections.pop()
    proposals = [proposal.model_dump() for proposal in adjudication.source_observations]
    identity = hashlib.sha256(
        json.dumps(
            {"session": state["session_id"], "turn": state["turn_id"], "proposals": proposals},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    directory = adventure_path(collection, "preparation-v2", "runtime-evidence")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    payload = {
        "version": 1,
        "purpose": "private rejected-answer replay; not a committed game turn",
        "session_id": state["session_id"],
        "turn_id": state["turn_id"],
        "state": {
            key: state.get(key)
            for key in (
                "source_mode",
                "manifest_status",
                "turn_id",
                "player_input",
                "adventure_manifests",
                "current_scene_id",
                "rules_edition",
                "world_flags",
                "adventure_context",
                "planner_context",
                "story_state",
                "parser_scene_context",
            )
        },
        "ruling": adjudication.model_dump(),
        "source_observations": proposals,
        "eligible_sources": evidence,
    }
    path = directory / f"{identity}.json"
    if not path.exists():
        atomic_json(path, payload)
        path.chmod(0o600)
    return path
