"""Operational logging must remain correlated and safe across all logging libraries."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
import structlog
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from apps.api.logging_config import RequestContextMiddleware, request_id_from_header


def test_all_logging_libraries_use_json_context_and_redaction() -> None:
    # Logging has process-wide state, including Loguru sinks and cached structlog proxies.
    # Exercise the actual stdout sink in isolation rather than disturbing other API tests.
    source = """
import logging
import structlog
from loguru import logger
import apps.api.logging_config as config
config.ENV = "production"
config.configure_logging()
config.configure_logging()
structlog.contextvars.bind_contextvars(request_id="request-123")
logging.getLogger("agent.test").warning("fallback %s", "ok", extra={"character_id": 42})
structlog.get_logger("api.test").info("accepted", credentials={"api-key": "example-secret"})
logger.bind(password="example-secret").warning("ingestion warning")
try:
    local_secret = "should-never-appear-in-logs"
    raise RuntimeError("postgresql://private-user:private-password@localhost/db Bearer abc.def sk-exampleabcdefghijklmnop")
except RuntimeError:
    logging.getLogger("db.test").exception("query_failed")
    logger.exception("ingestion_failed")
logging.getLogger("agent.test").debug("debug_not_enabled")
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "OPENAI_API_KEY": "test-key-for-ci"},
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 5  # repeated configuration does not add duplicate handlers
    assert all(event["request_id"] == "request-123" for event in events)
    assert all(event["timestamp"].endswith("Z") and event["logger"] for event in events)
    assert events[0]["event"] == "fallback ok" and events[0]["character_id"] == 42
    assert events[1]["credentials"]["api-key"] == "[REDACTED]"
    assert events[2]["loguru_extra"]["password"] == "[REDACTED]"
    assert all("RuntimeError" in event["exception"] for event in events[3:])
    for secret in (
        "example-secret",
        "private-user",
        "private-password",
        "abc.def",
        "sk-exampleabcdefghijklmnop",
        "should-never-appear-in-logs",
    ):
        assert secret not in result.stdout
    assert "debug_not_enabled" not in result.stdout
    assert not result.stderr


@pytest.mark.parametrize("value", [None, "", "a" * 129, "bad id", "bad\nid", "é"])
def test_invalid_request_id_is_replaced(value) -> None:
    generated = request_id_from_header(value)
    assert str(uuid.UUID(generated)) == generated
    assert uuid.UUID(generated).version == 4


def test_proxy_request_id_is_preserved() -> None:
    assert request_id_from_header("proxy.req-123:4") == "proxy.req-123:4"


async def test_request_context_survives_streaming_background_and_concurrent_requests() -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    observed = []

    async def capture(stage: str, expected: str) -> None:
        observed.append((stage, expected, structlog.contextvars.get_contextvars()))

    @app.get("/stream/{name}")
    async def stream(name: str):
        await run_in_threadpool(
            lambda: observed.append(("thread", name, structlog.contextvars.get_contextvars()))
        )

        async def body():
            await capture("first", name)
            yield b"first\n"
            await asyncio.sleep(0)
            await capture("last", name)
            yield b"last\n"

        return StreamingResponse(body(), background=BackgroundTask(capture, "background", name))

    with structlog.contextvars.bound_contextvars(request_id="outer", run_id="test-run"):
        async with AsyncClient(transport=ASGITransport(app), base_url="http://test") as client:
            first, second = await asyncio.gather(
                *(
                    client.get(f"/stream/{name}", headers={"X-Request-ID": name})
                    for name in ("first-request", "second-request")
                )
            )
        assert structlog.contextvars.get_contextvars()["request_id"] == "outer"
    assert first.headers["x-request-id"] == "first-request"
    assert second.headers["x-request-id"] == "second-request"
    assert len(observed) == 8
    assert all(
        context["request_id"] == expected and context["run_id"] == "test-run"
        for _stage, expected, context in observed
    )


async def test_unhandled_error_has_matching_safe_request_id_in_header_and_body() -> None:
    from apps.api.main import unhandled_exception_handler

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/crash")
    async def crash(request: Request):
        raise RuntimeError("private error detail")

    transport = ASGITransport(app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/crash", headers={"X-Request-ID": "invalid id"})
    assert response.status_code == 500
    request_id = response.json()["request_id"]
    assert response.headers["x-request-id"] == request_id
    assert uuid.UUID(request_id).version == 4
    assert "private error detail" not in response.text


async def test_lifespan_disposes_database_even_when_serving_or_redis_close_fails(
    monkeypatch,
) -> None:
    from apps.api import dependencies, main
    from apps.api.services import adventures_meta
    from db.postgres import session as session_module

    calls = []

    async def close_redis():
        calls.append("redis")
        raise RuntimeError("redis cleanup failed")

    async def dispose():
        calls.append("postgres")

    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(adventures_meta, "fail_stale_processing", lambda: [])
    monkeypatch.setattr(dependencies, "close_redis", close_redis)
    monkeypatch.setattr(session_module, "engine", SimpleNamespace(dispose=dispose))
    with pytest.raises(RuntimeError, match="redis cleanup failed"):
        async with main.lifespan(main.app):
            raise RuntimeError("serving failed")
    assert calls == ["redis", "postgres"]
