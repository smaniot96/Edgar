"""CLI to inspect Qdrant collections."""
import sys
from pathlib import Path

# Ensure Edgar root on path for db/config
_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))


def main() -> None:
    # Delegate to db.vector.scripts.inspect_collections
    from db.vector.scripts.inspect_collections import main as _main

    _main()


if __name__ == "__main__":
    main()
