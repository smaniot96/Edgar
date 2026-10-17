"""EDG-066 request, upload, concurrency and provider-spend limits.

Redis is fakeredis and the routes are stubbed around their database calls, so these tests run
without Docker. The stubs never reach Postgres: a real database must never be touched here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import structlog
from fastapi import BackgroundTasks
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request

from apps.api import limits
from apps.api.limits import (
    Admission,
    LimitConfigurationError,
    LimitRejected,
    UploadSizeLimitMiddleware,
    add_rejection_observer,
    admit_job,
    admit_turn,
    limit_rejection_response,
    record_turn_spend,
    reject,
    schedule_job,
    turn_cost_microusd,
    usage_snapshot,
    validate_limit_configuration,
)
from edgar_core import config as limit_config

TURN_GLOBAL = "limits:lease:turns"
JOB_GLOBAL = "limits:lease:jobs"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Deterministic wall clock for window and day arithmetic."""
    state = {"t": 1_000_000.0}
    monkeypatch.setattr(limits, "_now", lambda: state["t"])
    return state


@pytest.fixture(autouse=True)
def _isolated_observers():
    saved = list(limits._rejection_observers)
    yield
    limits._rejection_observers[:] = saved


def _request(ip: str = "203.0.113.1") -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "client": (ip, 1234)}
    )


async def _release_all(*admissions: Admission) -> None:
    for admission in admissions:
        await admission.release()


# --- Rate windows -------------------------------------------------------------------------------


async def test_turn_rate_limit_trips_and_recovers(clock, redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_TURNS_PER_MINUTE", 2)
    for _ in range(2):
        await _release_all(await admit_turn(_request(), redis_client))

    with pytest.raises(LimitRejected) as refused:
        await admit_turn(_request(), redis_client)
    assert refused.value.code == "rate_limited"
    assert refused.value.scope == limits.SCOPE_TURNS
    assert 1 <= refused.value.retry_after <= 60

    # Another client has its own window.
    await _release_all(await admit_turn(_request("198.51.100.7"), redis_client))

    # The window recovers once it has passed.
    clock["t"] += 61
    await _release_all(await admit_turn(_request(), redis_client))


async def test_job_start_limit_is_per_client_and_hourly(clock, redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_JOB_STARTS_PER_HOUR", 1)
    await (await admit_job(_request(), redis_client)).settle()
    with pytest.raises(LimitRejected) as refused:
        await admit_job(_request(), redis_client)
    assert refused.value.code == "rate_limited"
    assert refused.value.scope == limits.SCOPE_JOB_STARTS
    await (await admit_job(_request("198.51.100.9"), redis_client)).settle()
    clock["t"] += 3601
    await (await admit_job(_request(), redis_client)).settle()


# --- Concurrency leases -------------------------------------------------------------------------


async def test_global_turn_cap_refuses_then_frees_on_release(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS", 2)
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT", 2)
    first = await admit_turn(_request("203.0.113.1"), redis_client)
    second = await admit_turn(_request("203.0.113.2"), redis_client)
    with pytest.raises(LimitRejected) as refused:
        await admit_turn(_request("203.0.113.3"), redis_client)
    assert refused.value.code == "concurrency_limited"
    assert refused.value.scope == limits.SCOPE_TURN_CONCURRENCY

    await first.release()
    third = await admit_turn(_request("203.0.113.3"), redis_client)
    await _release_all(second, third)
    assert await redis_client.zcard(TURN_GLOBAL) == 0


async def test_refused_client_lease_does_not_leak_the_global_lease(
    redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS", 5)
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT", 1)
    held = await admit_turn(_request(), redis_client)
    with pytest.raises(LimitRejected) as refused:
        await admit_turn(_request(), redis_client)
    assert refused.value.scope == limits.SCOPE_TURN_CONCURRENCY_CLIENT
    # The refused attempt took and returned its global lease; only the first turn holds one.
    assert await redis_client.zcard(TURN_GLOBAL) == 1
    await held.release()
    assert await redis_client.zcard(TURN_GLOBAL) == 0


async def test_job_lease_is_released_when_the_job_fails(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_JOBS", 1)
    admission = await admit_job(_request(), redis_client)
    assert await redis_client.zcard(JOB_GLOBAL) == 1

    async def failing_job(slug: str) -> None:
        raise RuntimeError("provider said: connection refused at 10.0.0.7")

    background = BackgroundTasks()
    schedule_job(background, admission, failing_job, "slug-a")
    with pytest.raises(RuntimeError):
        await background()
    assert await redis_client.zcard(JOB_GLOBAL) == 0


async def test_job_lease_is_released_on_cancellation(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_JOBS", 1)
    admission = await admit_job(_request(), redis_client)

    async def hanging_job(slug: str) -> None:
        await asyncio.Event().wait()

    background = BackgroundTasks()
    schedule_job(background, admission, hanging_job, "slug-b")
    task = asyncio.create_task(background())
    await asyncio.sleep(0.01)
    assert await redis_client.zcard(JOB_GLOBAL) == 1
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert await redis_client.zcard(JOB_GLOBAL) == 0


async def test_rejected_job_start_returns_its_lease(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_JOBS", 1)
    held = await admit_job(_request("203.0.113.1"), redis_client)
    with pytest.raises(LimitRejected) as refused:
        await admit_job(_request("203.0.113.2"), redis_client)
    assert refused.value.code == "concurrency_limited"
    await held.release()
    assert await redis_client.zcard(JOB_GLOBAL) == 0


# --- Daily provider spend -----------------------------------------------------------------------


async def test_daily_spend_ceiling_refuses_and_resets_next_day(
    clock, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 1_000_000)
    first = await admit_job(_request(), redis_client)
    await first.reserve_daily_spend(600_000)

    second = await admit_job(_request("198.51.100.4"), redis_client)
    with pytest.raises(LimitRejected) as refused:
        await second.reserve_daily_spend(500_000)
    assert refused.value.code == "daily_budget_exhausted"
    assert refused.value.retry_after is not None and refused.value.retry_after > 0
    await second.settle()
    # The refused reservation was not added to today's spend.
    assert int(await redis_client.get(limits._spend_key(clock["t"]))) == 600_000

    await first.reserve_daily_spend(400_000)
    # At the ceiling, even a turn pre-check is refused: it is the fail-closed path.
    with pytest.raises(LimitRejected) as turn_refused:
        await admit_turn(_request(), redis_client)
    assert turn_refused.value.code == "daily_budget_exhausted"

    clock["t"] += 86_400
    await _release_all(await admit_turn(_request(), redis_client))


async def test_settle_refunds_a_reservation_for_work_never_started(
    redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 1_000_000)
    admission = await admit_job(_request(), redis_client)
    await admission.reserve_daily_spend(400_000)
    assert int(await redis_client.get(limits._spend_key(time.time()))) == 400_000
    await admission.settle()
    assert int(await redis_client.get(limits._spend_key(time.time()))) == 0
    assert await redis_client.zcard(JOB_GLOBAL) == 0


async def test_handed_over_job_keeps_its_reservation(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 1_000_000)
    admission = await admit_job(_request(), redis_client)
    await admission.reserve_daily_spend(400_000)

    async def job(slug: str) -> None:
        return None

    background = BackgroundTasks()
    schedule_job(background, admission, job, "slug-c")
    await admission.settle()  # Ownership moved to the background job; nothing is undone here.
    assert int(await redis_client.get(limits._spend_key(time.time()))) == 400_000
    await background()
    assert await redis_client.zcard(JOB_GLOBAL) == 0


def test_turn_cost_charges_unknown_calls_at_the_assumed_cost(monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_ASSUMED_CALL_COST_MICROUSD", 1000)

    class _Collector:
        def to_event(self) -> dict[str, Any]:
            return {
                "payload": {
                    "totals": {"cost_microusd": 300, "calls_unknown_cost": 2, "calls_dropped": 1}
                }
            }

    assert turn_cost_microusd(None) == 0
    assert turn_cost_microusd(_Collector()) == 300 + 3 * 1000


async def test_recorded_turn_spend_reaches_the_daily_counter(redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_ASSUMED_CALL_COST_MICROUSD", 1000)

    class _Collector:
        def to_event(self) -> dict[str, Any]:
            return {"payload": {"totals": {"cost_microusd": 250, "calls_unknown_cost": 1}}}

    await record_turn_spend(redis_client, _Collector())
    assert int(await redis_client.get(limits._spend_key(time.time()))) == 1250


# --- Redis outage policy ------------------------------------------------------------------------


class _BrokenRedis:
    """Every Redis call fails the way a refused connection does."""

    def pipeline(self, transaction: bool = True) -> Any:
        raise RedisConnectionError("connection refused")

    async def zremrangebyscore(self, *args: Any) -> None:
        raise RedisConnectionError("connection refused")

    async def zrem(self, *args: Any) -> None:
        raise RedisConnectionError("connection refused")


async def test_redis_outage_fails_rate_and_concurrency_open(monkeypatch) -> None:
    fresh = structlog.get_logger()
    monkeypatch.setattr(limits, "log", fresh)
    with structlog.testing.capture_logs() as logs:
        await limits._enforce_rate(_BrokenRedis(), "turns", "ip:x", 1, 60)
        admission = Admission(redis=_BrokenRedis(), principal="ip:x")
        await limits._lease_or_reject(admission, "turn_concurrency", "k", 1, 10)
        await limits.record_spend(_BrokenRedis(), 100)
        await admission.release()
    assert admission.leases == []
    open_events = [e for e in logs if e["event"] == "limits_backend_unavailable"]
    assert {e["policy"] for e in open_events} == {"fail_open"}


async def test_redis_outage_fails_the_spend_ceiling_closed(monkeypatch) -> None:
    fresh = structlog.get_logger()
    monkeypatch.setattr(limits, "log", fresh)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(LimitRejected) as refused:
            await limits._reserve_daily_spend(_BrokenRedis(), 0)
    assert refused.value.code == "limits_unavailable"
    assert any(e["policy"] == "fail_closed" for e in logs)
    assert limit_rejection_response(refused.value).status_code == 503


# --- Safe response shape and observers ----------------------------------------------------------

_SAFE_KEYS = {
    "code",
    "category",
    "message",
    "detail",
    "recovery_action",
    "retryable",
    "support_ref",
    "outcome",
    "retry_after_seconds",
}


def test_limit_response_is_uniform_and_leaks_nothing() -> None:
    response = limit_rejection_response(reject(limits.SCOPE_TURNS, "rate_limited", retry_after=12))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    body = json.loads(response.body)
    assert set(body) == _SAFE_KEYS
    assert body["code"] == "rate_limited"
    assert body["outcome"] == "not_committed"
    assert body["retryable"] is True
    assert body["retry_after_seconds"] == 12
    text = json.dumps(body)
    for leaked in ("Traceback", "redis", "limits:", "ip:", "connection"):
        assert leaked not in text


def test_rejection_observers_are_notified_and_never_break_the_refusal() -> None:
    seen: list[tuple[str, str]] = []

    def broken(scope: str, code: str) -> None:
        raise RuntimeError("observer bug")

    add_rejection_observer(broken)
    add_rejection_observer(lambda scope, code: seen.append((scope, code)))
    exc = reject(limits.SCOPE_UPLOAD_SIZE, "upload_too_large")
    assert isinstance(exc, LimitRejected)
    assert seen == [(limits.SCOPE_UPLOAD_SIZE, "upload_too_large")]


async def test_upload_size_middleware_refuses_before_the_app_runs(monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_MAX_UPLOAD_BYTES", 1024)
    monkeypatch.setattr(limits, "_MULTIPART_OVERHEAD_BYTES", 0)
    reached: list[str] = []

    async def inner(scope, receive, send) -> None:
        reached.append(scope["path"])

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    def scope(length: int) -> dict[str, Any]:
        return {
            "type": "http",
            "method": "POST",
            "path": "/api/adventures/upload",
            "headers": [(b"content-length", str(length).encode())],
        }

    middleware = UploadSizeLimitMiddleware(inner)
    await middleware(scope(2048), receive, send)
    assert reached == []
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["code"] == "upload_too_large"

    await middleware(scope(100), receive, send)
    assert reached == ["/api/adventures/upload"]


def test_limit_configuration_rejects_contradictions_naming_settings_only(monkeypatch) -> None:
    assert validate_limit_configuration()["max_concurrent_turns"] >= 1
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS", 2)
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT", 3)
    with pytest.raises(LimitConfigurationError, match="LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT"):
        validate_limit_configuration()


def test_limit_settings_parse_strictly_and_never_echo_values(monkeypatch) -> None:
    from edgar_core.config import _int_setting

    monkeypatch.setenv("LIMIT_TEST_SETTING", "  ")
    assert _int_setting("LIMIT_TEST_SETTING", 7, minimum=1) == 7
    monkeypatch.setenv("LIMIT_TEST_SETTING", "12")
    assert _int_setting("LIMIT_TEST_SETTING", 7, minimum=1) == 12
    monkeypatch.setenv("LIMIT_TEST_SETTING", "not-a-number-secret")
    with pytest.raises(ValueError, match="LIMIT_TEST_SETTING must be an integer") as bad:
        _int_setting("LIMIT_TEST_SETTING", 7, minimum=1)
    assert "secret" not in str(bad.value)
    monkeypatch.setenv("LIMIT_TEST_SETTING", "0")
    with pytest.raises(ValueError, match="must be at least 1"):
        _int_setting("LIMIT_TEST_SETTING", 7, minimum=1)


async def test_usage_snapshot_reports_totals_without_client_identifiers(
    redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 1_000_000)
    held = await admit_turn(_request("203.0.113.55"), redis_client)
    snapshot = await usage_snapshot(redis_client)
    assert snapshot["scope"] == "limits_usage"
    assert snapshot["turns"]["active"] == 1
    assert snapshot["spend"]["daily_limit_microusd"] == 1_000_000
    assert "203.0.113.55" not in json.dumps(snapshot)
    assert "ip:" not in json.dumps(snapshot)
    await held.release()


# --- Routes (no database) -----------------------------------------------------------------------


@pytest_asyncio.fixture
async def limits_client(
    redis_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[AsyncClient]:
    from apps.api.dependencies import get_redis, get_session
    from apps.api.main import app
    from apps.api.routers import adventures
    from apps.api.services import adventures_meta

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventures_meta, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(adventures, "_ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventures, "_PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(adventures, "_all_collection_names", lambda: [])

    async def override_redis() -> AsyncIterator[Any]:
        yield redis_client

    async def override_session() -> AsyncIterator[None]:
        yield None  # Routes under test never reach the database.

    app.dependency_overrides[get_redis] = override_redis
    app.dependency_overrides[get_session] = override_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def _turn_stubs(monkeypatch: pytest.MonkeyPatch, *, run_turn_error: bool = True) -> dict[str, int]:
    from apps.api.routers import session as session_routes

    calls = {"initial": 0, "run": 0}

    async def no_replay(db, session_id, body):
        return None

    async def initial_state(session_id, message, db):
        calls["initial"] += 1
        return {"campaign_id": 1, "turn_id": None}

    async def run_turn(state, diagnostics=None):
        calls["run"] += 1
        if run_turn_error:
            raise RuntimeError("provider said: connection refused at 10.0.0.7")
        return {}

    async def persist_turn(session_id, message, result):
        raise RuntimeError("persistence is stubbed in this test")

    monkeypatch.setattr(session_routes, "_replayed_turn", no_replay)
    monkeypatch.setattr(session_routes, "_load_turn_initial_state", initial_state)
    monkeypatch.setattr(session_routes, "run_turn", run_turn)
    monkeypatch.setattr(session_routes, "persist_turn", persist_turn)
    return calls


async def test_turn_rate_limit_returns_a_safe_429_before_any_turn_work(
    limits_client, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_TURNS_PER_MINUTE", 1)
    calls = _turn_stubs(monkeypatch)

    first = await limits_client.post("/api/sessions/1/turn", json={"message": "look"})
    assert first.status_code != 429
    second = await limits_client.post("/api/sessions/1/turn", json={"message": "look"})
    assert second.status_code == 429
    assert second.headers["retry-after"].isdigit()
    assert set(second.json()) == _SAFE_KEYS
    assert second.json()["code"] == "rate_limited"
    assert calls["initial"] == 1 and calls["run"] == 1


async def test_turn_is_refused_before_provider_work_at_the_daily_ceiling(
    limits_client, redis_client, monkeypatch
) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 1_000)
    await redis_client.set(limits._spend_key(time.time()), 1_000)
    calls = _turn_stubs(monkeypatch)

    response = await limits_client.post("/api/sessions/1/turn", json={"message": "look"})
    assert response.status_code == 429
    assert response.json()["code"] == "daily_budget_exhausted"
    assert calls == {"initial": 0, "run": 0}
    assert await redis_client.zcard(TURN_GLOBAL) == 0


async def test_normal_turn_is_untouched_by_defaults_and_releases_its_lease(
    limits_client, redis_client, monkeypatch
) -> None:
    calls = _turn_stubs(monkeypatch)  # run_turn fails: the lease must still be released.

    response = await limits_client.post("/api/sessions/1/turn", json={"message": "look"})
    assert response.status_code not in (429,)
    assert calls["run"] == 1
    assert await redis_client.zcard(TURN_GLOBAL) == 0
    assert await redis_client.keys("limits:lease:turns:client:*") == []


async def test_stream_turn_releases_its_lease_when_the_client_disconnects(
    redis_client, monkeypatch
) -> None:
    from apps.api.routers import session as session_routes
    from apps.api.schemas.turn import TurnRequest

    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS", 1)
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT", 1)

    async def no_replay(db, session_id, body):
        return None

    async def initial_state(session_id, message, db):
        return {"campaign_id": 1, "turn_id": None}

    async def hanging_stream(**kwargs):
        yield "event: narration\ndata: {}\n\n"
        await asyncio.Event().wait()

    monkeypatch.setattr(session_routes, "_replayed_turn", no_replay)
    monkeypatch.setattr(session_routes, "_load_turn_initial_state", initial_state)
    monkeypatch.setattr(session_routes, "stream_session_turn", hanging_stream)

    response = await session_routes.session_turn_stream(
        session_id=1,
        body=TurnRequest(message="look"),
        auth=None,
        request=_request(),
        db=None,
        redis=redis_client,
    )
    assert await redis_client.zcard(TURN_GLOBAL) == 1
    iterator = response.body_iterator
    await iterator.__anext__()
    # A disconnect closes the stream; the generator's cleanup must free both leases.
    await iterator.aclose()
    assert await redis_client.zcard(TURN_GLOBAL) == 0
    assert await redis_client.get("turn:1") is None


async def test_upload_of_non_pdf_content_is_refused_before_any_work(
    limits_client, redis_client, monkeypatch, tmp_path
) -> None:
    from apps.api.routers import adventures

    ingested: list[str] = []
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *a, **k: ingested.append("x"))

    response = await limits_client.post(
        "/api/adventures/upload",
        files={"file": ("notes.pdf", b"hello, this is not a pdf", "application/pdf")},
    )
    assert response.status_code == 415
    assert response.json()["code"] == "upload_unsupported"
    assert ingested == []
    assert not (tmp_path / "adventures").exists() or not any((tmp_path / "adventures").iterdir())


async def test_upload_declaring_a_non_pdf_type_is_refused(limits_client, monkeypatch) -> None:
    from apps.api.routers import adventures

    ingested: list[str] = []
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *a, **k: ingested.append("x"))
    response = await limits_client.post(
        "/api/adventures/upload",
        files={"file": ("notes.pdf", b"%PDF-1.4 body", "text/html")},
    )
    assert response.status_code == 415
    assert ingested == []


async def test_oversized_upload_is_413_before_the_app_runs(
    limits_client, monkeypatch
) -> None:
    from apps.api.routers import adventures

    monkeypatch.setattr(limit_config, "LIMIT_MAX_UPLOAD_BYTES", 1024 * 1024)
    monkeypatch.setattr(limits, "_MULTIPART_OVERHEAD_BYTES", 0)
    ingested: list[str] = []
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *a, **k: ingested.append("x"))
    response = await limits_client.post(
        "/api/adventures/upload",
        files={"file": ("big.pdf", b"%PDF-1.4 " + b"0" * (2 * 1024 * 1024), "application/pdf")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
    assert ingested == []


async def test_upload_past_the_copy_cap_is_413_and_returns_its_reservation(
    limits_client, redis_client, monkeypatch
) -> None:
    from apps.api.routers import adventures

    monkeypatch.setattr(adventures, "_MAX_UPLOAD_BYTES", 1024)
    ingested: list[str] = []
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *a, **k: ingested.append("x"))
    response = await limits_client.post(
        "/api/adventures/upload",
        files={"file": ("big.pdf", b"%PDF-1.4 " + b"0" * 4096, "application/pdf")},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "upload_too_large"
    assert ingested == []
    assert int(await redis_client.get(limits._spend_key(time.time())) or 0) == 0
    assert await redis_client.zcard(JOB_GLOBAL) == 0


async def test_upload_rate_limit_and_job_cap_are_safe_refusals(
    limits_client, redis_client, monkeypatch
) -> None:
    from apps.api.routers import adventures

    ingested: list[str] = []
    monkeypatch.setattr(adventures, "_ingest_adventure", lambda *a, **k: ingested.append("x"))
    monkeypatch.setattr(limit_config, "LIMIT_JOB_STARTS_PER_HOUR", 1)
    pdf = ("ok.pdf", b"%PDF-1.4 small", "application/pdf")

    accepted = await limits_client.post("/api/adventures/upload", files={"file": pdf})
    assert accepted.status_code == 202
    assert ingested == ["x"]
    assert await redis_client.zcard(JOB_GLOBAL) == 0  # The lease was freed when ingest ended.

    refused = await limits_client.post("/api/adventures/upload", files={"file": pdf})
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert ingested == ["x"]

    monkeypatch.setattr(limit_config, "LIMIT_JOB_STARTS_PER_HOUR", 100)
    monkeypatch.setattr(limit_config, "LIMIT_MAX_CONCURRENT_JOBS", 1)
    await redis_client.zadd(JOB_GLOBAL, {"occupied": time.time() + 3600})
    busy = await limits_client.post("/api/adventures/upload", files={"file": pdf})
    assert busy.status_code == 429
    assert busy.json()["code"] == "concurrency_limited"
    assert ingested == ["x"]


async def test_generation_is_refused_at_the_daily_ceiling_before_a_slug_exists(
    limits_client, redis_client, monkeypatch, tmp_path
) -> None:
    from apps.api.services import campaign_generator

    started: list[str] = []

    async def build(*args, **kwargs):
        started.append("x")

    monkeypatch.setattr(campaign_generator, "build_generated_campaign", build)
    monkeypatch.setattr(limit_config, "LIMIT_DAILY_SPEND_MICROUSD", 100_000)
    monkeypatch.setattr(limit_config, "LIMIT_UNBUDGETED_JOB_COST_MICROUSD", 500_000)

    response = await limits_client.post("/api/adventures/generate", json={"size": "small"})
    assert response.status_code == 429
    assert response.json()["code"] == "daily_budget_exhausted"
    assert started == []
    assert not (tmp_path / "adventures").exists() or not any((tmp_path / "adventures").iterdir())


async def test_accepted_generation_reserves_its_budget_and_releases_its_lease(
    limits_client, redis_client, monkeypatch
) -> None:
    from apps.api.services import campaign_generator

    started: list[str] = []

    async def build(*args, **kwargs):
        started.append("x")

    monkeypatch.setattr(campaign_generator, "build_generated_campaign", build)
    response = await limits_client.post("/api/adventures/generate", json={"size": "small"})
    assert response.status_code == 202
    assert started == ["x"]
    assert int(await redis_client.get(limits._spend_key(time.time()))) == (
        limit_config.LIMIT_UNBUDGETED_JOB_COST_MICROUSD
    )
    assert await redis_client.zcard(JOB_GLOBAL) == 0


async def test_usage_endpoint_is_developer_gated(limits_client, monkeypatch) -> None:
    monkeypatch.delenv("EDGAR_DEBUG", raising=False)
    hidden = await limits_client.get("/api/limits/usage")
    assert hidden.status_code == 404

    monkeypatch.setenv("EDGAR_DEBUG", "1")
    shown = await limits_client.get("/api/limits/usage")
    assert shown.status_code == 200
    body = shown.json()
    assert body["scope"] == "limits_usage"
    assert "ip:" not in json.dumps(body)


async def test_usage_endpoint_reports_redis_outage_safely(
    limits_client, monkeypatch
) -> None:
    from apps.api.dependencies import get_redis
    from apps.api.main import app

    async def broken_redis():
        yield _BrokenRedis()

    monkeypatch.setenv("EDGAR_DEBUG", "1")
    app.dependency_overrides[get_redis] = broken_redis
    response = await limits_client.get("/api/limits/usage")
    assert response.status_code == 503
    assert response.json()["code"] == "limits_unavailable"
    assert "connection" not in json.dumps(response.json())


async def test_retry_is_rate_limited_before_its_work(limits_client, redis_client, monkeypatch) -> None:
    monkeypatch.setattr(limit_config, "LIMIT_JOB_STARTS_PER_HOUR", 1)
    first = await limits_client.post("/api/adventures/missing-adventure/retry")
    assert first.status_code != 429  # The saved adventure is absent, so the route itself refuses.
    second = await limits_client.post("/api/adventures/missing-adventure/retry")
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limited"
    assert await redis_client.zcard(JOB_GLOBAL) == 0


# --- PDF upload content check ---------------------------------------------------------------------


def _upload(data: bytes, content_type: str = "application/pdf"):
    import io

    from fastapi import UploadFile
    from starlette.datastructures import Headers

    return UploadFile(
        io.BytesIO(data), filename="x.pdf", headers=Headers({"content-type": content_type})
    )


@pytest.mark.asyncio
async def test_empty_upload_is_left_to_the_routes_own_empty_file_validation():
    await limits.require_pdf_upload(_upload(b""))


@pytest.mark.asyncio
async def test_non_empty_non_pdf_content_is_refused_before_any_work():
    with pytest.raises(limits.LimitRejected) as info:
        await limits.require_pdf_upload(_upload(b"just some text, not a pdf"))
    assert info.value.code == "upload_unsupported"
    await limits.require_pdf_upload(_upload(b"%PDF-1.7 body"))


# --- Principal key (EDG-065) ----------------------------------------------------------------------


def test_client_principal_keys_on_the_authenticated_user_when_there_is_one():
    from apps.api.auth import Principal

    request = _request("203.0.113.9")
    assert limits.client_principal(request) == "ip:203.0.113.9"
    request.state.principal = Principal(user_id=42, token_id=7, is_admin=False)
    assert limits.client_principal(request) == "user:42"
