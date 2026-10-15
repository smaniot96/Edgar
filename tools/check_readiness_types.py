"""Run positive and negative MyPy checks for the ingestion readiness API."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INGESTION = ROOT / "apps" / "ingestion"


def run_mypy(fixture: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["MYPYPATH"] = "../../core/src:src"
    return subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "mypy",
            "src/ingestion/readiness.py",
            fixture,
        ],
        cwd=INGESTION,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def main() -> int:
    valid = run_mypy("tests/typecheck/readiness_valid.py")
    if valid.returncode:
        sys.stderr.write(valid.stdout + valid.stderr)
        return valid.returncode

    invalid = run_mypy("tests/typecheck/readiness_invalid.py")
    diagnostics = invalid.stdout + invalid.stderr
    expected = (
        'Argument "source_kind"',
        'Value of "source_sha256"',
    )
    missing = [message for message in expected if message not in diagnostics]
    if invalid.returncode == 0 or missing:
        sys.stderr.write(diagnostics)
        if invalid.returncode == 0:
            sys.stderr.write("Invalid readiness types unexpectedly passed MyPy.\n")
        if missing:
            sys.stderr.write(f"Missing expected MyPy diagnostics: {', '.join(missing)}\n")
        return 1

    print("Readiness types accept the production extraction and reject invalid values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
