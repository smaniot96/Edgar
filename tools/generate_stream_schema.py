"""Write, or check, the committed turn stream contract (EDG-058).

    .venv/bin/python -m tools.generate_stream_schema          # rewrite the file
    .venv/bin/python -m tools.generate_stream_schema --check  # exit 1 when it is stale

The source of truth is apps/api/schemas/stream.py. The output feeds the frontend contract test.
"""

from __future__ import annotations

import sys
from pathlib import Path

from apps.api.schemas.stream import render_stream_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = REPO_ROOT / "frontend" / "src" / "api" / "stream.schema.json"


def main(argv: list[str]) -> int:
    text = render_stream_schema()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale; run: .venv/bin/python -m tools.generate_stream_schema")
            return 1
        print(f"{TARGET} is current")
        return 0
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
