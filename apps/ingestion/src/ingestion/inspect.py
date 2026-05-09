"""CLI to inspect Qdrant collections."""


def main() -> None:
    # Delegate to db.vector.scripts.inspect_collections
    from db.vector.scripts.inspect_collections import main as _main

    _main()


if __name__ == "__main__":
    main()
