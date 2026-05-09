"""Structlog setup (plan 09 referenced `logging.py`; this name avoids shadowing stdlib `logging`)."""

import logging
import sys

import structlog

from edgar_core.config import ENV


def configure_logging() -> None:
    """JSON logs outside ENV=local; console renderer for local dev."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if ENV == "local":
        processors = [*shared_processors, structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())]
    else:
        processors = [*shared_processors, structlog.processors.JSONRenderer()]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)
