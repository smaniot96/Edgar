"""Coordinated, checksum-verified Edgar backup bundles and disposable restores.

The capture command requires an operator attestation that application writers and background
workers are stopped. PostgreSQL, source artifacts and Qdrant are then captured in one window.
Restore is deliberately restricted to an empty loopback PostgreSQL database named
``edgar_restore_*``, an empty loopback Qdrant instance and a new/empty source directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

import httpx
from qdrant_client import QdrantClient

SCHEMA_VERSION = 1
SOURCE_DIRS = ("adventures", "adventure-assets", "pdfs", "markdown", "extraction-qa")
SECRET_NAMES = {".env", ".env.local", "credentials", "credentials.json", "secrets.json"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RESTORE_DB_PREFIX = "edgar_restore_"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _database_url_from_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"Set {name}; credentials are read from the environment and never archived.")
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+psycopg", "postgresql+asyncpg"}:
        raise ValueError(f"{name} must be a PostgreSQL URL.")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError(f"{name} must identify a PostgreSQL host and database.")
    return value


def _pg_environment(database_url: str) -> tuple[dict[str, str], str]:
    parsed = urlparse(database_url)
    db_name = unquote(parsed.path.strip("/"))
    environment = os.environ.copy()
    environment.update(
        PGHOST=parsed.hostname or "",
        PGPORT=str(parsed.port or 5432),
        PGUSER=unquote(parsed.username or ""),
        PGPASSWORD=unquote(parsed.password or ""),
        PGDATABASE=db_name,
    )
    if parsed.query:
        # libpq options may carry connection settings, but query text is never written to bundles.
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        for key in ("sslmode", "application_name", "target_session_attrs"):
            if key in query:
                environment["PG" + key.upper()] = query[key][-1]
    return environment, db_name


def _require_local_disposable_database(database_url: str) -> str:
    parsed = urlparse(database_url)
    db_name = unquote(parsed.path.strip("/"))
    if parsed.hostname not in LOCAL_HOSTS or not db_name.startswith(RESTORE_DB_PREFIX):
        raise ValueError(
            "Restore target must be loopback and its database name must start with edgar_restore_."
        )
    if unquote(parsed.username or "") in {"postgres", "root", "admin"}:
        raise ValueError("Restore target must use a dedicated disposable database role.")
    return db_name


def _validate_qdrant_url(qdrant_url: str, *, require_loopback: bool) -> None:
    parsed = urlparse(qdrant_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Qdrant URL must use HTTP or HTTPS and identify a host.")
    if parsed.username or parsed.password or parsed.query:
        raise ValueError("Keep Qdrant credentials out of the URL; provide the API key separately.")
    if require_loopback and parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("Qdrant restore target must be loopback.")


def _is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SECRET_NAMES
        or name.startswith(".env.")
        or name.endswith(tuple(SECRET_SUFFIXES))
        or "credential" in name
        or "secret" in name
    )


def _write_source_archive(source_data: Path, target: Path) -> dict[str, int]:
    """Archive application source state only; never sweep arbitrary data or credentials."""
    root = source_data.resolve(strict=True)
    counts: dict[str, int] = {}
    with tarfile.open(target, "w:gz") as archive:
        for relative_root in SOURCE_DIRS:
            candidate = root / relative_root
            if not candidate.exists():
                counts[relative_root] = 0
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                raise ValueError(f"Source path {relative_root!r} must be a real directory.")
            count = 0
            for path in sorted(candidate.rglob("*")):
                if path.is_symlink():
                    raise ValueError("Source tree contains a symlink; review it before backup.")
                if _is_secret_path(path):
                    continue
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
                    count += 1
                elif path.is_dir():
                    archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
            counts[relative_root] = count
    return counts


def _validate_source_bindings(source_data: Path, vector_entries: list[dict]) -> None:
    """Check published adventure metadata, retained originals and active vector receipts agree."""
    active = {
        entry["collection"]: entry.get("active_versions", {}) for entry in vector_entries
    }
    adventures = source_data / "adventures"
    if not adventures.is_dir():
        return
    for meta_path in adventures.glob("*.json"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,254}", meta_path.stem):
            raise ValueError("Adventure metadata contains an invalid source binding.")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Adventure metadata {meta_path.name!r} is unreadable.") from exc
        ingestion = meta.get("ingestion")
        if meta.get("status") == "ready" and isinstance(ingestion, dict):
            expected = ingestion.get("index_version")
            if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"Adventure {meta_path.stem!r} has an invalid index-version receipt.")
            if expected and active.get(meta_path.stem, {}).get(meta_path.stem) != expected:
                raise ValueError(
                    f"Adventure {meta_path.stem!r} metadata does not match its active vector index version."
                )
        manuscript = meta.get("manuscript")
        if not isinstance(manuscript, dict):
            continue
        attempt = manuscript.get("attempt_id")
        filename = manuscript.get("filename")
        if (
            not isinstance(attempt, str)
            or not re.fullmatch(r"[0-9a-f]{32}", attempt)
            or filename != f"{meta_path.stem}.manuscript.md"
        ):
            raise ValueError(f"Adventure {meta_path.stem!r} has an unsafe manuscript reference.")
        path = adventures / meta_path.stem / ".attempts" / attempt / filename
        if not path.is_file() or _sha256(path) != manuscript.get("sha256"):
            raise ValueError(
                f"Adventure {meta_path.stem!r} retained manuscript checksum does not match metadata."
            )
        if manuscript.get("version") != manuscript.get("sha256"):
            raise ValueError(f"Adventure {meta_path.stem!r} manuscript version receipt is inconsistent.")
        for key in ("source_sha256", "extraction_sha256"):
            value = manuscript.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"Adventure {meta_path.stem!r} manuscript provenance is invalid.")
            if value != (ingestion or {}).get(key):
                raise ValueError(
                    f"Adventure {meta_path.stem!r} manuscript provenance does not match ingestion metadata."
                )
        index_version = (ingestion or {}).get("index_version")
        if index_version and manuscript.get("index_version") not in (None, index_version):
            raise ValueError(f"Adventure {meta_path.stem!r} manuscript index receipt is stale.")


def _qdrant_headers(api_key: str | None) -> dict[str, str]:
    return {"api-key": api_key} if api_key else {}


def _download_snapshot(
    url: str, api_key: str | None, collection: str, snapshot_name: str, destination: Path
) -> None:
    endpoint = (
        f"{url.rstrip('/')}/collections/{quote(collection, safe='')}/snapshots/"
        f"{quote(snapshot_name, safe='')}"
    )
    try:
        with httpx.stream("GET", endpoint, headers=_qdrant_headers(api_key), timeout=120) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for block in response.iter_bytes():
                    output.write(block)
    except httpx.HTTPError as exc:
        raise RuntimeError("Could not download a Qdrant collection snapshot.") from exc
    if destination.stat().st_size == 0:
        raise RuntimeError("Qdrant returned an empty collection snapshot.")


def _capture_vectors(qdrant_url: str, api_key: str | None, directory: Path) -> list[dict]:
    from db.vector.versions import read_index_state

    client = QdrantClient(url=qdrant_url, api_key=api_key or None, timeout=30)
    created: list[tuple[str, str]] = []
    entries = []
    try:
        names = sorted(item.name for item in client.get_collections().collections)
        for name in names:
            state = read_index_state(client, name)
            if any(entry.get("pending") for entry in state.get("sources", {}).values()):
                raise RuntimeError(
                    f"Qdrant collection {name!r} has a pending source activation; finish or reconcile it before capture."
                )
            snapshot = client.create_snapshot(name, wait=True)
            if snapshot is None or not snapshot.name:
                raise RuntimeError(f"Qdrant did not return a snapshot receipt for {name!r}.")
            created.append((name, snapshot.name))
            filename = f"collection-{hashlib.sha256(name.encode()).hexdigest()}.snapshot"
            path = directory / filename
            _download_snapshot(qdrant_url, api_key, name, snapshot.name, path)
            entries.append(
                {
                    "collection": name,
                    "snapshot_name": snapshot.name,
                    "artifact": f"vectors/{filename}",
                    "sha256": _sha256(path),
                    "bytes": path.stat().st_size,
                    "active_versions": {
                        source: entry["active"]["index_version"]
                        for source, entry in state.get("sources", {}).items()
                        if isinstance(entry.get("active"), dict)
                        and isinstance(entry["active"].get("index_version"), str)
                    },
                }
            )
    finally:
        for name, snapshot_name in created:
            try:
                client.delete_snapshot(name, snapshot_name, wait=True)
            except Exception:
                # The downloaded immutable archive is valid; a leftover transient source snapshot
                # is an operational cleanup warning, not a reason to conceal the capture failure.
                pass
        client.close()
    return entries


def _run_pg_dump(database_url: str, destination: Path) -> None:
    environment, db_name = _pg_environment(database_url)
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(destination),
        "--dbname",
        db_name,
    ]
    try:
        result = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError("pg_dump is required on PATH to capture PostgreSQL.") from exc
    if result.returncode:
        raise RuntimeError(f"pg_dump failed with exit status {result.returncode}; credentials were withheld.")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("pg_dump did not produce a nonempty database archive.")


def create_bundle(
    output: Path,
    source_data: Path,
    database_url: str,
    qdrant_url: str,
    *,
    writes_quiesced: bool,
    qdrant_api_key: str | None = None,
    pg_dump: Callable[[str, Path], None] = _run_pg_dump,
) -> Path:
    """Capture a coordinated bundle. ``pg_dump`` is injectable for disposable integration drills."""
    if not writes_quiesced:
        raise ValueError("Stop API writers and background workers, then attest with --writes-quiesced.")
    _validate_qdrant_url(qdrant_url, require_loopback=False)
    source_data = source_data.resolve(strict=True)
    output = Path(output).expanduser()
    if output.is_symlink():
        raise ValueError("Backup output must not be a symlink.")
    output = output.resolve()
    if output.exists():
        raise ValueError("Backup output already exists; choose a new destination.")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".edgar-backup-", dir=output.parent))
    vectors_dir = stage / "vectors"
    vectors_dir.mkdir()
    started = _utc_now()
    try:
        dump_path = stage / "postgres.dump"
        pg_dump(database_url, dump_path)
        source_path = stage / "sources.tar.gz"
        source_counts = _write_source_archive(source_data, source_path)
        vector_entries = _capture_vectors(qdrant_url, qdrant_api_key, vectors_dir)
        _validate_source_bindings(source_data, vector_entries)
        _, db_name = _pg_environment(database_url)
        artifacts = {}
        for relative in ("postgres.dump", "sources.tar.gz"):
            artifact = stage / relative
            artifacts[relative] = {"sha256": _sha256(artifact), "bytes": artifact.stat().st_size}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "capture_id": uuid.uuid4().hex,
            "captured_started_at": started,
            "captured_finished_at": _utc_now(),
            "source_database_label": db_name,
            "source_roots": list(SOURCE_DIRS),
            "source_file_counts": source_counts,
            "artifacts": artifacts,
            "qdrant_collections": vector_entries,
            "coordination": "operator-attested-writers-quiesced",
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        stage.replace(output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return output


def _load_manifest(bundle: Path) -> dict:
    try:
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Backup manifest is missing or invalid.") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Backup bundle schema is unsupported.")
    return manifest


def verify_bundle(bundle: Path) -> dict:
    bundle = Path(bundle).expanduser()
    if bundle.is_symlink():
        raise ValueError("Backup bundle must not be a symlink.")
    bundle = bundle.resolve(strict=True)
    if not bundle.is_dir():
        raise ValueError("Backup bundle must be a regular directory.")
    manifest = _load_manifest(bundle)
    expected = {"postgres.dump", "sources.tar.gz"}
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != expected:
        raise ValueError("Backup artifact inventory is incomplete or unexpected.")
    for relative, record in artifacts.items():
        path = bundle / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Backup artifact {relative!r} is missing or unsafe.")
        if path.stat().st_size != record.get("bytes") or _sha256(path) != record.get("sha256"):
            raise ValueError(f"Backup artifact {relative!r} failed its checksum.")
    vectors = manifest.get("qdrant_collections")
    if not isinstance(vectors, list):
        raise ValueError("Qdrant snapshot inventory is invalid.")
    seen_collections: set[str] = set()
    for record in vectors:
        collection = record.get("collection")
        relative = record.get("artifact")
        if not isinstance(collection, str) or not collection or collection in seen_collections:
            raise ValueError("Qdrant collection inventory contains a duplicate or invalid name.")
        seen_collections.add(collection)
        if not isinstance(relative, str) or not re.fullmatch(
            r"vectors/collection-[0-9a-f]{64}\.snapshot", relative
        ):
            raise ValueError(f"Qdrant snapshot path for {collection!r} is invalid.")
        path = bundle / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != record.get("bytes")
            or _sha256(path) != record.get("sha256")
        ):
            raise ValueError(f"Qdrant snapshot for {collection!r} failed its checksum.")
        versions = record.get("active_versions")
        if not isinstance(versions, dict) or any(
            not isinstance(source, str)
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9a-f]{64}", version)
            for source, version in versions.items()
        ):
            raise ValueError(f"Qdrant active-version receipts for {collection!r} are invalid.")
    expected_files = {
        "manifest.json",
        "postgres.dump",
        "sources.tar.gz",
        *(record["artifact"] for record in vectors),
    }
    actual_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != expected_files or any(_is_secret_path(Path(name)) for name in actual_files):
        raise ValueError("Backup bundle contains an unexpected file or secret-named path.")
    with tempfile.TemporaryDirectory(prefix="edgar-backup-verify-") as temporary:
        restored_data = Path(temporary)
        _extract_sources(bundle / "sources.tar.gz", restored_data)
        _validate_source_bindings(restored_data, vectors)
    return manifest


def _empty_destination(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("Source restore destination must be a new or empty real directory.")
    path = path.resolve()
    if path.exists():
        if path.is_symlink() or not path.is_dir() or any(path.iterdir()):
            raise ValueError("Source restore destination must be a new or empty real directory.")
    else:
        path.mkdir(parents=True)
    return path


def _extract_sources(archive_path: Path, destination: Path) -> None:
    allowed = set(SOURCE_DIRS)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                member_path = PurePosixPath(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or not member_path.parts
                    or member_path.parts[0] not in allowed
                    or not (member.isdir() or member.isfile())
                    or _is_secret_path(Path(member.name))
                ):
                    raise ValueError("Source archive contains an unsafe path or file type.")
            archive.extractall(destination, members=members, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError("Source archive could not be safely extracted.") from exc


def _qdrant_is_empty(client: QdrantClient) -> bool:
    return not client.get_collections().collections


def _assert_empty_qdrant(qdrant_url: str, api_key: str | None) -> None:
    _validate_qdrant_url(qdrant_url, require_loopback=True)
    client = QdrantClient(url=qdrant_url, api_key=api_key or None, timeout=30)
    try:
        if not _qdrant_is_empty(client):
            raise ValueError("Qdrant restore target must contain no collections.")
    finally:
        client.close()


def _restore_vectors(bundle: Path, manifest: dict, qdrant_url: str, api_key: str | None) -> None:
    _validate_qdrant_url(qdrant_url, require_loopback=True)
    client = QdrantClient(url=qdrant_url, api_key=api_key or None, timeout=60)
    try:
        if not _qdrant_is_empty(client):
            raise ValueError("Qdrant restore target must contain no collections.")
        for record in manifest["qdrant_collections"]:
            with (bundle / record["artifact"]).open("rb") as snapshot:
                client.recover_from_uploaded_snapshot(
                    collection_name=record["collection"], snapshot=snapshot, wait=True
                )
    finally:
        client.close()


def _run_empty_database_check(database_url: str) -> bool:
    environment, db_name = _pg_environment(database_url)
    sql = (
        "SELECT (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')) + "
        "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public') + "
        "(SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
        "WHERE n.nspname='public');"
    )
    try:
        result = subprocess.run(
            ["psql", "--no-psqlrc", "--tuples-only", "--no-align", "--dbname", db_name, "--command", sql],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError("psql is required on PATH to verify an empty restore database.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Could not verify the PostgreSQL restore target; credentials were withheld.") from exc
    return result.stdout.strip() == "0"


def _run_pg_restore(database_url: str, dump_path: Path) -> None:
    environment, db_name = _pg_environment(database_url)
    command = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        "--dbname",
        db_name,
        str(dump_path),
    ]
    try:
        result = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError("pg_restore is required on PATH to restore PostgreSQL.") from exc
    if result.returncode:
        raise RuntimeError(f"pg_restore failed with exit status {result.returncode}; credentials were withheld.")


def restore_bundle(
    bundle: Path,
    database_url: str,
    qdrant_url: str,
    source_destination: Path,
    *,
    disposable_target: bool,
    qdrant_api_key: str | None = None,
    pg_restore: Callable[[str, Path], None] = _run_pg_restore,
    empty_database_check: Callable[[str], bool] = _run_empty_database_check,
) -> dict:
    if not disposable_target:
        raise ValueError("Restore requires the explicit --disposable-target acknowledgement.")
    database_name = _require_local_disposable_database(database_url)
    manifest = verify_bundle(bundle)
    if database_name == manifest.get("source_database_label"):
        raise ValueError("Restore database must differ from the captured source database.")
    _validate_qdrant_url(qdrant_url, require_loopback=True)
    if not empty_database_check(database_url):
        raise ValueError("PostgreSQL restore target must contain no public objects.")
    _assert_empty_qdrant(qdrant_url, qdrant_api_key)
    destination = _empty_destination(source_destination)
    _extract_sources(Path(bundle) / "sources.tar.gz", destination)
    _restore_vectors(Path(bundle), manifest, qdrant_url, qdrant_api_key)
    pg_restore(database_url, Path(bundle) / "postgres.dump")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="Create a coordinated, checksummed bundle")
    capture.add_argument("--source-data", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--writes-quiesced", action="store_true")
    restore = commands.add_parser("restore", help="Restore only into an empty disposable target")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--source-destination", type=Path, required=True)
    restore.add_argument("--disposable-target", action="store_true")
    verify = commands.add_parser("verify", help="Verify all bundle checksums without restoring")
    verify.add_argument("--bundle", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            bundle = create_bundle(
                args.output,
                args.source_data,
                _database_url_from_env("EDGAR_BACKUP_DATABASE_URL"),
                os.environ.get("EDGAR_BACKUP_QDRANT_URL", ""),
                writes_quiesced=args.writes_quiesced,
                qdrant_api_key=os.environ.get("EDGAR_BACKUP_QDRANT_API_KEY"),
            )
            print(f"Created coordinated recovery bundle {bundle} ({_load_manifest(bundle)['capture_id']}).")
        elif args.command == "verify":
            manifest = verify_bundle(args.bundle)
            print(
                f"Verified capture {manifest['capture_id']}: PostgreSQL, source archive and "
                f"{len(manifest['qdrant_collections'])} Qdrant collection snapshots."
            )
        elif args.command == "restore":
            manifest = restore_bundle(
                args.bundle,
                _database_url_from_env("EDGAR_RESTORE_DATABASE_URL"),
                os.environ.get("EDGAR_RESTORE_QDRANT_URL", ""),
                args.source_destination,
                disposable_target=args.disposable_target,
                qdrant_api_key=os.environ.get("EDGAR_RESTORE_QDRANT_API_KEY"),
            )
            print(f"Restored capture {manifest['capture_id']} into the disposable target.")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"recovery: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
