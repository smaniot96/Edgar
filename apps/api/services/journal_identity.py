"""Opaque stable links for entries in a session's player journal."""

import base64
import hashlib


def journal_entry_id(session_id: int, category: str, stable_fact_id: str) -> str:
    """Return the same session-scoped handle used by the journal projection."""
    raw = f"edgar-journal-v1:{session_id}:{category}:{stable_fact_id}".encode()
    digest = base64.urlsafe_b64encode(hashlib.blake2s(raw, digest_size=16).digest())
    return "entry_" + digest.decode("ascii").rstrip("=")
