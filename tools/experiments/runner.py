"""Offline prompt-experiment runner and run-record writer (EDG-059).

The default path uses scripted fake models only: each scenario's `scripted` block answers the three
play stages (parser, consequence planner, narrator), and the production deterministic guards then run
over those outputs. No provider client is constructed and no network is used.

A live run (real models) is NOT implemented here. `--live` exists so that the cost and the authorization
requirement are explicit: it needs the flag AND the environment variable `EDGAR_PROMPT_EXPERIMENT_LIVE`
set to `authorized`. Even then it only prints the estimated stage-call count and exits with status 3.
Turning it into a real run is a separate, explicitly authorized change (docs/prompt-experiments.md).

Commands:
  python -m tools.experiments.runner validate [--scenarios DIR]
  python -m tools.experiments.runner manifest [--write]
  python -m tools.experiments.runner run --out DIR [--dm-settings FILE] [--human-reviewer ID]
                                         [--run-date YYYY-MM-DD] [--scenarios DIR]
  python -m tools.experiments.runner run --live ...   (refused without the flag and the env var)
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.models.adjudication import AdjudicationResult  # noqa: E402
from agent.models.parsed_input import ParsedInput  # noqa: E402
from agent.prompt_registry import manifest_drift, prompt_manifest  # noqa: E402
from agent.supported_rules import (  # noqa: E402
    actions_for_state,
    apply_adjudication_boundary,
    apply_narration_boundary,
    engine_applied_for_state,
)
from tools.experiments.checks import CHECKER_VERSION, RunOutputs, run_checks  # noqa: E402
from tools.experiments.scenarios import (  # noqa: E402
    CHECK_IDS,
    FUN_RUBRIC_PATH,
    SCENARIO_DIR,
    SCRIPTED_STAGES,
    Scenario,
    load_fun_rubric,
    load_scenarios,
)

RECORD_SCHEMA_VERSION = 1
LIVE_FLAG = "--live"
LIVE_ENV = "EDGAR_PROMPT_EXPERIMENT_LIVE"
LIVE_ENV_VALUE = "authorized"
GOLDEN_MANIFEST = ROOT / "evals" / "prompt_manifest.golden.json"
PROFILE_CONFIG = ROOT / "config" / "model-profiles.toml"
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_STAGE_KEYS = {"profile", "model", "reasoning_effort"}


class UnscriptedCall(KeyError):
    """The scenario did not script a stage that the runner asked for."""


class ScriptedModel:
    """A fake chat model that answers from a scenario's scripted outputs and records each call.

    It has no client, no key and no network. Asking for an unscripted stage raises UnscriptedCall.
    """

    def __init__(self, scripted: Mapping[str, Any]) -> None:
        self._scripted = dict(scripted)
        self.calls: list[str] = []

    def complete(self, stage: str) -> Any:
        if stage not in self._scripted:
            raise UnscriptedCall(stage)
        self.calls.append(stage)
        return copy.deepcopy(self._scripted[stage])


@dataclass(frozen=True, slots=True)
class DmSettings:
    """The settings of the model that plays the DM. Recorded apart from any evaluator setting."""

    label: str
    source: str  # "scripted-fake" for offline runs, "live" only after an authorized run
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "source": self.source, "stages": self.stages}


SCRIPTED_DM = DmSettings(label="scripted-fake", source="scripted-fake", stages={})


def load_dm_settings(path: Path | None) -> DmSettings:
    if path is None:
        return SCRIPTED_DM
    raw = json.loads(path.read_text(encoding="utf-8"))
    label = raw.get("label")
    if not isinstance(label, str) or not _LABEL.match(label):
        raise ValueError("dm settings: 'label' must be lowercase letters, digits and hyphens")
    if raw.get("source") not in ("scripted-fake", "live"):
        raise ValueError("dm settings: 'source' must be 'scripted-fake' or 'live'")
    stages = raw.get("stages", {})
    if not isinstance(stages, dict):
        raise ValueError("dm settings: 'stages' must be an object")
    for stage, settings in stages.items():
        if not stage.startswith("play."):
            raise ValueError(f"dm settings: stage {stage!r} is not a play stage")
        if not isinstance(settings, dict) or set(settings) - _STAGE_KEYS:
            raise ValueError(f"dm settings: stage {stage!r} has unknown keys")
    return DmSettings(label=label, source=raw["source"], stages=stages)


def evaluator_settings(human_reviewer: str | None) -> dict[str, Any]:
    return {
        "checker": "tools.experiments.checks",
        "checker_version": CHECKER_VERSION,
        "fun_scoring": "human-only",
        "human_reviewer": human_reviewer,
    }


def app_git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def profile_config_version() -> str | None:
    try:
        return str(tomllib.loads(PROFILE_CONFIG.read_text(encoding="utf-8")).get("profile_version"))
    except (OSError, tomllib.TOMLDecodeError):
        return None


def run_scenario(scenario: Scenario) -> dict[str, Any]:
    """Run one scenario through the scripted models and the production guards; return its row."""
    model = ScriptedModel(scenario.scripted)
    parsed = ParsedInput(**model.complete("play.parser"))
    planner_raw = model.complete("play.consequence_planner")
    narration_raw = model.complete("play.narrator")

    state = {
        "player_input": scenario.player_input,
        "parsed_input": parsed,
        "character": scenario.character,
        "engine_roll_mode": scenario.engine["roll_mode"],
        "engine_effects": scenario.engine["effects"],
    }
    actions = actions_for_state(state)
    applied = engine_applied_for_state(state)

    adj = AdjudicationResult(**{"success": False, **planner_raw})
    raw_summary = adj.mechanical_summary
    apply_adjudication_boundary(adj, actions, applied=applied)
    final_narration = apply_narration_boundary(narration_raw, actions, applied=applied)

    outputs = RunOutputs(
        player_input=scenario.player_input,
        parsed=parsed,
        raw_narration=narration_raw,
        final_narration=final_narration,
        raw_summary=raw_summary,
        final_summary=adj.mechanical_summary,
        adjudicated_success=bool(adj.success),
        combatants=scenario.combatants,
        engine=scenario.engine,
        agency=scenario.agency,
    )
    results = run_checks(outputs)
    checks = {}
    for check_id in CHECK_IDS:
        result = results[check_id]
        expected = scenario.expect.get(check_id)
        checks[check_id] = {
            **result.to_dict(),
            "expected": expected,
            "as_designed": None if expected is None else result.passed == expected,
        }
    return {
        "record": "scenario",
        "scenario_id": scenario.id,
        "scenario_version": scenario.version,
        "stages_called": list(model.calls),
        "outputs": {
            "parsed_intent": parsed.intent,
            "narration_raw": narration_raw,
            "narration_final": final_narration,
            "summary_final": adj.mechanical_summary,
        },
        "checks": checks,
        "fun": {
            "rubric_id": scenario.fun_rubric_id,
            "status": "pending_human",
            "reviewer": None,
            "scores": {},
        },
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_run(
    scenarios: Sequence[Scenario],
    dm: DmSettings,
    *,
    human_reviewer: str | None,
    run_date: str,
    sha: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    versions = {s.id: s.version for s in scenarios}
    manifest = prompt_manifest()
    evaluator = evaluator_settings(human_reviewer)
    identity = {
        "dm": dm.to_dict(),
        "evaluator": evaluator,
        "manifest": manifest,
        "scenarios": versions,
        "run_date": run_date,
        "app_git_sha": sha,
    }
    digest = hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest()[:12]
    run_id = f"{dm.label}-{digest}"
    header = {
        "record": "run",
        "schema_version": RECORD_SCHEMA_VERSION,
        "run_id": run_id,
        "run_date": run_date,
        "app_git_sha": sha,
        "mode": "offline-scripted" if dm.source == "scripted-fake" else dm.source,
        "provider_calls": 0,
        "dm": dm.to_dict(),
        "evaluator": evaluator,
        "prompt_manifest": manifest,
        "profile_config_version": profile_config_version(),
        "scenarios": versions,
        "fun_rubric": {"path": FUN_RUBRIC_PATH.name, "id": load_fun_rubric()["id"]},
    }
    rows = []
    for scenario in scenarios:
        row = run_scenario(scenario)
        row["run_id"] = run_id
        rows.append(row)
    return header, rows


def write_run(
    out_dir: Path,
    header: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    *,
    allow_in_repo: bool = False,
) -> Path:
    """Write one run as JSONL: a header line, then one line per scenario. Never overwrites a run."""
    out_dir = Path(out_dir).expanduser().resolve()
    if not allow_in_repo and (out_dir == ROOT or ROOT in out_dir.parents):
        raise SystemExit(
            f"refusing to write run records inside the repository ({ROOT}); pass --out outside it "
            "or --allow-in-repo"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{header['run_id']}.jsonl"
    if path.exists():
        raise SystemExit(
            f"{path} already exists; runs are append-once, choose another label or date"
        )
    with path.open("x", encoding="utf-8") as handle:
        handle.write(_canonical(header) + "\n")
        for row in rows:
            handle.write(_canonical(row) + "\n")
    return path


def live_gate(live_requested: bool, environ: Mapping[str, str]) -> str | None:
    """None when a live run may proceed; otherwise the reason it is refused."""
    if not live_requested:
        return None
    if environ.get(LIVE_ENV) != LIVE_ENV_VALUE:
        return f"live runs need {LIVE_FLAG} AND {LIVE_ENV}={LIVE_ENV_VALUE}; the environment variable is not set"
    return None


def estimated_live_calls(scenarios: Sequence[Scenario], repeats: int = 1) -> int:
    """Logical stage calls a live run would make: one per scripted stage per scenario per repeat."""
    return sum(len(SCRIPTED_STAGES) for _ in scenarios) * repeats


def cmd_validate(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(Path(args.scenarios))
    print(f"{len(scenarios)} scenario(s) valid in {args.scenarios}")
    for s in scenarios:
        print(f"  {s.id} v{s.version}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    current = prompt_manifest()
    if args.write:
        GOLDEN_MANIFEST.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {GOLDEN_MANIFEST}")
        return 0
    golden = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    problems = manifest_drift(current, golden)
    print(json.dumps(current, indent=2, sort_keys=True))
    for problem in problems:
        print(f"DRIFT: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.live:
        reason = live_gate(True, os.environ)
        scenarios = load_scenarios(Path(args.scenarios))
        estimate = estimated_live_calls(scenarios, args.repeats)
        if reason is not None:
            print(f"live run refused: {reason}", file=sys.stderr)
            print(f"a live run would make about {estimate} logical stage call(s); none were made.")
            return 2
        print(f"estimated logical stage calls: {estimate} (retries not counted)")
        print(
            "live execution is not implemented in this change; a later, explicitly authorized step "
            "must add it. No calls were made.",
            file=sys.stderr,
        )
        return 3

    scenarios = load_scenarios(Path(args.scenarios))
    dm = load_dm_settings(Path(args.dm_settings) if args.dm_settings else None)
    run_date = args.run_date or date.today().isoformat()
    header, rows = build_run(
        scenarios,
        dm,
        human_reviewer=args.human_reviewer,
        run_date=run_date,
        sha=app_git_sha(),
    )
    path = write_run(Path(args.out), header, rows, allow_in_repo=args.allow_in_repo)
    print(f"wrote {path} ({len(rows)} scenario record(s), provider calls: 0)")
    failing = [
        f"{r['scenario_id']}:{cid}"
        for r in rows
        for cid, c in r["checks"].items()
        if c["as_designed"] is False
    ]
    if failing:
        print(f"checks not as designed: {failing}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tools.experiments.runner", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate scenario files")
    v.add_argument("--scenarios", default=str(SCENARIO_DIR))
    v.set_defaults(func=cmd_validate)

    m = sub.add_parser("manifest", help="print or refresh the golden prompt manifest")
    m.add_argument(
        "--write", action="store_true", help="overwrite evals/prompt_manifest.golden.json"
    )
    m.set_defaults(func=cmd_manifest)

    r = sub.add_parser("run", help="run scenarios offline with scripted models")
    r.add_argument("--out", help="directory for run JSONL files (outside the repository)")
    r.add_argument("--scenarios", default=str(SCENARIO_DIR))
    r.add_argument("--dm-settings", help="JSON file describing the DM model settings")
    r.add_argument("--human-reviewer", default=None, help="id of the human who scores fun, if any")
    r.add_argument("--run-date", default=None, help="YYYY-MM-DD (default: today)")
    r.add_argument("--allow-in-repo", action="store_true")
    r.add_argument("--live", action="store_true", help="request a live run (refused by default)")
    r.add_argument("--repeats", type=int, default=1, help="repeats for the live estimate only")
    r.set_defaults(func=cmd_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run" and not args.live and not args.out:
        print("run needs --out DIR (outside the repository)", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
