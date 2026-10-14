"""
Create a Qdrant collection with the given name.
Run from Edgar: uv run python db/vector/scripts/new_collection.py "dnd_rules_phb"
"""
import sys

from db.vector import VECTOR_SIZE, ensure_collection, get_qdrant_client


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run scripts/new_collection.py <collection_name>")
        print("Example: uv run scripts/new_collection.py dnd_rules_phb")
        sys.exit(1)

    name = sys.argv[1].strip()
    if not name:
        print("Error: collection name cannot be empty.")
        sys.exit(1)

    client = get_qdrant_client()
    if not ensure_collection(client, name):
        print(f"Collection '{name}' already exists. Skipping.")
        return
    print(f"Created collection '{name}' (vector_size={VECTOR_SIZE}, distance=COSINE).")


if __name__ == "__main__":
    main()
