"""Payload-only exact creature catalogue, reusing verified engine infrastructure."""

from __future__ import annotations

from agent.monster_stats import MonsterStatsError, parse_monster_stats
from db.vector.client import get_qdrant_client
from db.vector.creatures import identified_creature
from db.vector.versions import active_index_read
from edgar_core.artifacts import text_digest


def source_profile(text, *, name, source_evidence=None):
    """Validate one source profile without substituting a catalogue creature.

    This is a structural/parser check, not a claim that a model's transcription has
    been visually verified. Original page evidence stays attached for the independent
    source review. Runtime resolves this same text through a module StatReference and
    the same conservative parser, so preparation cannot promise unimplemented traits.
    """
    record = identified_creature({"text": text, "section": name, "stat_block": True})
    if record is None:
        raise ValueError(
            f"Source profile {name!r} must contain one complete named stat block, "
            "with exactly one Armor Class and Hit Points field."
        )
    stats = parse_monster_stats(text, name=name)
    if len(stats.abilities) != 6 or stats.creature_type is None:
        raise ValueError(
            f"Source profile {name!r} needs its size/type and all six ability modifiers; "
            "keep the original evidence and correct the transcription rather than inventing defaults."
        )
    if stats.ac <= 0 or stats.hp_max <= 0:
        raise ValueError(f"Source profile {name!r} has nonpositive AC or HP.")
    return {
        "name": name,
        "text": text,
        "text_sha256": text_digest(text),
        "monster_stats": stats.to_dict(),
        "source_evidence": source_evidence or {},
        "capability_findings": list(stats.declared),
    }


def catalogue(edition):
    client = get_qdrant_client()
    result = []
    for collection in ("rules_monster_manual",):
        if not client.collection_exists(collection):
            continue
        with active_index_read(client, collection) as query_filter:
            offset = None
            while True:
                points, offset = client.scroll(
                    collection,
                    scroll_filter=query_filter,
                    offset=offset,
                    limit=256,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = {**(point.payload or {}), "collection": collection}
                    if payload.get("rules_edition") != edition:
                        continue
                    record = identified_creature(payload)
                    if not record or not record.get("source_sha256"):
                        continue
                    try:
                        stats = parse_monster_stats(record["text"], name=record["creature_name"])
                    except (ValueError, MonsterStatsError):
                        continue
                    if stats is None:
                        continue
                    result.append(
                        {
                            "name": record["creature_name"],
                            "collection": collection,
                            "source_sha256": record["source_sha256"],
                            "extraction_sha256": record["extraction_sha256"],
                            "rules_edition": edition,
                            "page": record.get("page"),
                            "text_sha256": record["text_sha256"],
                            "text": record["text"],
                        }
                    )
                if offset is None:
                    break
    return sorted(result, key=lambda r: r["name"])
