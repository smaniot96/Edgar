"""Exact identity for indivisible creature records produced by verified extraction.

This only identifies records; it does not interpret or implement their mechanics. A
generic heading, a creature mentioned in prose, or a mixed stat block is not an identity.
"""

from __future__ import annotations

import re
from typing import Any

from db.vector.references import normalize_reference, reference_matches
from edgar_core.artifacts import text_digest

_AC = re.compile(r"\bArmor Class\W{0,6}\d", re.IGNORECASE)
_HP = re.compile(r"\bHit Points\W{0,6}\d", re.IGNORECASE)


class CreatureResolutionError(ValueError):
    """An encounter needs an exact source record or a reviewed module actor reference."""


def creature_payload_fields(chunk: dict[str, Any]) -> dict[str, str]:
    """Stamp only a single complete, explicitly headed stat record.

    EDG-002 owns extraction/readiness and complete boundaries. These extra checks reject
    legacy mixed records and conflicting metadata; they never certify arbitrary prose.
    """
    text = chunk.get("text")
    if chunk.get("stat_block") is not True or not isinstance(text, str):
        return {}
    if len(_AC.findall(text)) != 1 or len(_HP.findall(text)) != 1:
        return {}
    section = chunk.get("section")
    if not isinstance(section, str) or not section.strip():
        return {}
    header = next((line.strip(" #*_`") for line in text.splitlines() if line.strip()), "")
    # The chunker prefixes the full heading path, retaining the leaf creature heading.
    header = header.rsplit(" > ", 1)[-1]
    if not reference_matches(header, section):
        return {}
    key = normalize_reference(section)
    if not key or any(
        chunk.get(field) is not None and not reference_matches(chunk[field], section)
        for field in ("creature_name", "creature_key")
    ):
        return {}
    return {"creature_name": section.strip(), "creature_key": key}


def identified_creature(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """Return the full record with stable edition/source identity, without truncating text."""
    fields = creature_payload_fields(chunk)
    if not fields:
        return None
    identity = {
        "collection": chunk.get("collection"),
        "source": chunk.get("source"),
        "source_sha256": chunk.get("source_sha256"),
        "extraction_sha256": chunk.get("extraction_sha256"),
        "rules_edition": chunk.get("rules_edition", "unspecified"),
        "creature_key": fields["creature_key"],
        "text_sha256": text_digest(chunk["text"]),
    }
    # Legacy and canonical collection aliases of the same source remain distinct citations.
    import json

    return {
        **chunk,
        **fields,
        "creature_id": text_digest(json.dumps(identity, sort_keys=True, separators=(",", ":"))),
        "identity_status": "resolved",
    }
