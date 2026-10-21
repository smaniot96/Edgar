from __future__ import annotations

import os
from pathlib import Path

import pytest

from edgar_core.model_profiles import (
    ModelProfileError,
    ModelProfileSnapshot,
    effective_profile,
    load_model_profiles,
    model_profile_snapshot,
)


def test_legacy_environment_remains_play_default_without_a_profile_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("edgar_core.model_profiles.DEFAULT_PROFILE_PATH", tmp_path / "missing.toml")
    snapshot = load_model_profiles(
        None,
        environ={"LLM_MODEL": "gpt-4o-mini", "LLM_REASONING_EFFORT": "low"},
    )

    assert snapshot.source == "environment"
    assert effective_profile("play.parser", snapshot).model == "gpt-4o-mini"
    assert effective_profile("play.narrator", snapshot).model == "gpt-4o-mini"
    assert effective_profile("play.parser", snapshot).temperature is None
    assert effective_profile("campaign.spine", snapshot).model == "gpt-6-luna"
    assert effective_profile("campaign.spine", snapshot).reasoning_effort == "medium"


def test_explicit_missing_profile_does_not_silently_use_defaults(tmp_path: Path) -> None:
    with pytest.raises(ModelProfileError, match="unavailable"):
        load_model_profiles(tmp_path / "missing.toml", environ={})


def test_toml_override_changes_only_its_named_stage_and_image_options_are_separate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        'schema_version = 1\nprofile_version = "test-2"\n'
        "[profiles.play.parser]\nmodel = 'gpt-4.1'\ntemperature = 0.25\n"
        "max_output_tokens = 321\ntimeout_seconds = 17\nretries = 1\n"
        "[images.image.adventure_map]\nsize = '1536x1024'\nquality = 'high'\n",
        encoding="utf-8",
    )
    snapshot = load_model_profiles(
        path,
        environ={"LLM_MODEL": "gpt-4o-mini", "LLM_REASONING_EFFORT": "low"},
    )

    parser = effective_profile("play.parser", snapshot)
    planner = effective_profile("play.consequence_planner", snapshot)
    assert (parser.model, parser.temperature, parser.max_output_tokens) == (
        "gpt-4.1",
        0.25,
        321,
    )
    assert (parser.timeout_seconds, parser.retries) == (17, 1)
    assert planner.model == "gpt-4o-mini"
    assert planner.temperature is None
    image = snapshot.image_profiles["image.adventure_map"]
    assert image.size == "1536x1024" and image.quality == "high"
    assert snapshot.image_profiles["image.illustration"].size == "1024x1024"


@pytest.mark.parametrize(
    "body",
    [
        "[profiles.campaign.spine]\nmodel = 'gpt-6.1-sol'\n",
        "[profiles.campaign.chapters]\nreasoning_effort = 'low'\n",
        "[profiles.play.narrator]\nmodel = 'gpt-6.1-sol'\ntemperature = 0.5\n",
        "[profiles.play.parser]\nretries = 99\n",
    ],
)
def test_unsupported_profile_combinations_fail_with_safe_actionable_errors(
    tmp_path: Path, body: str
) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(f'profile_version = "bad"\n{body}', encoding="utf-8")

    with pytest.raises(ModelProfileError) as error:
        load_model_profiles(path, environ={"LLM_MODEL": "gpt-4o-mini"})
    assert "bad.toml" not in str(error.value)
    assert "sk-" not in str(error.value)


def test_snapshot_round_trips_and_scoped_context_ignores_environment_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profiles.toml"
    path.write_text(
        "profile_version = \"snapshot-a\"\n[profiles.play.parser]\nmodel = 'gpt-4.1'\n",
        encoding="utf-8",
    )
    snapshot = load_model_profiles(
        path,
        environ={"LLM_MODEL": "gpt-4o-mini", "LLM_REASONING_EFFORT": "low"},
    )
    restored = ModelProfileSnapshot.from_dict(snapshot.to_dict())
    assert restored.to_dict() == snapshot.to_dict()
    assert restored.digest == snapshot.digest

    with model_profile_snapshot(restored):
        monkeypatch.setenv("LLM_MODEL", "gpt-4o")
        assert effective_profile("play.parser").model == "gpt-4.1"
    assert os.environ["LLM_MODEL"] == "gpt-4o"


def test_chatopenai_receives_profile_options_and_locked_responses_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import agent.llm as llm

    calls: list[dict] = []

    class ProviderSpy:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(llm, "ChatOpenAI", ProviderSpy)
    monkeypatch.setattr(llm, "OPENAI_API_KEY", "test-key-for-edg-141")
    path = tmp_path / "profiles.toml"
    path.write_text(
        'profile_version = "caps"\n'
        "[profiles.play.parser]\nmax_output_tokens = 800\n"
        "[profiles.campaign.chapters]\nmax_output_tokens = 7000\n",
        encoding="utf-8",
    )
    text_snapshot = load_model_profiles(
        path,
        environ={"LLM_MODEL": "gpt-4o-mini", "LLM_REASONING_EFFORT": "low"},
    )
    llm.make_chat_model(profile="play.parser", profiles=text_snapshot)
    assert calls[-1] == {
        "api_key": "test-key-for-edg-141",
        "timeout": 60.0,
        "max_retries": 3,
        "model": "gpt-4o-mini",
        "temperature": 0,
        "max_tokens": 800,
    }

    llm.make_chat_model(profile="play.parser", max_tokens=200, profiles=text_snapshot)
    assert calls[-1]["max_tokens"] == 200

    llm.make_chat_model(profile="campaign.chapters", max_tokens=1800, profiles=text_snapshot)
    assert calls[-1]["model"] == "gpt-6-luna"
    assert calls[-1]["reasoning_effort"] == "medium"
    assert calls[-1]["use_responses_api"] is True
    assert "temperature" not in calls[-1]
    assert calls[-1]["max_tokens"] == 1800


def test_model_profile_read_errors_do_not_disclose_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sk-private-profile-location.toml"
    path.write_text("profile_version = 'private'\n", encoding="utf-8")
    original_open = Path.open

    def fail_profile_open(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError("private path details")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_profile_open)
    with pytest.raises(ModelProfileError) as error:
        load_model_profiles(path, environ={"LLM_MODEL": "gpt-4o-mini"})

    assert "sk-private-profile-location" not in str(error.value)
    assert "private path details" not in str(error.value)
    assert "PermissionError" in str(error.value)
