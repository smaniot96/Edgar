"""
Inspect Qdrant collections: list counts, sample points, test search.

Run from Edgar: uv run python -m db.vector.scripts.inspect_collections [--sample] [--search QUERY]
"""

import argparse
import sys

from db.vector import get_embeddings, get_qdrant_client
from db.vector.versions import active_index_read
from edgar_core.model_profiles import ModelProfileError, validate_provider_configuration


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect Qdrant collections: list counts, sample points, test search"
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Show sample points (first 3) from each collection",
    )
    parser.add_argument(
        "--search",
        type=str,
        metavar="QUERY",
        help="Run a test semantic search with this query across all collections",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Number of sample/search results per collection (default: 3)",
    )
    args = parser.parse_args()
    if args.search:
        try:
            validate_provider_configuration()
        except ModelProfileError as exc:
            parser.exit(1, f"Configuration error: {exc}\n")

    client = get_qdrant_client()
    collections = client.get_collections().collections

    if not collections:
        print("No collections found.")
        sys.exit(0)

    print(f"\n{'Collection':<30} {'Points':>10}")
    print("-" * 42)

    for c in collections:
        with active_index_read(client, c.name) as active_filter:
            count = client.count(
                collection_name=c.name, count_filter=active_filter, exact=True
            ).count
        print(f"{c.name:<30} {count:>10}")

    if args.sample:
        print("\n--- Sample points ---")
        for c in collections:
            with active_index_read(client, c.name) as active_filter:
                points, _ = client.scroll(
                    collection_name=c.name,
                    scroll_filter=active_filter,
                    limit=args.limit,
                    with_payload=True,
                    with_vectors=False,
                )
            print(f"\n{c.name}:")
            for i, p in enumerate(points, 1):
                text = (
                    p.payload.get("text", "")[:120] + "..."
                    if len(p.payload.get("text", "")) > 120
                    else p.payload.get("text", "")
                )
                source = p.payload.get("source", "?")
                page = p.payload.get("page", "?")
                heading = p.payload.get("heading_path")
                where = f", {heading}" if heading else ""
                print(f"  {i}. [source={source}, page={page}{where}] {text}")

    if args.search:
        print(f'\n--- Search: "{args.search}" ---')
        query_vector = get_embeddings([args.search])[0]
        for c in collections:
            # qdrant-client >= 1.15 removed `search`; `query_points` is the replacement.
            with active_index_read(client, c.name) as active_filter:
                results = client.query_points(
                    collection_name=c.name,
                    query=query_vector,
                    limit=args.limit,
                    with_payload=True,
                    query_filter=active_filter,
                ).points
            print(f"\n{c.name}:")
            for i, r in enumerate(results, 1):
                text = (
                    (r.payload.get("text", "")[:100] + "...")
                    if len(r.payload.get("text", "")) > 100
                    else r.payload.get("text", "")
                )
                source = r.payload.get("source", "?")
                page = r.payload.get("page", "?")
                heading = r.payload.get("heading_path")
                where = f", {heading}" if heading else ""
                print(f"  {i}. [source={source}, page={page}{where}] score={r.score:.4f}")
                print(f"      {text}")

    print()


if __name__ == "__main__":
    main()
