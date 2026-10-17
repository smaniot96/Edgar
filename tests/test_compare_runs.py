"""EDG-059: tools/compare_runs.py refuses mismatched scenario versions and reports every diff."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tools.compare_runs import CompareError, compare, load_run, main, render
from tools.experiments.runner import SCRIPTED_DM, DmSettings, build_run, load_scenarios, write_run


def _write(tmp_path: Path, name: str, header: dict[str, Any], rows: list[dict[str, Any]]) -> Path:
    return write_run(tmp_path / name, header, rows)


@pytest.fixture()
def run_a(tmp_path: Path) -> Path:
    header, rows = build_run(
        load_scenarios(), SCRIPTED_DM, human_reviewer=None, run_date="2026-10-17", sha="sha-a"
    )
    return _write(tmp_path, "a", header, rows)


def _variant(tmp_path: Path, run_a: Path, mutate) -> Path:
    header, rows = load_raw(run_a)
    header, rows = copy.deepcopy(header), copy.deepcopy(rows)
    mutate(header, rows)
    return _write(tmp_path, f"b{len(list(tmp_path.iterdir()))}", header, rows)


def load_raw(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    return lines[0], lines[1:]


def test_identical_runs_compare_cleanly_with_no_diffs(run_a: Path) -> None:
    result = compare(load_run(run_a), load_run(run_a))
    assert result["n_scenarios_compared"] == 8
    assert result["prompt_manifest_diff"] == []
    assert result["dm_settings_diff"] == []
    assert result["evaluator_settings_diff"] == []
    assert result["totals"]["fidelity"]["a"] == result["totals"]["fidelity"]["b"]
    text = render(result)
    assert "n=8" in text
    assert "No statistical significance is claimed" in text


def test_scenario_version_mismatch_is_refused_by_default(tmp_path: Path, run_a: Path) -> None:
    def bump(header, rows):
        header["scenarios"]["rp-persuade-captain"] = "1.1.0"
        for row in rows:
            if row["scenario_id"] == "rp-persuade-captain":
                row["scenario_version"] = "1.1.0"

    run_b = _variant(tmp_path, run_a, bump)
    with pytest.raises(CompareError, match="scenario versions differ: rp-persuade-captain"):
        compare(load_run(run_a), load_run(run_b))
    assert main([str(run_a), str(run_b)]) == 2


def test_allow_mismatch_compares_the_rest_and_flags_the_mismatched_scenario(
    tmp_path: Path, run_a: Path
) -> None:
    def bump(header, rows):
        header["scenarios"]["rp-persuade-captain"] = "1.1.0"

    run_b = _variant(tmp_path, run_a, bump)
    result = compare(load_run(run_a), load_run(run_b), allow_mismatch=True)
    assert result["n_scenarios_not_comparable"] == ["rp-persuade-captain"]
    assert result["n_scenarios_compared"] == 7
    assert "rp-persuade-captain" not in {s["scenario_id"] for s in result["scenarios"]}
    assert "NOT comparable" in render(result)


def test_dm_manifest_and_evaluator_differences_are_printed(tmp_path: Path, run_a: Path) -> None:
    dm = DmSettings(
        label="dm-b",
        source="scripted-fake",
        stages={
            "play.narrator": {"profile": "play.narrator", "model": "m-2", "reasoning_effort": "low"}
        },
    )
    header_b, rows_b = build_run(
        load_scenarios(), dm, human_reviewer="reviewer-2", run_date="2026-10-17", sha="sha-b"
    )
    header_b["prompt_manifest"] = copy.deepcopy(header_b["prompt_manifest"])
    header_b["prompt_manifest"]["play.narrator"] = {"version": "1.0.1", "sha256": "f" * 64}
    run_b = _write(tmp_path, "dm-b", header_b, rows_b)

    result = compare(load_run(run_a), load_run(run_b))
    assert [r["prompt"] for r in result["prompt_manifest_diff"]] == ["play.narrator"]
    dm_keys = {r["key"] for r in result["dm_settings_diff"]}
    assert "label" in dm_keys and "stages.play.narrator.reasoning_effort" in dm_keys
    evaluator_keys = {r["key"] for r in result["evaluator_settings_diff"]}
    assert evaluator_keys == {"human_reviewer"}
    text = render(result)
    assert "play.narrator" in text and "reviewer-2" in text


def test_human_fun_scores_are_compared_only_when_both_runs_have_them(
    tmp_path: Path, run_a: Path
) -> None:
    def score(header, rows):
        for row in rows:
            if row["scenario_id"] == "rp-persuade-captain":
                row["fun"] = {**row["fun"], "reviewer": "r-1", "scores": {"overall_fun": 4}}

    run_b = _variant(tmp_path, run_a, score)
    result = compare(load_run(run_a), load_run(run_b))
    fun_by_id = {s["scenario_id"]: s["metrics"]["fun"] for s in result["scenarios"]}
    assert fun_by_id["rp-persuade-captain"] == "not scored by a human in both runs"

    both = compare(load_run(run_b), load_run(run_b))
    fun_both = {s["scenario_id"]: s["metrics"]["fun"] for s in both["scenarios"]}
    assert fun_both["rp-persuade-captain"] == {"overall_fun": {"a": 4.0, "b": 4.0}}
