"""EDG-059: the prompt manifest is stable, and changing prompt text requires a version bump."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent import prompt_registry
from agent.prompt_registry import PROMPTS, PromptEntry, manifest_drift, prompt_manifest
from agent.prompts import INPUT_PARSER, NARRATOR, RULES_ADJUDICATOR
from apps.api.services.session_intro import INTRO_SYSTEM

GOLDEN = Path(__file__).resolve().parents[1] / "evals" / "prompt_manifest.golden.json"

EXPECTED_IDS = {
    "play.parser",
    "play.consequence_planner",
    "play.narrator",
    "play.combat_builder",
    "play.combat_narrator",
    "play.memory_summarizer",
    "play.session_intro",
}


def _golden() -> dict[str, dict[str, str]]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_registry_covers_the_play_prompts_with_semver_versions() -> None:
    manifest = prompt_manifest()
    assert set(manifest) == EXPECTED_IDS
    for entry in manifest.values():
        assert set(entry) == {"version", "sha256"}
        assert len(entry["sha256"]) == 64
        assert all(part.isdigit() for part in entry["version"].split("."))


def test_registry_text_is_the_runtime_constant_not_a_copy() -> None:
    by_id = {p.id: p.text for p in PROMPTS}
    assert by_id["play.parser"] is INPUT_PARSER
    assert by_id["play.consequence_planner"] is RULES_ADJUDICATOR
    assert by_id["play.narrator"] is NARRATOR
    assert by_id["play.combat_narrator"] is NARRATOR
    assert by_id["play.session_intro"] is INTRO_SYSTEM


def test_committed_golden_manifest_matches_current_prompts() -> None:
    problems = manifest_drift(prompt_manifest(), _golden())
    assert problems == [], (
        "prompt text or version drifted from evals/prompt_manifest.golden.json. Bump the changed "
        "prompt's version in apps/agent/src/agent/prompt_registry.py, then refresh the golden file "
        "with: .venv/bin/python -m tools.experiments.runner manifest --write\n"
        + "\n".join(problems)
    )


def test_changed_text_without_version_bump_is_a_violation() -> None:
    current = prompt_manifest()
    golden = _golden()
    version = current["play.parser"]["version"]
    changed = PromptEntry("play.parser", "input parser", version, INPUT_PARSER + " ")
    patched = {**current, "play.parser": {"version": changed.version, "sha256": changed.sha256}}
    problems = manifest_drift(patched, golden)
    assert any(f"text changed but version is still {version}" in p for p in problems)


def test_bumped_version_with_changed_text_is_only_a_refresh_request() -> None:
    golden = _golden()
    # The next patch version of whatever the registry ships now, so a later bump does not break this.
    shipped = prompt_manifest()["play.narrator"]["version"]
    major, minor, patch = (int(part) for part in shipped.split("."))
    bumped = f"{major}.{minor}.{patch + 1}"
    changed = PromptEntry("play.narrator", "narrator", bumped, NARRATOR + " ")
    current = {**prompt_manifest(), "play.narrator": {"version": bumped, "sha256": changed.sha256}}
    problems = manifest_drift(current, golden)
    assert problems == ["play.narrator: differs from the golden manifest (refresh it)"]


def test_invalid_version_is_rejected() -> None:
    with pytest.raises(ValueError):
        PromptEntry("play.parser", "x", "v1", "text")
    with pytest.raises(ValueError):
        replace(PROMPTS[0], version="1.0")


def test_manifest_is_deterministic_across_calls() -> None:
    assert prompt_manifest() == prompt_manifest()
    assert prompt_registry.prompt_entries() == PROMPTS
