"""Build committed dev images and replace only the existing edgar-main API.

No migration, ingestion, data-service replacement or teardown is performed. Receipts,
source hashes and the generated Compose override belong outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path("/Users/psmaniotto/.claude-worktrees/edgar-b/edgar-main-run")
TARGETS = ("api", "ingestion", "migrate", "rules_engine")
INFRASTRUCTURE = ("edgar-main-db", "edgar-main-redis", "edgar-main-qdrant")
USER_CHANGES = {" D .claude/launch.json", " D .cursor/rules/Python-developer.mdc"}


def run(*args: str, capture: bool = True, cwd: Path = ROOT) -> str:
    result = subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inspect(kind: str, name: str) -> dict:
    return json.loads(run("docker", kind, "inspect", name))[0]


def source_revision() -> str:
    branch = run("git", "branch", "--show-current").strip()
    if branch != "dev":
        raise RuntimeError(f"Build/deploy requires the exact dev branch; found {branch!r}")
    changes = set(run("git", "status", "--porcelain", "--untracked-files=all").splitlines())
    unexpected = changes - USER_CHANGES
    if unexpected:
        raise RuntimeError(
            "Commit task changes first; uncommitted paths: " + repr(sorted(unexpected))
        )
    return run("git", "rev-parse", "HEAD").strip()


def infrastructure() -> dict:
    """Only retain safe identity/mount evidence; never serialize environment settings."""
    result = {}
    for name in INFRASTRUCTURE:
        item = inspect("container", name)
        if not item["State"]["Running"]:
            raise RuntimeError(f"Existing infrastructure is not running: {name}")
        result[name] = {
            "container_id": item["Id"],
            "image_id": item["Image"],
            "mounts": item["Mounts"],
        }
    expected = {
        "edgar-main-db": ("/var/lib/postgresql/data", "edgar-main_postgres_data"),
        "edgar-main-qdrant": ("/qdrant/storage", "edgar-main_qdrant_data"),
    }
    for name, (destination, volume) in expected.items():
        actual = {m["Destination"]: m.get("Name") for m in result[name]["mounts"]}
        if actual.get(destination) != volume:
            raise RuntimeError(f"Unexpected persistent mount for {name}: {actual}")
    return result


def backup_evidence(backup: Path) -> dict:
    manifest = json.loads((backup / "manifest.json").read_text())
    if Path(manifest["data"]).resolve() != (RUNTIME / "data").resolve():
        raise RuntimeError("Backup belongs to another campaign bind mount")
    for relative, expected in manifest["artifacts"].items():
        if digest(backup / relative) != expected:
            raise RuntimeError(f"Backup artifact hash mismatch: {relative}")
    files = json.loads((backup / "files-sha256.json").read_text())
    for relative, expected in files.items():
        if digest(RUNTIME / "data" / relative) != expected:
            raise RuntimeError(f"Archived file changed or is missing: {relative}")
    return {
        "path": str(backup),
        "manifest_sha256": digest(backup / "manifest.json"),
        "unchanged_files": len(files),
    }


def build(output: Path) -> None:
    revision = source_revision()
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "images.json"
    if receipt_path.exists():
        raise RuntimeError(f"Refusing to overwrite an existing image receipt: {receipt_path}")
    created = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix="edgar-dev-source-") as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "source.tar"
        context = temporary_path / "context"
        context.mkdir()
        run("git", "archive", "--format=tar", f"--output={archive}", revision)
        with tarfile.open(archive) as stream:
            stream.extractall(context, filter="data")
        # Hash the executable/source material that COPY includes in all owned images.
        suffixes = {".py", ".toml", ".json", ".sql", ".ini", ".js", ".ts", ".tsx", ".css", ".html"}
        files = {}
        for path in sorted(context.rglob("*")):
            relative = path.relative_to(context)
            if not path.is_file():
                continue
            if (
                path.suffix in suffixes
                or relative.as_posix() in {"uv.lock", "Dockerfile"}
                or relative.as_posix().startswith("apps/api/services/campaign_prep/prompts/")
            ) and relative.parts[0] in {
                "apps",
                "core",
                "db",
                "config",
                "tools",
                "frontend",
                "pyproject.toml",
                "uv.lock",
                "Dockerfile",
            }:
                files[relative.as_posix()] = digest(path)
        source = {"revision": revision, "branch": "dev", "created": created, "files": files}
        (context / "edgar-source-manifest.json").write_text(json.dumps(source, indent=2) + "\n")
        images = {}
        for target in TARGETS:
            tag = f"edgar-dev-{target.replace('_', '-')}:{revision}"
            run(
                "docker",
                "build",
                "--target",
                target,
                "--build-arg",
                f"EDGAR_SOURCE_REVISION={revision}",
                "--build-arg",
                "EDGAR_SOURCE_BRANCH=dev",
                "--build-arg",
                f"EDGAR_SOURCE_CREATED={created}",
                "--tag",
                tag,
                str(context),
                capture=False,
            )
            info = inspect("image", tag)
            labels = info["Config"]["Labels"]
            if (
                labels.get("org.opencontainers.image.revision") != revision
                or labels.get("io.edgar.source.branch") != "dev"
            ):
                raise RuntimeError(f"Build labels do not match dev source for {target}")
            images[target] = {"tag": tag, "image_id": info["Id"]}
        receipt = {
            "revision": revision,
            "branch": "dev",
            "created": created,
            "images": images,
            "source": source,
        }
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        f"Built all {len(TARGETS)} owned targets from committed dev {revision}; receipt: {receipt_path}"
    )


def override(output: Path, receipt: dict) -> Path:
    # JSON is YAML-compatible and avoids an additional YAML library in the rollout tool.
    services = {
        name: {"image": receipt["images"][name]["tag"], "pull_policy": "never"} for name in TARGETS
    }
    services["api"]["volumes"] = [
        {
            "type": "bind",
            "source": str(RUNTIME / "data"),
            "target": "/data",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": str(RUNTIME / "data/.index-locks"),
            "target": "/index-locks",
            "bind": {"create_host_path": False},
        },
    ]
    services["ingestion"]["volumes"] = [
        {
            "type": "bind",
            "source": str(RUNTIME / "data/pdfs"),
            "target": "/data",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": str(RUNTIME / "data/markdown"),
            "target": "/output/markdown",
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": str(RUNTIME / "data/.index-locks"),
            "target": "/index-locks",
            "bind": {"create_host_path": False},
        },
    ]
    services["rules_engine"]["volumes"] = [
        {
            "type": "bind",
            "source": str(RUNTIME / "data/.index-locks"),
            "target": "/index-locks",
            "bind": {"create_host_path": False},
        },
    ]
    value = {
        "services": services,
        "volumes": {
            "postgres_data": {"external": True, "name": "edgar-main_postgres_data"},
            "qdrant_data": {"external": True, "name": "edgar-main_qdrant_data"},
        },
    }
    path = output / "docker-compose.dev-rollout.json"
    path.write_text(json.dumps(value, indent=2) + "\n")
    return path


def compose_arguments(override_path: Path) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        "edgar-main",
        "--project-directory",
        str(RUNTIME),
        "--env-file",
        str(RUNTIME / ".env"),
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(RUNTIME / "docker-compose.local-main.yml"),
        "-f",
        str(override_path),
    ]


def verify(output: Path, *, require_clean_source: bool = True) -> dict:
    receipt = json.loads((output / "images.json").read_text())
    if require_clean_source and receipt["revision"] != source_revision():
        raise RuntimeError("Image receipt does not describe the current committed dev source")
    for target, image in receipt["images"].items():
        info = inspect("image", image["tag"])
        if info["Id"] != image["image_id"]:
            raise RuntimeError(f"Image tag no longer identifies the built {target} image")
    api = inspect("container", "edgar-main-api")
    if not api["State"]["Running"] or api["Image"] != receipt["images"]["api"]["image_id"]:
        raise RuntimeError("Running API is not the image recorded in the dev receipt")
    mounts = {m["Destination"]: m["Source"] for m in api["Mounts"]}
    if mounts.get("/data") != str(RUNTIME / "data") or mounts.get("/index-locks") != str(
        RUNTIME / "data/.index-locks"
    ):
        raise RuntimeError(f"API mounts differ from the original runtime: {mounts}")
    # Compare actual files in the running image, not just labels or a reusable tag.
    check = """import hashlib,json,pathlib
root=pathlib.Path('/app')
build=json.loads((root/'edgar-build.json').read_text())
source=json.loads((root/'edgar-source-manifest.json').read_text())
assert build['revision']==source['revision'] and build['branch']=='dev'
for name,expected in source['files'].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected, name
print(json.dumps({'build':build,'verified_source_files':len(source['files'])}))
"""
    evidence = json.loads(run("docker", "exec", "edgar-main-api", "python", "-c", check))
    if evidence["build"]["revision"] != receipt["revision"]:
        raise RuntimeError("Running source revision differs from build receipt")
    result = {
        "revision": receipt["revision"],
        "api_image_id": api["Image"],
        "api_container_id": api["Id"],
        "mounts": mounts,
        **evidence,
    }
    (output / "running.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def deploy(output: Path, backup: Path) -> None:
    revision = source_revision()
    receipt = json.loads((output / "images.json").read_text())
    if receipt["revision"] != revision:
        raise RuntimeError("Build receipt is for a different dev commit")
    # A tag may have been replaced after building. Check identities before any mutation.
    for target, item in receipt["images"].items():
        if inspect("image", item["tag"])["Id"] != item["image_id"]:
            raise RuntimeError(f"Built image identity changed for {target}")
    before = infrastructure()
    backup = backup.resolve()
    preserved = backup_evidence(backup)
    api = inspect("container", "edgar-main-api")
    mounts = {m["Destination"]: m["Source"] for m in api["Mounts"]}
    if mounts.get("/data") != str(RUNTIME / "data"):
        raise RuntimeError("Existing API uses an unexpected campaign mount")
    generated = override(output, receipt)
    arguments = compose_arguments(generated)
    # --no-deps preserves Redis memory, DB/Qdrant containers and their existing volumes.
    # --no-build ensures only the receipt's already-built committed source can be used.
    run(*arguments, "up", "--detach", "--no-deps", "--no-build", "--wait", "api", capture=False)
    after = infrastructure()
    if before != after:
        raise RuntimeError("Infrastructure identities/mounts changed during API rollout")
    evidence = verify(output)
    result = {"backup": preserved, "infrastructure": after, "running": evidence}
    (output / "deployment.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "revision": revision,
                "preserved_files": preserved["unchanged_files"],
                "api_image_id": evidence["api_image_id"],
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "deploy", "verify"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    arguments = parser.parse_args()
    output = arguments.output_dir.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error("Keep rollout receipts outside the source repository")
    if arguments.action == "build":
        build(output)
    elif arguments.action == "deploy":
        if arguments.backup is None:
            parser.error("deploy requires the existing consistent --backup directory")
        deploy(output, arguments.backup)
    else:
        print(json.dumps(verify(output), indent=2))


if __name__ == "__main__":
    main()
