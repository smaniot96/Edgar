"""Verify Bandit's blocking policy, then scan the repository at that policy."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bandit(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "--project", str(ROOT), "bandit", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def verify_blocking_policy() -> None:
    with tempfile.TemporaryDirectory(prefix="bandit-synthetic-") as temporary:
        source = Path(temporary)
        (source / "unsafe.py").write_text(
            "import pickle\npickle.loads(b'payload')\n", encoding="utf-8"
        )
        result = bandit("-r", "-f", "json", "-ll", "-iii", str(source))

    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Bandit did not return a valid synthetic JSON report.") from error

    expected = any(
        issue.get("test_id") == "B301"
        and issue.get("issue_severity") == "MEDIUM"
        and issue.get("issue_confidence") == "HIGH"
        for issue in report.get("results", [])
    )
    if result.returncode != 1 or not expected:
        sys.stderr.write("Bandit failed its synthetic blocking-policy check.\n")
        sys.stderr.write(result.stdout + result.stderr)
        return_code = result.returncode
        if return_code == 0:
            raise RuntimeError("Bandit did not fail for a medium/high-confidence finding.")
        raise RuntimeError("Bandit did not report the expected B301 severity and confidence.")

    print("Bandit blocks a synthetic medium-severity, high-confidence B301 finding.")


def main() -> int:
    verify_blocking_policy()
    result = bandit("-r", "apps", "core", "db", "tools", "-ll", "-iii")
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
