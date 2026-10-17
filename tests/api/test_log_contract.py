"""Static and behavioural checks on what call sites may put into operational logs (EDG-063).

Static: every structured log call in the production packages is scanned for forbidden keyword
arguments and `extra=` keys. Behavioural: the few paths that log under a provider or config failure
are checked for their fixed fields.
"""

import ast
from pathlib import Path

import pytest

from apps.api import main
from apps.api.logging_config import FORBIDDEN_LOG_FIELDS
from apps.api.routers import health
from edgar_core.model_profiles import ModelProfileError

REPO = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = ("apps", "core/src", "db", "tools")
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
LOG_NAMES = {"log", "logger", "_log", "LOG"}
LOG_HELPERS = {"log_failure"}  # apps/api/services/safe_errors.py forwards kwargs to the logger


def _normal(key: str) -> str:
    return key.lower().replace("-", "_")


def _forbidden_log_keys(source: str) -> list[tuple[int, str]]:
    """(line, key) for every log call that passes a forbidden keyword or `extra` key."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not _is_log_call(node.func):
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                continue  # **mapping: checked by the runtime processor, not statically
            if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                keys = [k.value for k in keyword.value.keys if isinstance(k, ast.Constant)]
                found.extend(
                    (node.lineno, k) for k in keys if _normal(str(k)) in FORBIDDEN_LOG_FIELDS
                )
            elif _normal(keyword.arg) in FORBIDDEN_LOG_FIELDS:
                found.append((node.lineno, keyword.arg))
    return found


def _is_log_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in LOG_HELPERS
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in LOG_HELPERS:
        return True
    if func.attr not in LOG_METHODS:
        return False
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id in LOG_NAMES
    # logging.getLogger(__name__).warning(...), structlog.get_logger().info(...)
    return (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Attribute)
        and (receiver.func.attr in {"getLogger", "get_logger"})
    )


def _production_sources() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            parts = set(path.relative_to(REPO).parts)
            if parts & {".venv", "tests", "node_modules", "__pycache__"}:
                continue
            files.append(path)
    return files


def test_static_scan_detects_forbidden_fields_in_synthetic_calls() -> None:
    # The scanner must find what it claims to find, or the repository scan below proves nothing.
    source = """
import logging
log = logging.getLogger(__name__)
log.info("a", narration="prose")
log.warning("b", extra={"Prompt": "prose", "chunks": 1})
logging.getLogger(__name__).error("c", api_key="placeholder")
structlog.get_logger().info("d", player_input="prose")
log_failure("e", failure, narration="prose")
log.info("ok", chunks=1, extra={"monster_name": "Goblin"})
other.info("not a logger", narration="prose")
"""
    assert [key for _line, key in _forbidden_log_keys(source)] == [
        "narration",
        "Prompt",
        "api_key",
        "player_input",
        "narration",
    ]


def test_production_log_calls_never_pass_forbidden_fields() -> None:
    sources = _production_sources()
    assert len(sources) > 100  # guards against the scan silently finding nothing
    violations = {}
    for path in sources:
        found = _forbidden_log_keys(path.read_text(encoding="utf-8"))
        if found:
            violations[str(path.relative_to(REPO))] = found
    assert violations == {}


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def _record(self, level: str):
        def record(event: str, **fields) -> None:
            self.events.append((f"{level}:{event}", fields))

        return record

    def __getattr__(self, name: str):
        return self._record(name)


async def test_provider_configuration_failure_emits_one_structured_event(monkeypatch) -> None:
    from fastapi import FastAPI

    recorder = _Recorder()
    secret_like_setting = "OPENAI_API_KEY value rejected: example-not-a-real-key-value"

    def reject(*_args, **_kwargs):
        raise ModelProfileError(secret_like_setting)

    monkeypatch.setattr(main, "log", recorder)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "validate_provider_configuration", reject)

    with pytest.raises(ModelProfileError):
        async with main.lifespan(FastAPI()):
            pass

    # Other startup events (for example the auth mode) may come first; this test is about the
    # provider failure, which must be exactly one structured event naming only the error class.
    provider_events = [e for e in recorder.events if "provider_configuration" in e[0]]
    assert provider_events == [
        ("error:provider_configuration_invalid", {"error_class": "ModelProfileError"})
    ]
    assert "example-not-a-real-key-value" not in repr(recorder.events)


async def test_readiness_failure_logs_error_class_not_exception_text(monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(health, "log", recorder)

    async def failing_check() -> None:
        raise ConnectionError("provider said: connection refused at 10.0.0.7")

    name, status = await health._probe("qdrant", failing_check())
    assert (name, status) == ("qdrant", "unavailable")
    assert recorder.events == [
        ("warning:readiness_check_failed", {"component": "qdrant", "error": "ConnectionError"})
    ]
    assert "10.0.0.7" not in repr(recorder.events)


def test_narration_roster_log_labels_never_include_prose() -> None:
    from agent.combat_narration import _roster_problem_labels

    combatants = [
        {"display_name": "Raider", "is_player": False},
        {"display_name": "Raider Captain", "is_player": False},
        {"name": "Eda", "is_player": True},
    ]
    problems = ["Raider", "another raider", "A second", "Raider Captain"]
    labels = _roster_problem_labels(problems, combatants)
    assert labels == ["Raider", "unnamed_attacker", "unnamed_attacker", "Raider Captain"]
    assert "second" not in " ".join(labels)
