"""Deterministic checks for one scenario run (EDG-059). No model is called and no score is guessed.

Every check is a pure function of what the run produced. The fidelity and claim checks reuse the
production helpers the engine itself applies (`agent.combatants.roster_violations`,
`agent.supported_rules.apply_narration_boundary` / `apply_adjudication_boundary` /
`ENGINE_ROLL_LINE`), so a check fails exactly when the shipped guard would have had to act.

Fun is never checked here. It is a human rubric (`evals/fun_rubric.json`) recorded separately.

CHECKER_VERSION is recorded in every run's evaluator block. Bump it when a check's rule changes,
so two runs scored by different rules are not silently compared.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.combatants import roster_violations
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.supported_rules import ENGINE_ROLL_LINE

CHECKER_VERSION = "1"
_SENT = re.compile(r"(?<=[.!?])\s+")
_HP_NUMBER = re.compile(r"(\d+)\s*(?:hp|hit\s+points)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RunOutputs:
    """Everything a deterministic check may read. Raw = the model's text before the guards ran."""

    player_input: str
    parsed: ParsedInput
    raw_narration: str
    final_narration: str
    raw_summary: str
    final_summary: str
    adjudicated_success: bool
    combatants: tuple[dict[str, Any], ...]
    engine: dict[str, Any]
    agency: dict[str, Any]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def check_intent_parsed(out: RunOutputs) -> CheckResult:
    want = out.agency["expected_intent"]
    got = out.parsed.intent
    return CheckResult(got == want, f"parsed intent {got!r}, expected {want!r}")


def check_target_acknowledged(out: RunOutputs) -> CheckResult:
    terms = out.agency["must_mention"]
    low = out.final_narration.lower()
    missing = [t for t in terms if t.lower() not in low]
    return CheckResult(
        not missing, "all named terms narrated" if not missing else f"missing {missing}"
    )


def check_consequence_tied(out: RunOutputs) -> CheckResult:
    if not out.engine["check_rolled"]:
        return CheckResult(True, "no engine roll for this action")
    tied = any(ENGINE_ROLL_LINE.search(s) for s in _sentences(out.final_summary))
    return CheckResult(
        tied, "engine roll line present" if tied else "no engine roll line in summary"
    )


def check_roster(out: RunOutputs) -> CheckResult:
    problems = roster_violations(out.raw_narration, out.combatants)
    return CheckResult(
        not problems, "no revived or invented attacker" if not problems else f"roster: {problems}"
    )


def check_claimed_effects(out: RunOutputs) -> CheckResult:
    """The model narrated effects the narration guard had to remove."""
    kept = out.final_narration
    removed = [s for s in _sentences(out.raw_narration) if s not in kept]
    if not removed:
        return CheckResult(True, "no unsupported claim removed by the narration guard")
    return CheckResult(
        False, f"guard removed {len(removed)} claim sentence(s): {removed[0][:120]!r}"
    )


def check_hp_numbers(out: RunOutputs) -> CheckResult:
    """Any HP figure printed in prose must be one the engine recorded for that turn."""
    allowed = {int(v) for v in out.engine["hp_after"].values()}
    stated = [int(m.group(1)) for m in _HP_NUMBER.finditer(out.raw_narration)]
    wrong = [n for n in stated if n not in allowed]
    if wrong:
        return CheckResult(False, f"narration states HP {wrong}, engine recorded {sorted(allowed)}")
    return CheckResult(True, f"{len(stated)} HP figure(s) agree with the engine")


def check_mechanics_summary(out: RunOutputs) -> CheckResult:
    """Engine outcome agrees with the summary, and engine roll lines survive the summary guard."""
    expected = out.engine["success"]
    if expected is not None and out.adjudicated_success != expected:
        return CheckResult(False, f"planner success {out.adjudicated_success}, engine {expected}")
    raw_lines = [s for s in _sentences(out.raw_summary) if ENGINE_ROLL_LINE.search(s)]
    lost = [s for s in raw_lines if s not in out.final_summary]
    if lost:
        return CheckResult(False, f"summary guard dropped {len(lost)} engine roll line(s)")
    return CheckResult(True, f"outcome agrees; {len(raw_lines)} engine roll line(s) kept")


CHECKS = {
    "agency.intent_parsed": check_intent_parsed,
    "agency.target_acknowledged": check_target_acknowledged,
    "agency.consequence_tied": check_consequence_tied,
    "fidelity.roster": check_roster,
    "fidelity.claimed_effects": check_claimed_effects,
    "fidelity.hp_numbers": check_hp_numbers,
    "fidelity.mechanics_summary": check_mechanics_summary,
}


def run_checks(out: RunOutputs) -> dict[str, CheckResult]:
    return {check_id: fn(out) for check_id, fn in CHECKS.items()}


def make_planner(raw: dict[str, Any]) -> AdjudicationResult:
    return AdjudicationResult(**{"success": False, **raw})
