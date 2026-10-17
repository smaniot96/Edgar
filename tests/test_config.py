"""Configuration URLs preserve credential characters without loading local secrets."""

import runpy
from contextlib import nullcontext

import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from edgar_core import config


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("user", "password"),
        ("user@tenant:one", "p@ss:/?#%+ word"),
        ("user name+tag", "100%+ café / secret"),
        ("user", ""),
    ],
)
def test_database_url_preserves_credentials(monkeypatch, username: str, password: str) -> None:
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)
    for key, value in {
        "POSTGRES_USER": username,
        "POSTGRES_PASSWORD": password,
        "DB_HOST": "test-db",
        "DB_PORT": "5438",
        "POSTGRES_DB": "test_dnd",
    }.items():
        monkeypatch.setenv(key, value)

    # Execute config in an isolated namespace; imports elsewhere retain their original values.
    loaded = runpy.run_path(config.__file__)
    url = make_url(loaded["DATABASE_URL"])
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == username
    assert url.password == password
    assert url.host == "test-db"
    assert url.port == 5438
    assert url.database == "test_dnd"
    assert loaded["POSTGRES_USER"] == username
    assert loaded["POSTGRES_PASSWORD"] == password


def test_alembic_accepts_url_encoded_credentials(monkeypatch) -> None:
    from alembic import context

    encoded_url = "postgresql+asyncpg://user%40tenant:p%40ss%25word@test-db:5438/test_dnd"
    monkeypatch.setattr(config, "DATABASE_URL", encoded_url)
    alembic_config = Config()
    configured = {}

    def configure(**kwargs):
        configured.update(kwargs)

    monkeypatch.setattr(context, "config", alembic_config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", configure)
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", lambda: None)
    runpy.run_path(str(config.EDGAR_ROOT / "db" / "alembic" / "env.py"))

    assert alembic_config.get_main_option("sqlalchemy.url") == encoded_url
    assert configured["url"] == encoded_url
    parsed = make_url(configured["url"])
    assert parsed.username == "user@tenant"
    assert parsed.password == "p@ss%word"


def _load_config_with(monkeypatch, **env: str) -> dict:
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runpy.run_path(config.__file__)


def test_redis_url_stays_unauthenticated_without_a_password(monkeypatch) -> None:
    loaded = _load_config_with(monkeypatch, REDIS_HOST="test-redis", REDIS_PORT="6390")

    assert loaded["REDIS_PASSWORD"] == ""
    assert loaded["REDIS_URL"] == "redis://test-redis:6390"


def test_redis_url_carries_an_encoded_password(monkeypatch) -> None:
    from redis import Redis

    password = "p@ss:/?# word%+"
    loaded = _load_config_with(
        monkeypatch, REDIS_HOST="test-redis", REDIS_PORT="6390", REDIS_PASSWORD=password
    )

    # from_url only parses the URL; it does not open a connection.
    kwargs = Redis.from_url(loaded["REDIS_URL"]).connection_pool.connection_kwargs
    assert kwargs["password"] == password
    assert kwargs["host"] == "test-redis"
    assert kwargs["port"] == 6390
