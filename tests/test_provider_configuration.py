"""Startup provider checks fail early on local settings, never call a provider, and never echo keys."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.api.main import app
from edgar_core.config import EDGAR_ROOT
from edgar_core.model_profiles import ModelProfileError, validate_provider_configuration

SECRET = "sk-test-secret-value"
VALID_ENV = {
    "OPENAI_API_KEY": SECRET,
    "LLM_MODEL": "gpt-4o-mini",
    "LLM_REASONING_EFFORT": "low",
    "EMBEDDING_MODEL": "text-embedding-3-small",
}


def test_valid_configuration_reports_settings_without_credentials() -> None:
    result = validate_provider_configuration(VALID_ENV)

    assert result["availability_checked"] is False
    assert result["profiles"]["play.parser"]["model"] == "gpt-4o-mini"
    assert result["embedding_model"] == "text-embedding-3-small"
    assert result["required_credentials"] == ["OPENAI_API_KEY"]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("api_key", ["", "   ", "sk-your-openai-api-key-here"])
def test_blank_or_placeholder_api_key_is_rejected_without_echoing_it(api_key: str) -> None:
    with pytest.raises(ModelProfileError, match="OPENAI_API_KEY") as error:
        validate_provider_configuration({**VALID_ENV, "OPENAI_API_KEY": api_key})

    assert "sk-your" not in str(error.value)


def test_missing_api_key_is_rejected() -> None:
    environ = {key: value for key, value in VALID_ENV.items() if key != "OPENAI_API_KEY"}

    with pytest.raises(ModelProfileError, match="OPENAI_API_KEY"):
        validate_provider_configuration(environ)


@pytest.mark.parametrize(
    ("overrides", "setting"),
    [
        ({"EMBEDDING_MODEL": "text embedding!"}, "EMBEDDING_MODEL"),
        ({"LLM_REASONING_EFFORT": "maximum"}, "LLM_REASONING_EFFORT"),
    ],
)
def test_invalid_model_setting_is_named_without_the_api_key(
    overrides: dict[str, str], setting: str
) -> None:
    with pytest.raises(ModelProfileError, match=setting) as error:
        validate_provider_configuration({**VALID_ENV, **overrides})

    assert SECRET not in str(error.value)


def test_invalid_profile_file_is_rejected_at_configuration_time(tmp_path: Path) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text("[profiles.play.parser]\ntemperature = 5\n", encoding="utf-8")

    with pytest.raises(ModelProfileError, match="temperature") as error:
        validate_provider_configuration({**VALID_ENV, "MODEL_PROFILES_PATH": str(path)})

    assert SECRET not in str(error.value)


async def test_api_startup_rejects_missing_key_before_background_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")

    with pytest.raises(ModelProfileError, match="OPENAI_API_KEY"):
        async with app.router.lifespan_context(app):
            pass


def test_importing_and_help_never_require_provider_configuration() -> None:
    # An empty key blocks python-dotenv from filling it from a developer's local .env file.
    env = {**os.environ, "OPENAI_API_KEY": ""}
    imported = subprocess.run(
        [sys.executable, "-c", "import apps.api.main, ingestion.main, rules_engine.main"],
        cwd=EDGAR_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    helped = subprocess.run(
        [sys.executable, "-m", "rules_engine.main", "--help"],
        cwd=EDGAR_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert imported.returncode == 0, imported.stderr
    assert helped.returncode == 0, helped.stderr
    assert "Search installed D&D rules" in helped.stdout


def test_cli_reports_missing_key_before_any_provider_work(tmp_path: Path) -> None:
    pdf = tmp_path / "rules.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    env = {**os.environ, "OPENAI_API_KEY": ""}
    commands = [
        [sys.executable, "-m", "rules_engine.main", "ability", "check"],
        [sys.executable, "-m", "ingestion.main", str(pdf)],
    ]

    for command in commands:
        result = subprocess.run(command, cwd=EDGAR_ROOT, env=env, capture_output=True, text=True)

        assert result.returncode == 1, command
        assert "Configuration error: OPENAI_API_KEY" in result.stderr, command
