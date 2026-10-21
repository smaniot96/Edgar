"""Versioned registry of the play prompts (EDG-059).

The registry does not own prompt text: each entry points at the constant the runtime already sends
(`agent.prompts`, `agent.graph_combat`, `agent.nodes.memory_summarizer`). It adds a stable id, a
semantic version and the sha256 of the exact UTF-8 text. `prompt_manifest()` is what an experiment
run records, so a result can name the prompt text it was produced with.

Rule: when a prompt's text changes, its `version` must be bumped in the same change, and the golden
manifest `evals/prompt_manifest.golden.json` must be refreshed (see docs/prompt-experiments.md).
`tests/test_prompt_registry.py` enforces both. Nothing at runtime reads this module.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from agent.graph_combat import ENCOUNTER_BUILDER
from agent.nodes.memory_summarizer import MEMORY_SUMMARY_PROMPT
from agent.prompts import INPUT_PARSER, NARRATOR, RULES_ADJUDICATOR

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True, slots=True)
class PromptEntry:
    id: str
    role: str
    version: str
    text: str

    def __post_init__(self) -> None:
        if not _SEMVER.match(self.version):
            raise ValueError(f"prompt {self.id!r}: version must be semver, got {self.version!r}")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# Ids are the model-profile stage names (`edgar_core.model_profiles.PLAY_STAGES`) so a manifest
# entry can be matched to the profile that sent it. combat_narrator sends the NARRATOR text too
# (`agent.nodes.narrator`), so editing NARRATOR changes both entries.
PROMPTS: tuple[PromptEntry, ...] = (
    PromptEntry("play.parser", "input parser", "1.2.0", INPUT_PARSER),
    PromptEntry("play.consequence_planner", "rules adjudicator / consequence planner", "1.0.8", RULES_ADJUDICATOR),
    PromptEntry("play.narrator", "narrator", "1.0.5", NARRATOR),
    PromptEntry("play.combat_builder", "combat encounter builder", "1.0.0", ENCOUNTER_BUILDER),
    PromptEntry("play.combat_narrator", "combat narrator", "1.0.5", NARRATOR),
    PromptEntry("play.memory_summarizer", "rolling story summarizer", "1.0.0", MEMORY_SUMMARY_PROMPT),
)  # fmt: skip


def prompt_entries() -> tuple[PromptEntry, ...]:
    return PROMPTS


def prompt_manifest() -> dict[str, dict[str, str]]:
    """{prompt id: {"version": semver, "sha256": hex}} for the prompts this build sends."""
    return {p.id: {"version": p.version, "sha256": p.sha256} for p in PROMPTS}


def manifest_drift(
    current: dict[str, dict[str, str]], golden: dict[str, dict[str, str]]
) -> list[str]:
    """Human-readable problems between this build's manifest and the committed golden manifest.

    A prompt whose text changed without a version bump is reported as a bump violation. Any other
    difference (new/removed id, version changed without text, stale golden) asks for a refresh.
    """
    problems: list[str] = []
    for missing in sorted(set(golden) - set(current)):
        problems.append(f"{missing}: in golden manifest but no longer registered")
    for new in sorted(set(current) - set(golden)):
        problems.append(f"{new}: registered but missing from the golden manifest (refresh it)")
    for prompt_id in sorted(set(current) & set(golden)):
        now, was = current[prompt_id], golden[prompt_id]
        if now["sha256"] != was["sha256"] and now["version"] == was["version"]:
            problems.append(
                f"{prompt_id}: text changed but version is still {was['version']}; bump its "
                "semantic version and refresh the golden manifest"
            )
        elif now != was:
            problems.append(f"{prompt_id}: differs from the golden manifest (refresh it)")
    return problems
