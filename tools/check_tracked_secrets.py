"""Exercise Gitleaks, then scan only files committed in the checked-out tree."""

from __future__ import annotations

import os
import secrets
import string
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run_gitleaks(binary: str, source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            binary,
            "dir",
            "--redact=100",
            "--no-banner",
            "--gitleaks-ignore-path",
            str(source),
            "--exit-code",
            "1",
            str(source),
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def verify_redaction(binary: str) -> None:
    alphabet = string.ascii_letters + string.digits
    fake_token = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
    with tempfile.TemporaryDirectory(prefix="gitleaks-synthetic-") as temporary:
        source = Path(temporary)
        (source / "synthetic.txt").write_text(f"github_token={fake_token}\n", encoding="utf-8")
        result = run_gitleaks(binary, source)
    output = result.stdout + result.stderr
    if result.returncode != 1 or fake_token in output or "leaks found: 1" not in output:
        raise RuntimeError("Gitleaks synthetic detection or redaction check failed.")
    print("Gitleaks detected and redacted one synthetic token.")


def scan_tracked_tree(binary: str) -> int:
    with tempfile.TemporaryDirectory(prefix="gitleaks-tracked-tree-") as temporary:
        archive = subprocess.Popen(
            ["git", "archive", "--format=tar", "HEAD"], stdout=subprocess.PIPE
        )
        if archive.stdout is None:
            raise RuntimeError("Could not read the Git archive stream.")
        with tarfile.open(fileobj=archive.stdout, mode="r|*") as source:
            source.extractall(temporary, filter="data")
        archive.stdout.close()
        archive_status = archive.wait()
        if archive_status:
            raise RuntimeError("Could not create the tracked Git archive.")
        result = run_gitleaks(binary, Path(temporary))

    output = result.stdout + result.stderr
    if output:
        sys.stdout.write(output)
        if not output.endswith("\n"):
            sys.stdout.write("\n")
    return result.returncode


def main() -> int:
    binary = os.environ.get("GITLEAKS_BIN", "gitleaks")
    verify_redaction(binary)
    return scan_tracked_tree(binary)


if __name__ == "__main__":
    raise SystemExit(main())
