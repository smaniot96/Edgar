"""Compare two prompt-experiment run files (EDG-059). Standard library only; no model, no network.

Input: two JSONL files written by `python -m tools.experiments.runner run --out DIR`. Each file is one
run: a header line (DM settings, evaluator settings, prompt manifest, app git sha, scenario versions)
followed by one line per scenario.

Output, per scenario and per metric:
  * agency and fidelity: the deterministic pass rate of that category's checks, for each run;
  * fun: the mean human score per rubric dimension, only where both runs have human scores.
Then the prompt-manifest diff, the DM-settings diff and the evaluator-settings diff.

Refuses to compare when scenario versions differ, unless --allow-mismatch is given (the mismatched
scenarios are then printed as not comparable). With only a few scenarios any difference is anecdotal:
the report states n and makes no significance claim.

Usage: python tools/compare_runs.py RUN_A.jsonl RUN_B.jsonl [--allow-mismatch] [--json]
Exit status: 0 compared, 2 refused or unreadable input.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CATEGORIES = ("agency", "fidelity")
NO_SIGNIFICANCE = (
    "Differences over a handful of scenarios are anecdotal. No statistical significance is "
    "claimed; read the n values before drawing any conclusion."
)


class CompareError(Exception):
    """Input that cannot be compared. The message is shown to the user."""


def load_run(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        lines = [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise CompareError(f"{path}: cannot read run file ({exc})") from exc
    if not lines or lines[0].get("record") != "run":
        raise CompareError(f"{path}: first line must be the run header")
    header = lines[0]
    rows: dict[str, dict[str, Any]] = {}
    for line in lines[1:]:
        if line.get("record") != "scenario":
            raise CompareError(f"{path}: unexpected record {line.get('record')!r}")
        if line.get("run_id") != header.get("run_id"):
            raise CompareError(f"{path}: scenario row belongs to another run")
        sid = line["scenario_id"]
        if sid in rows:
            raise CompareError(f"{path}: duplicate scenario row {sid!r}")
        rows[sid] = line
    if set(rows) != set(header.get("scenarios", {})):
        raise CompareError(f"{path}: scenario rows do not match the header's scenario list")
    return header, rows


def _rate(row: Mapping[str, Any], category: str) -> tuple[int, int]:
    checks = [c for cid, c in row["checks"].items() if cid.startswith(category + ".")]
    return sum(1 for c in checks if c["passed"]), len(checks)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key in value:
            out.update(_flatten(value[key], f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def _diff(a: Any, b: Any) -> list[dict[str, Any]]:
    fa, fb = _flatten(a), _flatten(b)
    return [
        {"key": k, "a": fa.get(k), "b": fb.get(k)}
        for k in sorted(set(fa) | set(fb))
        if fa.get(k) != fb.get(k)
    ]


def _manifest_diff(a: Mapping[str, Any], b: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for pid in sorted(set(a) | set(b)):
        ea, eb = a.get(pid), b.get(pid)
        if ea != eb:
            rows.append({"prompt": pid, "a": ea, "b": eb})
    return rows


def _fun_means(row: Mapping[str, Any]) -> dict[str, float]:
    scores = row.get("fun", {}).get("scores") or {}
    return {k: float(v) for k, v in scores.items() if isinstance(v, (int, float))}


def compare(
    run_a: tuple[dict[str, Any], dict[str, dict[str, Any]]],
    run_b: tuple[dict[str, Any], dict[str, dict[str, Any]]],
    *,
    allow_mismatch: bool = False,
) -> dict[str, Any]:
    (ha, ra), (hb, rb) = run_a, run_b
    versions_a, versions_b = ha.get("scenarios", {}), hb.get("scenarios", {})
    common = sorted(set(ra) & set(rb))
    mismatched = sorted(s for s in common if versions_a.get(s) != versions_b.get(s))
    if mismatched and not allow_mismatch:
        detail = ", ".join(f"{s} ({versions_a.get(s)} vs {versions_b.get(s)})" for s in mismatched)
        raise CompareError(
            f"refusing to compare: scenario versions differ: {detail}. Re-run with the same "
            "scenario versions, or pass --allow-mismatch to see them flagged as not comparable."
        )
    comparable = [s for s in common if s not in mismatched]

    scenarios = []
    totals: dict[str, dict[str, list[int]]] = {c: {"a": [0, 0], "b": [0, 0]} for c in CATEGORIES}
    for sid in comparable:
        entry: dict[str, Any] = {"scenario_id": sid, "version": versions_a.get(sid), "metrics": {}}
        for cat in CATEGORIES:
            pa, na = _rate(ra[sid], cat)
            pb, nb = _rate(rb[sid], cat)
            totals[cat]["a"][0] += pa
            totals[cat]["a"][1] += na
            totals[cat]["b"][0] += pb
            totals[cat]["b"][1] += nb
            entry["metrics"][cat] = {"a": [pa, na], "b": [pb, nb]}
        fa, fb = _fun_means(ra[sid]), _fun_means(rb[sid])
        if fa and fb:
            dims = sorted(set(fa) & set(fb))
            entry["metrics"]["fun"] = {d: {"a": fa[d], "b": fb[d]} for d in dims}
        else:
            entry["metrics"]["fun"] = "not scored by a human in both runs"
        scenarios.append(entry)

    return {
        "run_a": ha.get("run_id"),
        "run_b": hb.get("run_id"),
        "n_scenarios_compared": len(comparable),
        "n_scenarios_not_comparable": mismatched,
        "only_in_a": sorted(set(ra) - set(rb)),
        "only_in_b": sorted(set(rb) - set(ra)),
        "scenarios": scenarios,
        "totals": {cat: {"a": totals[cat]["a"], "b": totals[cat]["b"]} for cat in CATEGORIES},
        "prompt_manifest_diff": _manifest_diff(
            ha.get("prompt_manifest", {}), hb.get("prompt_manifest", {})
        ),
        "dm_settings_diff": _diff(ha.get("dm", {}), hb.get("dm", {})),
        "evaluator_settings_diff": _diff(ha.get("evaluator", {}), hb.get("evaluator", {})),
        "other_diff": _diff(
            {k: ha.get(k) for k in ("app_git_sha", "run_date", "mode", "profile_config_version")},
            {k: hb.get(k) for k in ("app_git_sha", "run_date", "mode", "profile_config_version")},
        ),
        "note": NO_SIGNIFICANCE,
    }


def _rate_text(pair: list[int]) -> str:
    passed, total = pair
    return f"{passed}/{total}" if total else "n/a"


def render(result: dict[str, Any]) -> str:
    lines = [
        f"run A: {result['run_a']}",
        f"run B: {result['run_b']}",
        f"scenarios compared: n={result['n_scenarios_compared']}",
    ]
    if result["n_scenarios_not_comparable"]:
        lines.append(
            f"NOT comparable (scenario version mismatch): {result['n_scenarios_not_comparable']}"
        )
    if result["only_in_a"] or result["only_in_b"]:
        lines.append(f"only in A: {result['only_in_a']}; only in B: {result['only_in_b']}")
    lines.append("")
    lines.append("Deterministic pass counts (passed/checks), per scenario:")
    for sc in result["scenarios"]:
        agency, fidelity = sc["metrics"]["agency"], sc["metrics"]["fidelity"]
        lines.append(
            f"  {sc['scenario_id']} v{sc['version']}: agency A {_rate_text(agency['a'])} "
            f"B {_rate_text(agency['b'])} | fidelity A {_rate_text(fidelity['a'])} B {_rate_text(fidelity['b'])}"
        )
        fun = sc["metrics"]["fun"]
        if isinstance(fun, str):
            lines.append(f"    fun: {fun}")
        else:
            for dim, pair in fun.items():
                lines.append(f"    fun {dim}: A {pair['a']:.2f} B {pair['b']:.2f}")
    lines.append("")
    for cat in CATEGORIES:
        t = result["totals"][cat]
        lines.append(f"Total {cat}: A {_rate_text(t['a'])} B {_rate_text(t['b'])}")
    lines.append("")
    lines.append("Prompt manifest differences:")
    lines.extend(
        [f"  {r['prompt']}: A {r['a']} vs B {r['b']}" for r in result["prompt_manifest_diff"]]
        or ["  none"]
    )
    lines.append("DM settings differences:")
    lines.extend(
        [f"  {r['key']}: A {r['a']!r} vs B {r['b']!r}" for r in result["dm_settings_diff"]]
        or ["  none"]
    )
    lines.append("Evaluator settings differences:")
    lines.extend(
        [f"  {r['key']}: A {r['a']!r} vs B {r['b']!r}" for r in result["evaluator_settings_diff"]]
        or ["  none"]
    )
    lines.append("Run metadata differences:")
    lines.extend(
        [f"  {r['key']}: A {r['a']!r} vs B {r['b']!r}" for r in result["other_diff"]] or ["  none"]
    )
    lines.append("")
    lines.append(result["note"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_a", type=Path)
    parser.add_argument("run_b", type=Path)
    parser.add_argument("--allow-mismatch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = compare(
            load_run(args.run_a), load_run(args.run_b), allow_mismatch=args.allow_mismatch
        )
    except CompareError as exc:
        print(f"compare_runs: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
