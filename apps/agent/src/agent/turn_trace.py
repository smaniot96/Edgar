"""Developer trace of one DM turn, kept across reload (EDG-138).

The turn runner builds this only when the request opted into debug AND developer debug is enabled
(`EDGAR_DEBUG`). It is staged in `telemetry_events` as one `turn_trace` diagnostic row, so it
commits or rolls back with the turn (`persist_turn`) and a retry of the same turn cannot write it
twice. The builder never opens a connection and never calls a provider.

The row is a whitelist built here field by field, never a dump of graph state:
  * parsed intent without the reviewed selectors (`resolution_policy_id`, `recall_fact_id`);
  * retrieval evidence as citations (source, edition, page, hashes). Snippet text is kept only for
    player-audience chunks, bounded in count and width. Planner-only adventure text is never kept;
  * model profile names with their model, reasoning effort and API, never secrets or endpoints;
  * routing (path, delivery, rules edition, source mode).

It REFERENCES, rather than copies, the EDG-053 `turn_diagnostics` and EDG-031 `combat_round` rows:
both carry the same `turn_id`, and the read path composes them with this row (see
`apps/api/services/turn_traces.py`). Stage timings, usage and dice are therefore never duplicated.
A turn without a `turn_id` cannot be linked on reload, so no trace is written for it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from edgar_core.sources import source_citation

TURN_TRACE_EVENT = "turn_trace"
TRACE_VERSION = 1

# Bounds shared with the turn runner's developer `debug` frame (`_truncate_ctx` defaults).
TRACE_MAX_CHUNKS = 8
TRACE_SNIPPET_WIDTH = 320
_MAX_ENTITIES = 8
_MAX_ENTITY_TEXT = 80
_MAX_SOURCE_TEXT = 200

# Parsed-intent fields a developer needs to confirm what the turn was understood as. Anything else
# the parser produces (including the reviewed selectors) stays out of the stored row.
_PARSED_FIELDS = (
    "intent",
    "check",
    "difficulty",
    "target",
    "retrieval_query",
    "uses_item",
    "lethality",
    "confirms_ending",
)
_CITATION_FIELDS = (
    "reference_id",
    "collection",
    "source",
    "source_sha256",
    "extraction_sha256",
    "manifest_sha256",
    "rules_edition",
    "edition_status",
    "page",
    "page_end",
    "source_pages",
    "chunk_index",
    "audience",
)
_PROFILE_FIELDS = ("model", "reasoning_effort", "api")
_PUBLIC_KEYS = (
    "version",
    "scope",
    "turn_id",
    "delivery",
    "path",
    "parsed_input",
    "rag",
    "settings",
    "composed_with",
)
COMPOSED_WITH = ("turn_diagnostics", "combat_round")


def _text(value: Any, width: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:width] if text else None


def _scalar(value: Any) -> str | int | float | bool | None:
    """Plain scalars only: nested planner structures are never stored."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        return value[:_MAX_ENTITY_TEXT]
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def parsed_intent_evidence(parsed: Any) -> dict[str, Any] | None:
    """The parsed intent as the developer saw it, without reviewed selector IDs."""
    if parsed is None:
        return None
    data = parsed if isinstance(parsed, Mapping) else parsed.model_dump()
    out: dict[str, Any] = {}
    for key in _PARSED_FIELDS:
        value = data.get(key)
        if key in ("intent", "check", "difficulty", "target", "retrieval_query", "uses_item"):
            out[key] = _text(value, 200)
        elif key == "lethality":
            out[key] = value if value in ("lethal", "nonlethal") else None
        elif key == "confirms_ending":
            out[key] = bool(value)
    entities = data.get("entities") if isinstance(data.get("entities"), Mapping) else {}
    # Nested structures are planner detail, not intent: only plain scalars are kept.
    scalars = [
        (key, value)
        for key, value in entities.items()
        if isinstance(key, str) and not isinstance(value, Mapping | list | tuple | set)
    ]
    out["entities"] = {
        key[:40]: _scalar(value) for key, value in scalars[:_MAX_ENTITIES] if key.strip()
    }
    return out


def _citation_evidence(chunk: Mapping[str, Any], selected_edition: str) -> dict[str, Any]:
    ref = source_citation(dict(chunk), selected_edition)
    out = {k: ref.get(k) for k in _CITATION_FIELDS}
    out["source"] = _text(out.get("source"), _MAX_SOURCE_TEXT)
    out["source_pages"] = [p for p in (ref.get("source_pages") or []) if type(p) is int][:32]
    return out


def retrieved_evidence(
    chunks: Sequence[Mapping[str, Any]] | None,
    *,
    selected_edition: str,
    max_chunks: int = TRACE_MAX_CHUNKS,
    width: int = TRACE_SNIPPET_WIDTH,
) -> dict[str, Any]:
    """The retrieved chunks as citations, with snippet text only where the audience allows it."""
    items = list(chunks or [])
    kept: list[dict[str, Any]] = []
    for chunk in items[:max_chunks]:
        citation = _citation_evidence(chunk, selected_edition)
        player_visible = citation.get("audience") == "player"
        entry: dict[str, Any] = {
            "collection": _text(chunk.get("collection"), 120),
            "source": citation["source"],
            "page": _int_or_none(chunk.get("page")),
            "score": _number(chunk.get("score")),
            "citation": citation,
            "text_withheld": not player_visible,
        }
        if player_visible:
            entry["text"] = str(chunk.get("text") or "")[:width]
        kept.append(entry)
    return {
        "count": len(items),
        "chunks": kept,
        "chunks_dropped": max(0, len(items) - len(kept)),
    }


def profile_settings(model_profiles: Mapping[str, Any] | None) -> dict[str, Any]:
    """Profile names with their model, effort and API: the settings a developer compares."""
    profiles = (model_profiles or {}).get("profiles") or {}
    return {
        "profile_version": _int_or_none((model_profiles or {}).get("profile_version")),
        "profiles": {
            str(name): {k: _scalar(item.get(k)) for k in _PROFILE_FIELDS}
            for name, item in sorted(profiles.items())
            if isinstance(item, Mapping)
        },
    }


def turn_trace_event(
    *,
    turn_id: str | None,
    delivery: str,
    path: str,
    parsed_input: Any,
    rules_context: Sequence[Mapping[str, Any]] | None,
    adventure_context: Sequence[Mapping[str, Any]] | None,
    rules_edition: str,
    source_mode: str,
    narrator_profile: str,
    model_profiles: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The `{event_type, payload}` row for this turn, or None when it cannot be linked (no turn_id)."""
    if not turn_id:
        return None
    settings = {
        "rules_edition": rules_edition,
        "source_mode": source_mode,
        "narrator_profile": narrator_profile,
        **profile_settings(model_profiles),
    }
    return {
        "event_type": TURN_TRACE_EVENT,
        "payload": {
            "version": TRACE_VERSION,
            "scope": "dm_turn",
            "turn_id": str(turn_id)[:100],
            "delivery": delivery,
            "path": path,
            "parsed_input": parsed_intent_evidence(parsed_input),
            "rag": {
                "rules": retrieved_evidence(rules_context, selected_edition=rules_edition),
                "adventure": retrieved_evidence(
                    adventure_context, selected_edition=rules_edition
                ),
            },
            "settings": settings,
            "composed_with": list(COMPOSED_WITH),
        },
    }


def turn_trace_public(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Developer view of a stored row, rebuilt key by key so a new stored field cannot leak."""
    if not payload or payload.get("version") != TRACE_VERSION:
        return None
    out = {k: payload.get(k) for k in _PUBLIC_KEYS}
    rag = payload.get("rag") or {}
    out["rag"] = {
        side: {
            "count": _int_or_none((rag.get(side) or {}).get("count")) or 0,
            "chunks": list((rag.get(side) or {}).get("chunks") or [])[:TRACE_MAX_CHUNKS],
            "chunks_dropped": _int_or_none((rag.get(side) or {}).get("chunks_dropped")) or 0,
        }
        for side in ("rules", "adventure")
    }
    return out
