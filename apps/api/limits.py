"""Request, upload, concurrency and provider-spend limits (EDG-066).

Every counter lives in Redis so all API replicas agree. Checks run before paid or heavy work
starts and answer with the shared safe-error payload (429, 413, 415 or 503) plus `Retry-After`.

Principal: the authenticated user in token mode (EDG-065); in local mode the client's direct peer
address, because the single-user seam (`current_user_id`) returns the same user for every caller
and would put every client in one bucket. Forwarded headers are deliberately not trusted.

Failure policy when Redis is unavailable:
- rate windows and concurrency leases fail open and log `limits_backend_unavailable`;
- daily spending estimates are advisory and never prevent paid work from continuing;
- recording spend after work already ran fails open, so an outage under-counts spend.

Atomicity uses WATCH/MULTI (as `TurnLock` does) so the same code runs against fakeredis.
"""

from __future__ import annotations

import inspect
import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import BackgroundTasks, Request, UploadFile
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError, WatchError
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from edgar_core import config as limit_config
from edgar_core.safe_errors import OutcomeState

from .services.safe_errors import classify_error

log = structlog.get_logger()

_TURN_GLOBAL_KEY = "limits:lease:turns"
_TURN_CLIENT_KEY = "limits:lease:turns:client"
_JOB_GLOBAL_KEY = "limits:lease:jobs"
_SPEND_KEY_PREFIX = "limits:spend"
_SPEND_KEY_TTL_SECONDS = 3 * 24 * 3600
_TURN_WINDOW_SECONDS = 60
_JOB_WINDOW_SECONDS = 3600
_DAY_SECONDS = 86400
# Allowance above the file cap for multipart framing and small form fields.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_UPLOAD_PATH = re.compile(r"/api/(adventures/upload|campaigns/\d+/sources/[^/]+/upload)")
# A PDF header may sit up to 1024 bytes into the file, as readers allow.
_PDF_HEADER = b"%PDF-"
_PDF_HEADER_SEARCH_BYTES = 1024
PDF_CONTENT_TYPES = frozenset({"application/pdf", "application/x-pdf", "application/octet-stream", ""})

# Fixed scope names only. They appear in logs and observer callbacks, never raw client values.
SCOPE_TURNS = "turns"
SCOPE_JOB_STARTS = "job_starts"
SCOPE_TURN_CONCURRENCY = "turn_concurrency"
SCOPE_TURN_CONCURRENCY_CLIENT = "turn_concurrency_client"
SCOPE_JOB_CONCURRENCY = "job_concurrency"
SCOPE_DAILY_SPEND = "daily_spend"
SCOPE_UPLOAD_SIZE = "upload_size"
SCOPE_UPLOAD_TYPE = "upload_type"

_rejection_observers: list[Callable[[str, str], None]] = []


class LimitConfigurationError(ValueError):
    """Raised at startup for limit settings that contradict each other. Names settings only."""


class LimitRejected(Exception):
    """An action was refused before any paid or heavy work started."""

    def __init__(
        self,
        code: str,
        *,
        scope: str,
        retry_after: int | None = None,
        daily_budget: dict[str, int] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.scope = scope
        self.retry_after = retry_after
        # Integer amounts only, for a refused reservation: what was asked and what is left.
        self.daily_budget = daily_budget


def add_rejection_observer(callback: Callable[[str, str], None]) -> None:
    """Register `callback(scope, code)` for every rejection (hook for EDG-139 metrics)."""
    _rejection_observers.append(callback)


def reject(
    scope: str,
    code: str,
    *,
    retry_after: int | None = None,
    daily_budget: dict[str, int] | None = None,
) -> LimitRejected:
    """Build the rejection to raise, notifying observers. Observer failures never escape."""
    for observer in list(_rejection_observers):
        try:
            observer(scope, code)
        except Exception:
            log.warning("limits_observer_failed", scope=scope)
    return LimitRejected(code, scope=scope, retry_after=retry_after, daily_budget=daily_budget)


def limit_rejection_response(exc: LimitRejected) -> JSONResponse:
    """The uniform safe body: the taxonomy payload plus an integer retry hint, never internals."""
    failure = classify_error(exc.code, outcome=OutcomeState.NOT_COMMITTED)
    payload: dict[str, Any] = failure.to_payload()
    headers: dict[str, str] = {}
    if exc.retry_after is not None:
        payload["retry_after_seconds"] = exc.retry_after
        headers["Retry-After"] = str(exc.retry_after)
    if exc.daily_budget is not None:
        payload["daily_budget"] = dict(exc.daily_budget)
    log.warning(
        "request_limited",
        scope=exc.scope,
        code=failure.code,
        status=failure.status_code,
        retry_after_seconds=exc.retry_after,
    )
    return JSONResponse(status_code=failure.status_code, content=payload, headers=headers)


def client_principal(request: Request) -> str:
    """Key for per-client limits: the authenticated user in token mode (EDG-065), otherwise the
    direct peer address (see module docstring)."""
    from .auth import principal_for

    principal = principal_for(request)
    if principal is not None:
        return f"user:{principal.user_id}"
    client = request.client
    return f"ip:{client.host if client is not None else 'unknown'}"


def _now() -> float:
    return time.time()


def _backend_unavailable(scope: str) -> None:
    log.warning("limits_backend_unavailable", scope=scope, policy="fail_open")


async def _enforce_rate(redis: Redis, scope: str, principal: str, limit: int, window: int) -> None:
    """Fixed-window counter. Rejected attempts still count, so a hammering client stays blocked."""
    now = _now()
    window_start = int(now // window) * window
    key = f"limits:rate:{scope}:{principal}:{window_start}"
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window + 5)
            count, _ = await pipe.execute()
    except RedisError:
        _backend_unavailable(scope)
        return
    if int(count) > limit:
        retry_after = max(1, math.ceil(window_start + window - now))
        raise reject(scope, "rate_limited", retry_after=retry_after)


async def _acquire_lease(redis: Redis, key: str, limit: int, lifetime: int) -> str | None:
    """Add a token scored by its expiry if fewer than `limit` live leases exist. Atomic via WATCH."""
    now = _now()
    await redis.zremrangebyscore(key, "-inf", now)
    token = uuid.uuid4().hex
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                if await pipe.zcard(key) >= limit:
                    await pipe.unwatch()
                    return None
                pipe.multi()
                pipe.zadd(key, {token: now + lifetime})
                pipe.expire(key, lifetime + 60)
                await pipe.execute()
                return token
            except WatchError:
                continue


async def _lease_or_reject(
    admission: Admission, scope: str, key: str, limit: int, lifetime: int
) -> None:
    try:
        token = await _acquire_lease(admission.redis, key, limit, lifetime)
    except RedisError:
        _backend_unavailable(scope)
        return
    if token is None:
        raise reject(scope, "concurrency_limited")
    admission.leases.append((key, token))


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y%m%d")


def _spend_key(now: float) -> str:
    return f"{_SPEND_KEY_PREFIX}:{_utc_day(now)}"


async def _reserve_daily_spend(redis: Redis, amount: int) -> str:
    """Record a spending estimate; crossing the estimate only emits a warning."""
    now = _now()
    key = _spend_key(now)
    ceiling = limit_config.LIMIT_DAILY_SPEND_MICROUSD
    try:
        async with redis.pipeline(transaction=True) as pipe:
            while True:
                try:
                    await pipe.watch(key)
                    spent = int(await pipe.get(key) or 0)
                    over = spent >= ceiling if amount == 0 else spent + amount > ceiling
                    if over:
                        log.warning("daily_spend_estimate_exceeded", scope=SCOPE_DAILY_SPEND,
                                    estimated_microusd=spent + amount, advisory_microusd=ceiling)
                    pipe.multi()
                    pipe.incrby(key, amount)
                    pipe.expire(key, _SPEND_KEY_TTL_SECONDS)
                    await pipe.execute()
                    return key
                except WatchError:
                    continue
    except RedisError:
        _backend_unavailable(SCOPE_DAILY_SPEND)
        return ""


async def record_spend(redis: Redis, amount: int) -> None:
    """Add spend that already happened (a negative amount refunds). Fails open: an outage under-counts."""
    if amount == 0:
        return
    key = _spend_key(_now())
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incrby(key, amount)
            pipe.expire(key, _SPEND_KEY_TTL_SECONDS)
            await pipe.execute()
    except RedisError:
        log.warning("limits_spend_record_failed", policy="fail_open")


def turn_cost_microusd(diagnostics: Any) -> int:
    """Estimated cost of one turn from its EDG-053 collector.

    Known call costs are summed. A call with unknown cost, or one that was dropped from the
    collector, is charged the assumed per-call cost, so unknown spend is never counted as zero.
    """
    assumed = limit_config.LIMIT_ASSUMED_CALL_COST_MICROUSD
    if diagnostics is None:
        return 0
    try:
        totals = diagnostics.to_event()["payload"]["totals"]
    except Exception:
        log.warning("limits_turn_cost_unreadable")
        return assumed
    known = int(totals.get("cost_microusd") or 0)
    unknown = int(totals.get("calls_unknown_cost") or 0) + int(totals.get("calls_dropped") or 0)
    return known + unknown * assumed


async def record_turn_spend(redis: Redis, diagnostics: Any) -> None:
    await record_spend(redis, turn_cost_microusd(diagnostics))


async def record_assumed_call(redis: Redis) -> None:
    """Charge one call of unknown cost, e.g. an intro whose usage is not measured."""
    await record_spend(redis, limit_config.LIMIT_ASSUMED_CALL_COST_MICROUSD)


@dataclass(frozen=True, slots=True)
class JobSpend:
    """What a background job's own durable ledger says it has spent (USD micro-units).

    `terminal` is true once the job can spend nothing more: ready, failed, blocked or cancelled.
    """

    spent_microusd: int
    terminal: bool


SpendProbe = Callable[[], JobSpend | None]


@dataclass
class Admission:
    """Leases and one budget reservation held for one admitted action. Never raises on release.

    A job that is handed to a background task (`schedule_job`) owns its leases and reservation
    from then on; `settle` undoes both only for work that never got that far. A job scheduled
    with a spend probe settles its reservation when it ends in a terminal state: the unused part
    is returned and spend above the reservation is added. A job whose spend is unknown or which
    has not reached a terminal state keeps its whole reservation.
    """

    redis: Redis
    principal: str
    leases: list[tuple[str, str]] = field(default_factory=list)
    reserved: int = 0
    reserved_key: str = ""
    handed_over: bool = False

    async def reserve_daily_spend(self, microusd: int) -> None:
        self.reserved_key = await _reserve_daily_spend(self.redis, microusd)
        if self.reserved_key:
            self.reserved += microusd

    async def settle(self) -> None:
        """End of the admitted request: undo the reservation and leases unless a job took them."""
        if not self.handed_over:
            await self.abort()

    async def abort(self) -> None:
        amount, self.reserved = self.reserved, 0
        if amount > 0:
            try:
                await self.redis.incrby(self.reserved_key, -amount)
            except RedisError:
                log.warning("limits_refund_failed", policy="fail_open")
        await self.release()

    async def settle_spend(self, before: JobSpend | None, after: JobSpend | None) -> None:
        """Replace a handed-over reservation with what the job actually spent."""
        if not self.reserved_key:
            return
        if after is None or not after.terminal:
            log.info(
                "limits_reservation_held",
                reserved_microusd=self.reserved,
                reason="spend_unknown" if after is None else "not_terminal",
            )
            return
        # Without a baseline, everything the job's ledger holds is attributed to this attempt.
        baseline = before.spent_microusd if before is not None else 0
        used = max(0, after.spent_microusd - baseline)
        adjustment = used - self.reserved
        if adjustment > 0 and before is None:
            # Earlier attempts' spend cannot be told apart: never charge more than was reserved.
            adjustment = 0
        if adjustment != 0:
            try:
                await self.redis.incrby(self.reserved_key, adjustment)
            except RedisError:
                log.warning("limits_refund_failed", policy="fail_open")
                return
        log.info(
            "limits_reservation_settled",
            reserved_microusd=self.reserved,
            spent_microusd=used,
            released_microusd=max(0, -adjustment),
        )
        self.reserved += adjustment

    async def release(self) -> None:
        leases, self.leases = self.leases, []
        for key, token in leases:
            try:
                await self.redis.zrem(key, token)
            except RedisError:
                # The lease expires on its own after its lifetime.
                log.warning("limits_release_failed", scope=key.rsplit(":", 1)[0])


async def admit_turn(request: Request, redis: Redis) -> Admission:
    """Admit one play turn or intro: per-client rate, daily ceiling, concurrency leases."""
    principal = client_principal(request)
    await _enforce_rate(redis, SCOPE_TURNS, principal, limit_config.LIMIT_TURNS_PER_MINUTE, _TURN_WINDOW_SECONDS)
    await _reserve_daily_spend(redis, 0)
    admission = Admission(redis=redis, principal=principal)
    try:
        await _lease_or_reject(
            admission,
            SCOPE_TURN_CONCURRENCY,
            _TURN_GLOBAL_KEY,
            limit_config.LIMIT_MAX_CONCURRENT_TURNS,
            limit_config.LIMIT_TURN_LEASE_SECONDS,
        )
        await _lease_or_reject(
            admission,
            SCOPE_TURN_CONCURRENCY_CLIENT,
            f"{_TURN_CLIENT_KEY}:{principal}",
            limit_config.LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT,
            limit_config.LIMIT_TURN_LEASE_SECONDS,
        )
    except BaseException:
        await admission.release()
        raise
    return admission


async def admit_job(request: Request, redis: Redis) -> Admission:
    """Admit one background job start: per-client starts per hour and a global job lease.

    The caller reserves the job's budget once it has validated the request, then hands the lease
    to the background task with `schedule_leased`.
    """
    principal = client_principal(request)
    await _enforce_rate(
        redis, SCOPE_JOB_STARTS, principal, limit_config.LIMIT_JOB_STARTS_PER_HOUR, _JOB_WINDOW_SECONDS
    )
    admission = Admission(redis=redis, principal=principal)
    try:
        await _lease_or_reject(
            admission,
            SCOPE_JOB_CONCURRENCY,
            _JOB_GLOBAL_KEY,
            limit_config.LIMIT_MAX_CONCURRENT_JOBS,
            limit_config.LIMIT_JOB_LEASE_SECONDS,
        )
    except BaseException:
        await admission.release()
        raise
    return admission


def job_budget_microusd(declared: int | None) -> int:
    """A job reserves its declared preparation budget, or the unbudgeted default."""
    if declared is not None and declared > 0:
        return declared
    return limit_config.LIMIT_UNBUDGETED_JOB_COST_MICROUSD


async def _read_spend(probe: SpendProbe) -> JobSpend | None:
    try:
        return await run_in_threadpool(probe)
    except Exception:
        log.warning("limits_spend_unreadable")
        return None


async def _run_leased(
    admission: Admission,
    fn: Callable[..., Any],
    *args: Any,
    spend_probe: SpendProbe | None = None,
    **kwargs: Any,
) -> None:
    before = await _read_spend(spend_probe) if spend_probe is not None else None
    try:
        if inspect.iscoroutinefunction(fn):
            await fn(*args, **kwargs)
        else:
            await run_in_threadpool(fn, *args, **kwargs)
    finally:
        try:
            if spend_probe is not None:
                await admission.settle_spend(before, await _read_spend(spend_probe))
        finally:
            await admission.release()


def schedule_job(
    background: BackgroundTasks,
    admission: Admission | None,
    fn: Callable[..., Any],
    *args: Any,
    spend_probe: SpendProbe | None = None,
    **kwargs: Any,
) -> None:
    """Hand a job to the background queue. With an admission, its job lease is released when the
    job finishes, however it ends. Without one (direct callers such as tests), it is plain.

    `spend_probe` reads the job's own spend ledger. It is read before the job starts and after it
    ends, and a job that ended in a terminal state keeps only what it spent of its reservation.
    """
    if admission is None:
        background.add_task(fn, *args, **kwargs)
        return
    admission.handed_over = True
    background.add_task(_run_leased, admission, fn, *args, spend_probe=spend_probe, **kwargs)


def declared_budget_microusd(captured: Any) -> int | None:
    """The preparation budget a captured setup declares (`capture_setup` shape), if any."""
    if not isinstance(captured, dict):
        return None
    request = captured.get("request")
    if not isinstance(request, dict):
        return None
    value = request.get("max_cost_microusd")
    return value if type(value) is int and value > 0 else None


def is_pdf_upload_type(content_type: str | None) -> bool:
    """Declared media type check. Browsers often send octet-stream for a PDF, so it is allowed."""
    if not content_type:
        return True
    return content_type.split(";", 1)[0].strip().lower() in PDF_CONTENT_TYPES


async def require_pdf_upload(file: UploadFile) -> None:
    """Refuse a non-PDF before any copy, reservation or provider work. The content decides too,
    not only the declared type or the file name."""
    if not is_pdf_upload_type(file.content_type):
        raise reject(SCOPE_UPLOAD_TYPE, "upload_unsupported")
    head = await file.read(_PDF_HEADER_SEARCH_BYTES)
    await file.seek(0)
    # An empty file is left to the route's existing empty-file validation (400), so that public
    # status code does not change; non-empty content that is not a PDF is refused here.
    if head and _PDF_HEADER not in head:
        raise reject(SCOPE_UPLOAD_TYPE, "upload_unsupported")


class UploadSizeLimitMiddleware:
    """Refuse an oversized declared upload before multipart parsing spools it to disk.

    Only the declared Content-Length is checked here; the exact file size is still enforced when
    the file is copied, so chunked uploads without a length are bounded there, not here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and _UPLOAD_PATH.fullmatch(scope["path"]):
            declared = _declared_length(scope)
            if declared is not None and declared > limit_config.LIMIT_MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES:
                response = limit_rejection_response(reject(SCOPE_UPLOAD_SIZE, "upload_too_large"))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def validate_limit_configuration() -> dict[str, int]:
    """Startup check (EDG-078 pattern). Raises naming settings only, never their values."""
    if limit_config.LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT > limit_config.LIMIT_MAX_CONCURRENT_TURNS:
        raise LimitConfigurationError(
            "LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT cannot exceed LIMIT_MAX_CONCURRENT_TURNS."
        )
    return limits_settings()


def limits_settings() -> dict[str, int]:
    return {
        "turns_per_minute": limit_config.LIMIT_TURNS_PER_MINUTE,
        "job_starts_per_hour": limit_config.LIMIT_JOB_STARTS_PER_HOUR,
        "max_concurrent_turns": limit_config.LIMIT_MAX_CONCURRENT_TURNS,
        "max_concurrent_turns_per_client": limit_config.LIMIT_MAX_CONCURRENT_TURNS_PER_CLIENT,
        "max_concurrent_jobs": limit_config.LIMIT_MAX_CONCURRENT_JOBS,
        "max_upload_bytes": limit_config.LIMIT_MAX_UPLOAD_BYTES,
        "daily_spend_microusd": limit_config.LIMIT_DAILY_SPEND_MICROUSD,
        "assumed_call_cost_microusd": limit_config.LIMIT_ASSUMED_CALL_COST_MICROUSD,
        "unbudgeted_job_cost_microusd": limit_config.LIMIT_UNBUDGETED_JOB_COST_MICROUSD,
    }


async def usage_snapshot(redis: Redis) -> dict[str, Any]:
    """Current global usage against the configured limits. No per-client values are included."""
    now = _now()
    await redis.zremrangebyscore(_TURN_GLOBAL_KEY, "-inf", now)
    await redis.zremrangebyscore(_JOB_GLOBAL_KEY, "-inf", now)
    turns_active = int(await redis.zcard(_TURN_GLOBAL_KEY))
    jobs_active = int(await redis.zcard(_JOB_GLOBAL_KEY))
    spent = int(await redis.get(_spend_key(now)) or 0)
    ceiling = limit_config.LIMIT_DAILY_SPEND_MICROUSD
    return {
        "scope": "limits_usage",
        "day_utc": _utc_day(now),
        "resets_in_seconds": math.ceil(_DAY_SECONDS - (now % _DAY_SECONDS)),
        "turns": {"active": turns_active, "limit": limit_config.LIMIT_MAX_CONCURRENT_TURNS},
        "jobs": {"active": jobs_active, "limit": limit_config.LIMIT_MAX_CONCURRENT_JOBS},
        "spend": {
            "spent_microusd": spent,
            "daily_limit_microusd": ceiling,
            "remaining_microusd": max(0, ceiling - spent),
        },
        "settings": limits_settings(),
    }


__all__ = [
    "Admission",
    "JobSpend",
    "LimitConfigurationError",
    "LimitRejected",
    "UploadSizeLimitMiddleware",
    "add_rejection_observer",
    "admit_job",
    "admit_turn",
    "client_principal",
    "is_pdf_upload_type",
    "job_budget_microusd",
    "limit_rejection_response",
    "limits_settings",
    "record_assumed_call",
    "record_spend",
    "record_turn_spend",
    "reject",
    "require_pdf_upload",
    "declared_budget_microusd",
    "schedule_job",
    "turn_cost_microusd",
    "usage_snapshot",
    "validate_limit_configuration",
]
