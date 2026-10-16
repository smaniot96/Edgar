"""Opaque stable links for entries in a session's player journal."""

import base64
import hashlib
import json
import re

from edgar_core.story import StoryRef


def journal_entry_id(session_id: int, category: str, stable_fact_id: str) -> str:
    """Return the same session-scoped handle used by the journal projection."""
    raw = f"edgar-journal-v1:{session_id}:{category}:{stable_fact_id}".encode()
    digest = base64.urlsafe_b64encode(hashlib.blake2s(raw, digest_size=16).digest())
    return "entry_" + digest.decode("ascii").rstrip("=")



def actor_identity_id(session_id: int, actor: StoryRef) -> str:
    """Return an opaque, session-scoped person identity stable across source revisions.

    A canonical actor keeps the same reviewed entity id as source content and manifest
    versions advance. Source authority remains checked on every fact before projection.
    """
    identity = json.dumps(
        {
            "session_id": session_id,
            "collection": actor.collection,
            "kind": actor.kind,
            "canonical_id": actor.canonical_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "actor_" + hashlib.blake2s(identity, digest_size=16).hexdigest()


def actor_entry_id(session_id: int, actor: StoryRef) -> str:
    """Return the stable journal link for a canonical actor in this session."""
    return journal_entry_id(session_id, "person", actor_identity_id(session_id, actor))


def earned_actor_label(display_name: str, earned_text: str | None) -> str:
    """Return the longest earned prefix of a private prepared name.

    The full name is shown only when it appears as a complete phrase in player-audience
    text. If only an alias is present, expose only that literal prefix; never reconstruct a
    hidden surname from the prepared definition.
    """
    text = earned_text or ""
    words = display_name.split()
    for end in range(len(words), 0, -1):
        candidate = " ".join(words[:end])
        if re.search(rf"(?<!\w){re.escape(candidate)}(?!\w)", text, flags=re.IGNORECASE):
            return candidate
    return ""
