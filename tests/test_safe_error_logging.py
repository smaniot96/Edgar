"""Provider and CLI failures log a stable error summary, never the provider's message text."""

import logging
import subprocess
import sys
from pathlib import Path

from agent.nodes import input_parser, rules_adjudicator
from edgar_core.log_safety import error_summary

PRIVATE_PLAYER_TEXT = "I whisper to the innkeeper that the duke is secretly the thief"
API_KEY = "sk-examplekeyabcdefghijkl"


class ProviderBadRequest(Exception):
    """Shaped like an OpenAI status error: class, status and request id are safe metadata."""

    status_code = 400
    request_id = "req_provider123"


def _provider_error() -> ProviderBadRequest:
    return ProviderBadRequest(f"Invalid value for prompt '{PRIVATE_PLAYER_TEXT}' with {API_KEY}")


class _FailingStructuredModel:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def with_structured_output(self, *_args, **_kwargs) -> "_FailingStructuredModel":
        return self

    async def ainvoke(self, _messages) -> None:
        raise self.error


def _logged_fields(caplog) -> str:
    # Structured extras live on the record, not in the formatted message.
    return "\n".join(f"{record.getMessage()} {vars(record)}" for record in caplog.records)


def test_error_summary_keeps_category_status_and_request_id_only() -> None:
    assert error_summary(_provider_error()) == (
        "ProviderBadRequest status=400 provider_request_id=req_provider123"
    )


def test_error_summary_drops_request_id_that_is_not_a_plain_token() -> None:
    error = ProviderBadRequest("provider failure")
    error.request_id = f"req {PRIVATE_PLAYER_TEXT}"
    assert error_summary(error) == "ProviderBadRequest status=400"


async def test_input_parser_fallback_logs_summary_not_provider_message(monkeypatch, caplog) -> None:
    monkeypatch.setattr(input_parser, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        input_parser, "make_chat_model", lambda **_kw: _FailingStructuredModel(_provider_error())
    )
    caplog.set_level(logging.WARNING)

    out = await input_parser.input_parser_node({"player_input": PRIVATE_PLAYER_TEXT})

    assert out["parsed_input"].intent == "exploration"
    logged = _logged_fields(caplog)
    assert "input_parser_fallback" in logged
    assert "ProviderBadRequest status=400 provider_request_id=req_provider123" in logged
    assert PRIVATE_PLAYER_TEXT not in logged
    assert API_KEY not in logged


async def test_adjudicator_retry_logs_summary_not_provider_message(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        rules_adjudicator,
        "make_chat_model",
        lambda **_kw: _FailingStructuredModel(_provider_error()),
    )
    caplog.set_level(logging.WARNING)

    assert await rules_adjudicator._invoke_with_retry([]) is None

    logged = _logged_fields(caplog)
    assert "adjudicator_structured_output_failed" in logged
    assert "ProviderBadRequest status=400 provider_request_id=req_provider123" in logged
    assert PRIVATE_PLAYER_TEXT not in logged
    assert API_KEY not in logged


def test_ingestion_cli_failure_and_sink_redaction_hide_provider_message(tmp_path: Path) -> None:
    # Loguru and the CLI's sink configuration live in process state, so run the CLI isolated.
    pdf = tmp_path / "rules.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    source = f"""
import sys
import ingestion.main as cli
from loguru import logger

class ProviderBadRequest(Exception):
    status_code = 400
    request_id = "req_cli123"

def fail(*_args, **_kwargs):
    logger.warning("embedding key {{}}", {API_KEY!r})
    raise ProviderBadRequest({PRIVATE_PLAYER_TEXT!r})

cli.validate_provider_configuration = lambda: None
cli.run_pipeline = fail
sys.argv = ["ingest", {str(pdf)!r}]
cli.main()
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)

    assert result.returncode == 1
    assert "Ingestion failed: ProviderBadRequest status=400 provider_request_id=req_cli123" in (
        result.stderr
    )
    assert "embedding key [REDACTED]" in result.stderr
    assert PRIVATE_PLAYER_TEXT not in result.stderr
    assert API_KEY not in result.stderr
