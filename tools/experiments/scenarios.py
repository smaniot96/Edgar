"""Versioned declarative scenarios for prompt experiments (EDG-059).

A scenario is one JSON file under `evals/scenarios/`. It fixes the player input, the engine state
(roll, effects, HP, roster), the scripted outputs of the play stages, and the observable expectations.
The schema is validated by hand (no extra dependency) and unknown keys are rejected, so a typo cannot
silently drop an expectation.

Versions: a scenario's `version` is semver. Bump it whenever any field that changes what is measured
changes (input, state, scripted outputs, expectations). `tools/compare_runs.py` refuses to compare runs
whose scenario versions differ unless told to.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.models.parsed_input import ParsedInput

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
SCENARIO_DIR = ROOT / "evals" / "scenarios"
FUN_RUBRIC_PATH = ROOT / "evals" / "fun_rubric.json"

CHECK_IDS: tuple[str, ...] = (
    "agency.intent_parsed",
    "agency.target_acknowledged",
    "agency.consequence_tied",
    "fidelity.roster",
    "fidelity.claimed_effects",
    "fidelity.hp_numbers",
    "fidelity.mechanics_summary",
)
# Stages a scenario must script. The offline runner calls exactly these, and nothing else.
SCRIPTED_STAGES: tuple[str, ...] = ("play.parser", "play.consequence_planner", "play.narrator")
ROLL_MODES = ("normal", "advantage", "disadvantage")
INTENTS = ("combat", "rp", "exploration")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_TOP_KEYS = {
    "schema_version",
    "id",
    "version",
    "title",
    "description",
    "player_input",
    "character",
    "engine",
    "combatants",
    "scripted",
    "agency",
    "expect",
    "fun_rubric_id",
    "labels",
}
_REQUIRED_KEYS = {
    "id",
    "version",
    "title",
    "player_input",
    "character",
    "engine",
    "scripted",
    "agency",
}
_ENGINE_KEYS = {"roll_mode", "effects", "check_rolled", "success", "hp_after"}
_AGENCY_KEYS = {"expected_intent", "must_mention"}


class ScenarioError(ValueError):
    """A scenario file that does not match the schema. Messages name the file and the field."""


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    version: str
    title: str
    description: str
    player_input: str
    character: dict[str, Any]
    engine: dict[str, Any]
    combatants: tuple[dict[str, Any], ...]
    scripted: Mapping[str, Any]
    agency: dict[str, Any]
    expect: Mapping[str, bool]
    fun_rubric_id: str
    labels: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""

    def scripted_stage_count(self) -> int:
        return len(self.scripted)


def _fail(where: str, message: str) -> None:
    raise ScenarioError(f"{where}: {message}")


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    return value


def _check_keys(obj: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        _fail(where, f"unknown keys {unknown}")


def _str(obj: Mapping[str, Any], key: str, where: str, *, allow_empty: bool = False) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(where, f"'{key}' must be a non-empty string")
    return value


def validate_scenario(raw: Any, *, source: str = "<memory>") -> Scenario:
    """Validate one decoded scenario object; raises ScenarioError with the field that failed."""
    where = source
    obj = _require_mapping(raw, where)
    _check_keys(obj, _TOP_KEYS, where)
    missing = sorted(_REQUIRED_KEYS - set(obj))
    if missing:
        _fail(where, f"missing keys {missing}")
    if obj.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        _fail(where, f"schema_version must be {SCHEMA_VERSION}")

    scenario_id = _str(obj, "id", where)
    if not _ID.match(scenario_id):
        _fail(where, "'id' must be lowercase letters, digits and hyphens (3-64 chars)")
    where = f"{source} ({scenario_id})"
    version = _str(obj, "version", where)
    if not _SEMVER.match(version):
        _fail(where, "'version' must be semver")
    _str(obj, "title", where)
    _str(obj, "description", where, allow_empty=True)
    player_input = _str(obj, "player_input", where)

    character = dict(_require_mapping(obj["character"], f"{where}.character"))
    _str(character, "name", f"{where}.character")

    engine = dict(_require_mapping(obj["engine"], f"{where}.engine"))
    _check_keys(engine, _ENGINE_KEYS, f"{where}.engine")
    if engine.get("roll_mode", "normal") not in ROLL_MODES:
        _fail(where, f"engine.roll_mode must be one of {ROLL_MODES}")
    effects = engine.get("effects", [])
    if not isinstance(effects, list) or not all(isinstance(e, str) for e in effects):
        _fail(where, "engine.effects must be a list of strings")
    if not isinstance(engine.get("check_rolled"), bool):
        _fail(where, "engine.check_rolled must be a boolean")
    success = engine.get("success")
    if success is not None and not isinstance(success, bool):
        _fail(where, "engine.success must be a boolean or null")
    hp_after = engine.get("hp_after", {})
    if not isinstance(hp_after, dict) or not all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in hp_after.values()
    ):
        _fail(where, "engine.hp_after must map names to non-negative integers")
    engine.setdefault("roll_mode", "normal")
    engine["effects"] = list(effects)
    engine["hp_after"] = dict(hp_after)

    combatants_raw = obj.get("combatants", [])
    if not isinstance(combatants_raw, list):
        _fail(where, "combatants must be a list")
    combatants: list[dict[str, Any]] = []
    for index, item in enumerate(combatants_raw):
        c = dict(_require_mapping(item, f"{where}.combatants[{index}]"))
        _str(c, "name", f"{where}.combatants[{index}]")
        if not isinstance(c.get("is_player", False), bool):
            _fail(where, f"combatants[{index}].is_player must be a boolean")
        combatants.append(c)

    scripted_raw = _require_mapping(obj["scripted"], f"{where}.scripted")
    if set(scripted_raw) != set(SCRIPTED_STAGES):
        _fail(where, f"scripted must provide exactly {list(SCRIPTED_STAGES)}")
    parser = _require_mapping(scripted_raw["play.parser"], f"{where}.scripted.play.parser")
    unknown_parser = sorted(set(parser) - set(ParsedInput.model_fields))
    if unknown_parser:
        _fail(where, f"scripted parser has unknown fields {unknown_parser}")
    ParsedInput(**parser)  # raises if a value violates the production model
    planner = _require_mapping(
        scripted_raw["play.consequence_planner"], f"{where}.scripted.play.consequence_planner"
    )
    if not isinstance(planner.get("mechanical_summary"), str):
        _fail(where, "scripted consequence planner needs a string mechanical_summary")
    if "success" in planner and not isinstance(planner["success"], bool):
        _fail(where, "scripted consequence planner success must be a boolean")
    if not isinstance(scripted_raw["play.narrator"], str) or not scripted_raw["play.narrator"]:
        _fail(where, "scripted narrator output must be a non-empty string")

    agency = dict(_require_mapping(obj["agency"], f"{where}.agency"))
    _check_keys(agency, _AGENCY_KEYS, f"{where}.agency")
    if agency.get("expected_intent") not in INTENTS:
        _fail(where, f"agency.expected_intent must be one of {INTENTS}")
    mentions = agency.get("must_mention", [])
    if not isinstance(mentions, list) or not all(isinstance(m, str) and m for m in mentions):
        _fail(where, "agency.must_mention must be a list of non-empty strings")
    agency["must_mention"] = list(mentions)

    expect_raw = _require_mapping(obj.get("expect", {}), f"{where}.expect")
    unknown_checks = sorted(set(expect_raw) - set(CHECK_IDS))
    if unknown_checks:
        _fail(where, f"expect names unknown checks {unknown_checks}")
    if not all(isinstance(v, bool) for v in expect_raw.values()):
        _fail(where, "expect values must be booleans")

    rubric = _str(obj, "fun_rubric_id", where) if "fun_rubric_id" in obj else "fun-v1"
    labels = obj.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        _fail(where, "labels must be a list of strings")

    return Scenario(
        id=scenario_id,
        version=version,
        title=str(obj["title"]),
        description=str(obj.get("description", "")),
        player_input=player_input,
        character=character,
        engine=engine,
        combatants=tuple(combatants),
        scripted=dict(scripted_raw),
        agency=agency,
        expect=dict(expect_raw),
        fun_rubric_id=rubric,
        labels=tuple(labels),
        source=source,
    )


def load_scenarios(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    """All `*.json` scenarios in a directory, validated, sorted by id. Duplicate ids are an error."""
    scenarios: list[Scenario] = []
    seen: dict[str, str] = {}
    for path in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScenarioError(
                f"{path.name}: invalid JSON ({exc.msg} at line {exc.lineno})"
            ) from exc
        scenario = validate_scenario(raw, source=path.name)
        if scenario.id in seen:
            raise ScenarioError(
                f"{path.name}: duplicate scenario id {scenario.id!r} ({seen[scenario.id]})"
            )
        seen[scenario.id] = path.name
        scenarios.append(scenario)
    return sorted(scenarios, key=lambda s: s.id)


def load_fun_rubric(path: Path = FUN_RUBRIC_PATH) -> dict[str, Any]:
    rubric = json.loads(path.read_text(encoding="utf-8"))
    for key in ("id", "version", "scale", "dimensions", "reviewer_policy"):
        if key not in rubric:
            raise ScenarioError(f"{path.name}: missing {key!r}")
    return rubric
