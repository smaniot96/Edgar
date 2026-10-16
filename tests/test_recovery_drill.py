"""Coordinated restore proof using disposable PostgreSQL and Qdrant containers only."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import pytest
from docker.errors import DockerException
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer
from testcontainers.qdrant import QdrantContainer

from tools import recovery

ROOT = Path(__file__).resolve().parents[1]
SLUG = "recovery_fixture"
SOURCE_HASH = "a" * 64
EXTRACTION_HASH = "b" * 64
MANUSCRIPT_TEXT = b"# Recovery fixture\n\nA wholly synthetic retained manuscript.\n"
PDF_TEXT = b"synthetic PDF source bytes"
VECTOR = [1.0] + [0.0] * 1535


def _pg_url(host: str, port: int, user: str, password: str, database: str) -> str:
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{database}"
    )


def _async_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def _connection_parts(database_url: str) -> tuple[str, str]:
    parsed = urlparse(database_url)
    return parsed.username or "", parsed.path.strip("/")


def _qdrant_url(container: QdrantContainer) -> str:
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6333)
    return f"http://{host}:{port}"


def _create_database(pg, name: str) -> None:
    result = pg.exec(["createdb", "-U", _connection_parts(pg.get_connection_url())[0], name])
    assert result.exit_code == 0, result.output


def _pg_dump_inside_container(pg, database_url: str, destination: Path) -> None:
    user, database = _connection_parts(database_url)
    result = pg.exec(
        ["pg_dump", "--format=custom", "--no-owner", "--no-acl", "--username", user, "--dbname", database]
    )
    assert result.exit_code == 0, result.output
    destination.write_bytes(result.output)


def _empty_database_inside_container(pg, database_url: str) -> bool:
    user, database = _connection_parts(database_url)
    query = (
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f');"
    )
    result = pg.exec(["psql", "--no-psqlrc", "-tA", "-U", user, "-d", database, "-c", query])
    return result.exit_code == 0 and result.output.decode().strip() == "0"


def _pg_restore_inside_container(pg, database_url: str, dump_path: Path) -> None:
    user, database = _connection_parts(database_url)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        archive.add(dump_path, arcname="edgar-restore.dump")
    assert pg._container.put_archive("/tmp", stream.getvalue())
    result = pg.exec(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--username",
            user,
            "--dbname",
            database,
            "/tmp/edgar-restore.dump",
        ]
    )
    assert result.exit_code == 0, result.output


def _migrate_source_database(database_url: str, user: str, password: str, host: str, port: int) -> None:
    environment = os.environ.copy()
    environment.update(
        DB_HOST=host,
        DB_PORT=str(port),
        POSTGRES_USER=user,
        POSTGRES_PASSWORD=password,
        POSTGRES_DB=urlparse(database_url).path.strip("/"),
        OPENAI_API_KEY="test-key-for-ci",
    )
    subprocess.run(
        ["uv", "run", "--project", str(ROOT), "python", "-m", "alembic", "upgrade", "head"],
        cwd=ROOT / "db",
        env=environment,
        check=True,
        capture_output=True,
    )


async def _seed_source(database_url: str, index_version: str, data_root: Path) -> tuple[int, int]:
    from db.postgres.models import (
        Campaign,
        Character,
        CharacterAssignment,
        EventLog,
        Session,
        User,
    )

    engine = create_async_engine(_async_url(database_url), poolclass=None)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        user = User(email="recovery-drill@example.test", display_name="Recovery Test")
        db.add(user)
        await db.flush()
        campaign = Campaign(
            title="Disposable recovery campaign",
            system="D&D 5e",
            created_by=user.id,
            adventure_collections=[SLUG],
            source_mode="homebrew",
            rules_edition="2014",
        )
        db.add(campaign)
        await db.flush()
        hero = Character(
            owner_user_id=user.id,
            name="Mira",
            character_class="Fighter",
            level=1,
            hp_max=12,
            base_stats={"str": 12, "dex": 12, "con": 12, "int": 10, "wis": 10, "cha": 10},
            base_inventory={},
        )
        db.add(hero)
        await db.flush()
        db.add(
            CharacterAssignment(
                character_id=hero.id,
                campaign_id=campaign.id,
                hp_current=12,
                hp_max=12,
                stats=hero.base_stats,
                inventory={},
            )
        )
        session = Session(
            campaign_id=campaign.id,
            active_character_id=hero.id,
            current_scene_id="recovery_scene",
        )
        db.add(session)
        await db.flush()
        db.add(
            EventLog(
                session_id=session.id,
                event_type="narration",
                payload={
                    "player_input": "I inspect the old map.",
                    "narration": "The map carries a hand-drawn mark.",
                    "citations": [],
                    "rules_edition": "2014",
                    "turn_id": "prior-recovered-turn",
                },
            )
        )
        await db.commit()
        campaign_id, session_id = campaign.id, session.id
    await engine.dispose()

    manuscript_digest = hashlib.sha256(MANUSCRIPT_TEXT).hexdigest()
    attempt_id = "c" * 32
    attempts = data_root / "adventures" / SLUG / ".attempts" / attempt_id
    attempts.mkdir(parents=True)
    (attempts / f"{SLUG}.manuscript.md").write_bytes(MANUSCRIPT_TEXT)
    (data_root / "pdfs").mkdir(parents=True)
    (data_root / "pdfs" / f"{SLUG}.pdf").write_bytes(PDF_TEXT)
    meta = {
        "status": "ready",
        "title": "Recovery fixture",
        "rules_edition": "2014",
        "artifact_attempt": attempt_id,
        "chunks": 1,
        "ingestion": {
            "source_kind": "generated",
            "source_sha256": SOURCE_HASH,
            "extraction_sha256": EXTRACTION_HASH,
            "index_version": index_version,
            "quality_passed": True,
            "required_pages": [1],
        },
        "manuscript": {
            "attempt_id": attempt_id,
            "filename": f"{SLUG}.manuscript.md",
            "version": manuscript_digest,
            "sha256": manuscript_digest,
            "source_sha256": SOURCE_HASH,
            "extraction_sha256": EXTRACTION_HASH,
            "index_version": index_version,
        },
    }
    meta_dir = data_root / "adventures"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{SLUG}.json").write_text(json.dumps(meta), encoding="utf-8")
    return campaign_id, session_id


@pytest.mark.asyncio
async def test_coordinated_bundle_restores_campaign_history_manuscript_vectors_and_offline_turn(
    tmp_path, monkeypatch
):
    try:
        with (
            PostgresContainer(
                "postgres:16-alpine",
                username="edgar",
                password="edgar-test-only",
                dbname="edgar_restore_source",
            ) as pg,
            QdrantContainer("qdrant/qdrant:v1.16.3") as source_qdrant,
            QdrantContainer("qdrant/qdrant:v1.16.3") as target_qdrant,
        ):
            source_db = pg.get_connection_url()
            parsed = urlparse(source_db)
            host, port = parsed.hostname or "127.0.0.1", parsed.port or 5432
            user, password = parsed.username or "edgar", parsed.password or "edgar-test-only"
            source_db = _pg_url(host, port, user, password, "edgar_restore_source")
            _create_database(pg, "edgar_restore_target")
            _migrate_source_database(source_db, user, password, host, port)

            from qdrant_client.models import Distance, VectorParams

            from db.vector.versions import read_index_state
            from ingestion import embed

            lock_dir = tmp_path / "locks"
            monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(lock_dir))
            source_client = source_qdrant.get_client()
            source_client.create_collection(
                SLUG, vectors_config=VectorParams(size=1536, distance=Distance.COSINE)
            )
            monkeypatch.setattr(embed, "get_qdrant_client", lambda: source_client)
            monkeypatch.setattr(embed, "get_embeddings", lambda texts: [VECTOR for _ in texts])
            embed._embed_and_upsert(
                [
                    {
                        "text": "A synthetic trail begins near the old bridge.",
                        "index": 0,
                        "source": SLUG,
                        "page": 1,
                        "source_pages": [1],
                        "source_sha256": SOURCE_HASH,
                        "extraction_sha256": EXTRACTION_HASH,
                        "rules_edition": "2014",
                    }
                ],
                SLUG,
            )
            index_version = read_index_state(source_client, SLUG)["sources"][SLUG]["active"]["index_version"]
            data_root = tmp_path / "source-data"
            campaign_id, session_id = await _seed_source(source_db, index_version, data_root)

            source_q_url = _qdrant_url(source_qdrant)
            target_q_url = _qdrant_url(target_qdrant)
            bundle = tmp_path / "bundle"
            recovery.create_bundle(
                bundle,
                data_root,
                source_db,
                source_q_url,
                writes_quiesced=True,
                pg_dump=lambda url, path: _pg_dump_inside_container(pg, url, path),
            )
            assert recovery.verify_bundle(bundle)["qdrant_collections"][0]["collection"] == SLUG
            target_db = _pg_url(host, port, user, password, "edgar_restore_target")
            restored_data = tmp_path / "restored-data"
            recovery.restore_bundle(
                bundle,
                target_db,
                target_q_url,
                restored_data,
                disposable_target=True,
                pg_restore=lambda url, dump: _pg_restore_inside_container(pg, url, dump),
                empty_database_check=lambda url: _empty_database_inside_container(pg, url),
            )

            target_client = target_qdrant.get_client()
            restored_state = read_index_state(target_client, SLUG)
            restored_version = restored_state["sources"][SLUG]["active"]["index_version"]
            assert restored_version == index_version

            from apps.api.dependencies import get_redis, get_session
            from apps.api.main import app
            from apps.api.routers import health, session as session_router
            from apps.api.services import adventures_meta, campaign_sources
            from db.vector.source_status import inspect_source
            from db.vector.versions import active_index_version

            target_engine = create_async_engine(_async_url(target_db), poolclass=None)
            target_factory = async_sessionmaker(
                target_engine, class_=AsyncSession, expire_on_commit=False
            )
            monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", restored_data / "adventures")
            monkeypatch.setattr(adventures_meta, "PDF_DIR", restored_data / "pdfs")
            monkeypatch.setattr(campaign_sources, "PDF_DIR", restored_data / "pdfs")
            monkeypatch.setattr(
                campaign_sources,
                "inspect_source",
                lambda slug, **kwargs: inspect_source(slug, client=target_client, **kwargs),
            )
            monkeypatch.setattr(
                campaign_sources,
                "active_index_version",
                lambda slug: active_index_version(slug, client=target_client),
            )
            monkeypatch.setattr(health, "VECTOR_DB_URL", target_q_url)
            monkeypatch.setattr(health, "VECTOR_DB_API_KEY", "")
            import db.postgres.session as session_module

            monkeypatch.setattr(session_module, "async_session_factory", target_factory)
            monkeypatch.setattr(session_module, "engine", target_engine)

            async def override_session():
                async with target_factory() as db:
                    yield db

            redis = FakeAsyncRedis(decode_responses=True)

            async def override_redis():
                yield redis

            async def offline_turn(initial):
                assert initial["player_input"] == "I trace the trail."
                assert initial["turn_snapshot"]["character"]["name"] == "Mira"
                return {
                    "narration": "The restored trail continues beyond the bridge.",
                    "source_mode": initial["source_mode"],
                    "adventure_collections": initial["adventure_collections"],
                    "rules_edition": initial["rules_edition"],
                    "turn_id": initial["turn_id"],
                    "retrieval_degraded": initial["retrieval_degraded"],
                    "source_status": initial["source_status"],
                    "turn_snapshot": initial["turn_snapshot"],
                    "story_state": initial["story_state"],
                    "world_flags": initial["world_flags"],
                    "adventure_manifests": {},
                    "prepared_campaign_overlays": initial["prepared_campaign_overlays"],
                    "prepared_amendment_events": initial["prepared_amendment_events"],
                    "rules_context": [],
                    "adventure_context": [],
                }

            async def no_summary(_session_id):
                return None

            monkeypatch.setattr(session_router, "run_turn", offline_turn)
            monkeypatch.setattr(session_router, "maybe_update_memory_summary", no_summary)
            app.dependency_overrides[get_session] = override_session
            app.dependency_overrides[get_redis] = override_redis
            try:
                from asgi_lifespan import LifespanManager

                async with LifespanManager(app):
                    async with AsyncClient(
                        transport=ASGITransport(app=app), base_url="http://recovery.test"
                    ) as client:
                        ready = await client.get("/ready")
                        assert ready.status_code == 200
                        assert ready.json()["checks"] == {
                            "postgres": "ok",
                            "schema": "ok",
                            "redis": "ok",
                            "qdrant": "ok",
                        }
                        campaign = await client.get(f"/api/campaigns/{campaign_id}")
                        assert campaign.status_code == 200
                        assert campaign.json()["adventure_collections"] == [SLUG]
                        assert campaign.json()["sources"][0]["status"] == "ready"
                        assert (restored_data / "pdfs" / f"{SLUG}.pdf").read_bytes() == PDF_TEXT
                        history = await client.get(f"/api/sessions/{session_id}/messages")
                        assert history.status_code == 200
                        assert history.json()[0]["content"] == "I inspect the old map."

                        from apps.api.services.manuscript import resolve_canonical_manuscript

                        manuscript = resolve_canonical_manuscript(SLUG)
                        assert manuscript.path.read_bytes() == MANUSCRIPT_TEXT
                        assert manuscript.sha256 == json.loads(
                            (restored_data / "adventures" / f"{SLUG}.json").read_text()
                        )["manuscript"]["sha256"]
                        assert manuscript.index_version == restored_version

                        turn = await client.post(
                            f"/api/sessions/{session_id}/turn", json={"message": "I trace the trail."}
                        )
                        assert turn.status_code == 200, turn.text
                        assert turn.json()["narration"] == "The restored trail continues beyond the bridge."
                        assert turn.json()["character"]["name"] == "Mira"
                        reopened = await client.get(f"/api/sessions/{session_id}/messages")
                        assert reopened.status_code == 200
                        assert reopened.json()[-1]["content"] == "The restored trail continues beyond the bridge."
            finally:
                app.dependency_overrides.clear()
                await redis.aclose()
                await target_engine.dispose()
                source_client.close()
                target_client.close()
    except DockerException as exc:
        if os.environ.get("CI"):
            pytest.fail(f"Docker is required for the coordinated recovery drill in CI: {exc}")
        pytest.skip(f"Docker unavailable; disposable PostgreSQL/Qdrant restore proof not run: {exc}")
