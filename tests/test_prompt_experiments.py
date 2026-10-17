"""EDG-059: offline prompt-experiment framework (scenario schema, checks, runner, live guard)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agent.models.parsed_input import ParsedInput
from agent.prompt_registry import prompt_manifest
from tools.experiments import runner as runner_mod
from tools.experiments.checks import (
    RunOutputs,
    check_consequence_tied,
    check_hp_numbers,
    check_mechanics_summary,
)
from tools.experiments.runner import (
    LIVE_ENV,
    LIVE_ENV_VALUE,
    SCRIPTED_DM,
    ScriptedModel,
    UnscriptedCall,
    build_run,
    estimated_live_calls,
    live_gate,
    main,
    run_scenario,
    write_run,
)
from tools.experiments.scenarios import (
    CHECK_IDS,
    SCENARIO_DIR,
    ScenarioError,
    load_scenarios,
    validate_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SCENARIOS = {
    "combat-hit-engine-damage",
    "combat-hp-agrees-engine",
    "combat-revived-foe-control",
    "combat-two-foes-one-down",
    "exploration-search-chest",
    "rp-persuade-captain",
    "spell-unsupported-claim-control",
    "spell-unsupported-no-claim",
}


def _raw(scenario_id: str) -> dict[str, Any]:
    return json.loads((SCENARIO_DIR / f"{scenario_id}.json").read_text(encoding="utf-8"))


def _by_id() -> dict[str, Any]:
    return {s.id: s for s in load_scenarios()}


def test_shipped_scenarios_validate_and_cover_the_three_check_families() -> None:
    scenarios = load_scenarios()
    assert {s.id for s in scenarios} == EXPECTED_SCENARIOS
    assert 6 <= len(scenarios) <= 8
    labels = {label for s in scenarios for label in s.labels}
    assert {"roster", "boundary", "mechanics-summary"} <= labels
    assert all(s.version == "1.0.0" for s in scenarios)


def test_scenario_schema_rejects_unknown_keys_and_bad_values() -> None:
    base = _raw("combat-hit-engine-damage")
    validate_scenario(copy.deepcopy(base))

    typo = copy.deepcopy(base)
    typo["expectt"] = {}
    with pytest.raises(ScenarioError, match="unknown keys"):
        validate_scenario(typo)

    bad_version = copy.deepcopy(base)
    bad_version["version"] = "one"
    with pytest.raises(ScenarioError, match="semver"):
        validate_scenario(bad_version)

    unknown_check = copy.deepcopy(base)
    unknown_check["expect"] = {"fun.overall": True}
    with pytest.raises(ScenarioError, match="unknown checks"):
        validate_scenario(unknown_check)

    missing_stage = copy.deepcopy(base)
    del missing_stage["scripted"]["play.narrator"]
    with pytest.raises(ScenarioError, match="scripted must provide"):
        validate_scenario(missing_stage)

    bad_parser_field = copy.deepcopy(base)
    bad_parser_field["scripted"]["play.parser"]["dice"] = "2d6"
    with pytest.raises(ScenarioError, match="unknown fields"):
        validate_scenario(bad_parser_field)

    bad_intent = copy.deepcopy(base)
    bad_intent["agency"]["expected_intent"] = "shopping"
    with pytest.raises(ScenarioError):
        validate_scenario(bad_intent)

    bad_hp = copy.deepcopy(base)
    bad_hp["engine"]["hp_after"] = {"Lyra": -1}
    with pytest.raises(ScenarioError, match="hp_after"):
        validate_scenario(bad_hp)


def test_duplicate_scenario_ids_are_rejected(tmp_path: Path) -> None:
    raw = _raw("exploration-search-chest")
    (tmp_path / "a.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ScenarioError, match="duplicate scenario id"):
        load_scenarios(tmp_path)


def test_offline_runner_produces_a_run_file_and_checks_behave_as_designed(tmp_path: Path) -> None:
    scenarios = load_scenarios()
    header, rows = build_run(
        scenarios, SCRIPTED_DM, human_reviewer=None, run_date="2026-10-17", sha="test-sha"
    )
    path = write_run(tmp_path / "runs", header, rows)
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert lines[0]["record"] == "run"
    assert lines[0]["provider_calls"] == 0
    assert lines[0]["mode"] == "offline-scripted"
    assert lines[0]["prompt_manifest"] == prompt_manifest()
    assert lines[0]["evaluator"]["fun_scoring"] == "human-only"
    assert len(lines) == 1 + len(scenarios)
    rows_by_id = {r["scenario_id"]: r for r in lines[1:]}
    assert set(rows_by_id) == EXPECTED_SCENARIOS
    for row in rows_by_id.values():
        assert row["stages_called"] == ["play.parser", "play.consequence_planner", "play.narrator"]
        assert set(row["checks"]) == set(CHECK_IDS)
        assert all(c["as_designed"] is not False for c in row["checks"].values())
        assert row["fun"]["status"] == "pending_human"
        assert row["fun"]["scores"] == {}

    # The deliberately failing controls must fail exactly where they were designed to.
    roster_control = rows_by_id["combat-revived-foe-control"]["checks"]
    assert roster_control["fidelity.roster"]["passed"] is False
    assert roster_control["fidelity.roster"]["as_designed"] is True
    claim_control = rows_by_id["spell-unsupported-claim-control"]["checks"]
    assert claim_control["fidelity.claimed_effects"]["passed"] is False
    assert "removed" in claim_control["fidelity.claimed_effects"]["detail"]
    # And the honest scenarios pass every check that is not designed to fail.
    assert rows_by_id["combat-hit-engine-damage"]["checks"]["fidelity.roster"]["passed"] is True
    assert rows_by_id["spell-unsupported-no-claim"]["checks"]["fidelity.claimed_effects"]["passed"]


def test_scripted_model_refuses_unscripted_stages() -> None:
    model = ScriptedModel({"play.parser": {"intent": "rp"}})
    assert model.complete("play.parser") == {"intent": "rp"}
    with pytest.raises(UnscriptedCall):
        model.complete("play.narrator")
    assert model.calls == ["play.parser"]


def test_a_changed_scripted_output_flips_the_check_it_targets() -> None:
    scenario = _by_id()["combat-hit-engine-damage"]
    honest = run_scenario(scenario)
    assert honest["checks"]["fidelity.roster"]["passed"] is True

    raw = _raw("combat-hit-engine-damage")
    raw["scripted"]["play.narrator"] = "The goblin rises and stabs you in the back."
    mutated = validate_scenario(raw, source="mutated")
    assert run_scenario(mutated)["checks"]["fidelity.roster"]["passed"] is False


def _outputs(**overrides: Any) -> RunOutputs:
    base: dict[str, Any] = {
        "player_input": "x",
        "parsed": ParsedInput(intent="combat"),
        "raw_narration": "",
        "final_narration": "",
        "raw_summary": "",
        "final_summary": "",
        "adjudicated_success": True,
        "combatants": (),
        "engine": {"check_rolled": True, "success": True, "hp_after": {"Lyra": 22}},
        "agency": {"expected_intent": "combat", "must_mention": []},
    }
    base.update(overrides)
    return RunOutputs(**base)


def test_hp_check_bites_when_prose_states_a_number_the_engine_did_not_record() -> None:
    ok = check_hp_numbers(
        _outputs(raw_narration="He is left with 9 HP.", engine={"hp_after": {"G": 9}})
    )
    assert ok.passed
    wrong = check_hp_numbers(
        _outputs(raw_narration="He is left with 4 HP.", engine={"hp_after": {"G": 9}})
    )
    assert not wrong.passed and "4" in wrong.detail


def test_mechanics_check_bites_on_outcome_disagreement_and_lost_roll_lines() -> None:
    engine = {"check_rolled": True, "success": True, "hp_after": {}}
    disagree = check_mechanics_summary(_outputs(engine=engine, adjudicated_success=False))
    assert not disagree.passed
    line = "Attack (d20 rolled 12 + 4 = 16 vs AC 14 -> success)."
    lost = check_mechanics_summary(
        _outputs(engine=engine, raw_summary=line, final_summary="The blade lands.")
    )
    assert not lost.passed
    kept = check_mechanics_summary(_outputs(engine=engine, raw_summary=line, final_summary=line))
    assert kept.passed


def test_consequence_check_needs_an_engine_roll_line_when_a_roll_happened() -> None:
    engine = {"check_rolled": True, "success": None, "hp_after": {}}
    assert not check_consequence_tied(
        _outputs(engine=engine, final_summary="Nothing rolled.")
    ).passed
    rolled = check_consequence_tied(
        _outputs(
            engine=engine, final_summary="Stealth (d20 rolled 9 + 2 = 11 vs DC 12 -> failure)."
        )
    )
    assert rolled.passed
    assert check_consequence_tied(_outputs(engine={"check_rolled": False, "hp_after": {}})).passed


def test_live_gate_needs_both_the_flag_and_the_environment_variable() -> None:
    assert live_gate(False, {}) is None
    assert live_gate(True, {}) is not None
    assert live_gate(True, {LIVE_ENV: "1"}) is not None
    assert live_gate(True, {LIVE_ENV: LIVE_ENV_VALUE}) is None


def test_live_request_without_authorization_makes_no_calls_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LIVE_ENV, raising=False)
    out = tmp_path / "runs"
    assert main(["run", "--live", "--out", str(out)]) == 2
    assert not out.exists()
    assert "live run refused" in capsys.readouterr().err


def test_authorized_live_request_only_estimates_and_does_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(LIVE_ENV, LIVE_ENV_VALUE)
    out = tmp_path / "runs"
    assert main(["run", "--live", "--out", str(out)]) == 3
    assert not out.exists()
    stdout = capsys.readouterr().out
    assert f"estimated logical stage calls: {estimated_live_calls(load_scenarios())}" in stdout


def test_estimate_counts_one_call_per_scripted_stage_per_scenario() -> None:
    scenarios = load_scenarios()
    assert estimated_live_calls(scenarios) == 3 * len(scenarios)
    assert estimated_live_calls(scenarios, repeats=5) == 15 * len(scenarios)


def test_run_records_refuse_the_repository_and_overwrites(tmp_path: Path) -> None:
    header, rows = build_run(
        load_scenarios()[:1], SCRIPTED_DM, human_reviewer=None, run_date="2026-10-17", sha="s"
    )
    with pytest.raises(SystemExit, match="inside the repository"):
        write_run(ROOT / "evals", header, rows)
    write_run(tmp_path, header, rows)
    with pytest.raises(SystemExit, match="already exists"):
        write_run(tmp_path, header, rows)


def test_run_id_is_stable_for_identical_inputs_and_separates_dm_settings() -> None:
    scenarios = load_scenarios()
    first, _ = build_run(
        scenarios, SCRIPTED_DM, human_reviewer=None, run_date="2026-10-17", sha="s"
    )
    again, _ = build_run(
        scenarios, SCRIPTED_DM, human_reviewer=None, run_date="2026-10-17", sha="s"
    )
    assert first["run_id"] == again["run_id"]
    other = runner_mod.DmSettings(label="other-dm", source="scripted-fake", stages={})
    moved, _ = build_run(scenarios, other, human_reviewer=None, run_date="2026-10-17", sha="s")
    assert moved["run_id"] != first["run_id"]


def test_dm_and_evaluator_settings_are_recorded_separately() -> None:
    dm = runner_mod.DmSettings(
        label="dm-low",
        source="scripted-fake",
        stages={
            "play.narrator": {"profile": "play.narrator", "model": "m-a", "reasoning_effort": "low"}
        },
    )
    header, _ = build_run(
        load_scenarios()[:1], dm, human_reviewer="reviewer-1", run_date="2026-10-17", sha="s"
    )
    assert header["dm"]["stages"]["play.narrator"]["reasoning_effort"] == "low"
    assert header["evaluator"]["human_reviewer"] == "reviewer-1"
    assert "reasoning_effort" not in header["evaluator"]
    assert "stages" not in header["evaluator"]


def test_golden_manifest_and_scenario_validation_commands_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["manifest"]) == 0
    assert main(["validate"]) == 0
    assert "8 scenario(s) valid" in capsys.readouterr().out
