"""Prove pip-audit detects a known bad pin, then audit the locked local environment."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="pip-audit-synthetic-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        requirements.write_text("starlette==1.0.0\n", encoding="utf-8")
        synthetic = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--disable-pip",
                "--no-deps",
                "--requirement",
                str(requirements),
                "--progress-spinner",
                "off",
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    synthetic_output = synthetic.stdout + synthetic.stderr
    if synthetic.returncode != 1 or "PYSEC-2026-161" not in synthetic_output:
        sys.stderr.write("pip-audit failed its synthetic vulnerable-package check.\n")
        sys.stderr.write(synthetic_output)
        return 1

    print("pip-audit detected the synthetic vulnerable Starlette pin.")
    audit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "--local",
            "--skip-editable",
            "--progress-spinner",
            "off",
        ],
        check=False,
        text=True,
    )
    return audit.returncode


if __name__ == "__main__":
    raise SystemExit(main())
