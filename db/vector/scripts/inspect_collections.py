"""
Inspect Qdrant collections: list counts, sample points, test search.

Run from Edgar: uv run python -m db.vector.scripts.inspect_collections [--sample] [--search QUERY]
"""
import argparse
import sys

from db.vector import get_qdrant_client, get_embeddings


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

    client = get_qdrant_client()
    collections = client.get_collections().collections

    if not collections:
        print("No collections found.")
        sys.exit(0)

    print(f"\n{'Collection':<30} {'Points':>10}")
    print("-" * 42)

    for c in collections:
        info = client.get_collection(c.name)
        print(f"{c.name:<30} {info.points_count:>10}")

    if args.sample:
        print("\n--- Sample points ---")
        for c in collections:
            info = client.get_collection(c.name)
            if info.points_count == 0:
                print(f"\n{c.name}: (empty)")
                continue
            points, _ = client.scroll(
                collection_name=c.name,
                limit=args.limit,
                with_payload=True,
                with_vectors=False,
            )
            print(f"\n{c.name}:")
            for i, p in enumerate(points, 1):
                text = p.payload.get("text", "")[:120] + "..." if len(p.payload.get("text", "")) > 120 else p.payload.get("text", "")
                source = p.payload.get("source", "?")
                page = p.payload.get("page", "?")
                print(f"  {i}. [source={source}, page={page}] {text}")

    if args.search:
        print(f"\n--- Search: \"{args.search}\" ---")
        query_vector = get_embeddings([args.search])[0]
        for c in collections:
            results = client.search(
                collection_name=c.name,
                query_vector=query_vector,
                limit=args.limit,
                with_payload=True,
            )
            print(f"\n{c.name}:")
            for i, r in enumerate(results, 1):
                text = (r.payload.get("text", "")[:100] + "...") if len(r.payload.get("text", "")) > 100 else r.payload.get("text", "")
                source = r.payload.get("source", "?")
                page = r.payload.get("page", "?")
                print(f"  {i}. [source={source}, page={page}] score={r.score:.4f}")
                print(f"      {text}")

    print()


if __name__ == "__main__":
    main()
