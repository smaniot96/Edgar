"""Source references shared by ingestion, retrieval and persisted turn diagnostics.

Hashes identify source bytes/selected text, not the edition: that is always declared.
References contain no excerpts or private titles, so public projections can remain small.
"""

import json
import re
from typing import Literal

from edgar_core.artifacts import text_digest

RulesEdition = Literal["2014", "2024"]
SourceEdition = Literal["2014", "2024", "unspecified"]
DEFAULT_RULES_EDITION: RulesEdition = "2014"


def source_citation(chunk: dict, selected_edition: str = DEFAULT_RULES_EDITION) -> dict:
    edition = chunk.get("rules_edition") or "unspecified"
    pages = chunk.get("source_pages") or [chunk.get("page")]
    ref = {
        "collection": chunk.get("collection"),
        "source": chunk.get("source", ""),
        "source_sha256": chunk.get("source_sha256"),
        "extraction_sha256": chunk.get("extraction_sha256"),
        "rules_edition": edition,
        "page": chunk.get("page"),
        "page_end": chunk.get("page_end", chunk.get("page")),
        "source_pages": sorted({p for p in pages if type(p) is int and p > 0}),
        "audience": chunk.get("audience", "player" if chunk.get("kind") == "rules" else "planner"),
    }
    for key in ("scene_id", "passage_id", "manifest_sha256", "chunk_index"):
        if chunk.get(key) is not None:
            ref[key] = chunk[key]
    # Distinguish passages sharing a page even in legacy payloads without chunk indices.
    identity = {**ref, "text_sha256": text_digest(chunk.get("text", ""))}
    ref["reference_id"] = text_digest(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    identified = bool(ref["source_pages"]) and all(
        isinstance(ref[key], str) and re.fullmatch(r"[0-9a-f]{64}", ref[key])
        for key in ("source_sha256", "extraction_sha256")
    )
    ref["edition_status"] = (
        "incompatible"
        if edition in ("2014", "2024") and edition != selected_edition
        else "compatible"
        if edition == selected_edition and identified
        else "unverified"
    )
    return ref


def citations_for(chunks: list[dict], selected_edition: str = DEFAULT_RULES_EDITION) -> list[dict]:
    """Deduplicate references without collapsing different passages from the same page."""
    refs = [source_citation(c, selected_edition) for c in chunks]
    return list({ref["reference_id"]: ref for ref in refs}.values())


def cited_excerpt(chunk: dict, selected_edition: str = DEFAULT_RULES_EDITION) -> str:
    ref = source_citation(chunk, selected_edition)
    pages = ",".join(map(str, ref["source_pages"])) or "unknown"
    return (
        f"[{ref['reference_id']}] source={ref['source']}; collection={ref['collection']}; "
        f"edition={ref['rules_edition']} ({ref['edition_status']}); "
        f"source_sha256={ref['source_sha256']}; extraction_sha256={ref['extraction_sha256']}; "
        f"physical pages={pages}\n{chunk.get('text', '')}"
    )


def public_citations(refs: list[dict]) -> list[dict]:
    """Private/planner/excluded references stay in diagnostics, never the player export."""
    return [
        {k: v for k, v in ref.items() if k not in ("scene_id", "passage_id")}
        for ref in refs
        if ref.get("audience") == "player"
    ]


def eligible_context(
    chunks: list[dict], selected_edition: str = DEFAULT_RULES_EDITION
) -> list[dict]:
    """Never supply a known conflicting edition as grounding; legacy evidence is unverified."""
    return [
        c
        for c in chunks
        if source_citation(c, selected_edition)["edition_status"] != "incompatible"
    ]
