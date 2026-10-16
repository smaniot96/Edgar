"""One correlated logging pipeline for API, agent, ingestion and server logs."""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Mapping
from traceback import TracebackException
from typing import Any

import structlog
from loguru import logger as ingestion_logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from edgar_core.config import ENV
from edgar_core.log_safety import REDACTED, REQUEST_ID, redact_text

_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "cookie",
        "set_cookie",
        "password",
        "api_key",
        "openai_api_key",
        "x_api_key",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
    }
)
_handler: logging.Handler | None = None


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if str(key).lower().replace("-", "_") in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_sensitive_data(
    _logger: Any, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Redact structured credentials and common credential forms, including tracebacks.

    This runs after exception formatting. It deliberately keeps stack frames but never
    enables traceback locals; call sites must still avoid logging request bodies/prompts.
    """
    return _redact(event_dict)


def _format_exception(exc_info: structlog.types.ExcInfo) -> str:
    """Redact each exception message before joining traceback chains/groups.

    TracebackException yields each exception's complete message as its own piece. A
    PostgreSQL DETAIL may contain arbitrary blank/new lines, so discard the remainder
    of that message rather than guessing where row data ends in the joined traceback.
    Other exceptions and stack frames remain intact; locals are never captured.
    """
    trace = TracebackException(*exc_info, capture_locals=False)
    return "".join(_redact(piece) for piece in trace.format()).rstrip()


format_safe_exception = structlog.processors.ExceptionRenderer(_format_exception)


def request_id_from_header(value: str | None) -> str:
    """Accept bounded, printable proxy correlation ids; mint an id for malformed input."""
    return value if value and REQUEST_ID.fullmatch(value) else str(uuid.uuid4())


class RequestContextMiddleware:
    """Keep request context for the entire ASGI response, including streamed/background work."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = request_id_from_header(Headers(scope=scope).get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        # Reset just our binding when done, preserving any context provided by the caller.
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            await self.app(scope, receive, send_with_request_id)


def _forward_ingestion_log(message: Any) -> None:
    record = message.record
    exception = record["exception"]
    logging.getLogger(record["name"] or "ingestion").log(
        record["level"].no,
        record["message"],
        exc_info=(exception.type, exception.value, exception.traceback) if exception else None,
        extra={"loguru_extra": record["extra"]} if record["extra"] else None,
    )


def configure_logging() -> None:
    """JSON outside local development; one renderer for structlog, stdlib and Loguru.

    Repeated lifespans replace only our handler, retaining host/test capture handlers.
    The ingestion CLI configures Loguru independently in its own process.
    """
    global _handler
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer = (
        structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
        if ENV == "local"
        else structlog.processors.JSONRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            format_safe_exception,
            redact_sensitive_data,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
        _handler.close()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    _handler = handler
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    ingestion_logger.remove()
    ingestion_logger.add(_forward_ingestion_log, level="INFO", backtrace=False, diagnose=False)
