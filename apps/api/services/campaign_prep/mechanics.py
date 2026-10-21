"""Payload-only exact creature catalogue, reusing verified engine infrastructure."""

from __future__ import annotations

from agent.monster_stats import MonsterStatsError, parse_monster_stats
from db.vector.client import get_qdrant_client
from db.vector.creatures import identified_creature
from db.vector.versions import active_index_read


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
