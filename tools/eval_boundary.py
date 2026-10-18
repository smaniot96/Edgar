"""Offline evaluation of the EDG-147 deterministic heuristics over frozen real-model fixtures.

Re-runs, with no model call and no database:
  * `agent.supported_rules.classify_unsupported_actions` against the parser's real ParsedInput,
  * the `lethality` field of ParsedInput and `agent.combatants.declared_lethality` (field first,
    then the player's words; and the text-only path used by `agent.captives`),
  * `agent.combatants.roster_violations` against the real narrator outputs,
  * `apply_narration_boundary` / `apply_adjudication_boundary` on those outputs and summaries.

Fixtures live in tests/fixtures/real_model/ (see its README.md). Labels are human judgement and are
stored in the fixture files. Run: `uv run python tools/eval_boundary.py [--examples N]`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "real_model"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.combatants import (  # noqa: E402
    classify_combat_maneuver,
    declared_lethality,
    roster_violations,
)
from agent.models.adjudication import AdjudicationResult  # noqa: E402
from agent.models.parsed_input import ParsedInput  # noqa: E402
from agent.spells import cast_request  # noqa: E402
from agent.supported_rules import (  # noqa: E402
    ENGINE_ROLL_LINE,
    EngineApplied,
    actions_for_state,
    apply_adjudication_boundary,
    apply_narration_boundary,
    classify_unsupported_actions,
    engine_applied_for_state,
)

CHARACTER = {"name": "Lyra", "class": "Wizard", "level": 5, "hp_current": 22, "hp_max": 22}
KINDS = ("spell", "feature", "maneuver", "condition", "advantage", "action_economy", "positioning")
_SENT = re.compile(r"(?<=[.!?])\s+")


SETS = {
    # tuned against while the heuristics were being fixed (in-sample)
    "in_sample": ("parse_fixtures.json", "narration_fixtures.json"),
    # written and labelled before its model run; never used to choose a fix (held-out)
    "held_out": ("heldout_fixtures.json", "heldout_fixtures.json"),
}


def load(name: str = "in_sample") -> tuple[dict, dict]:
    parse_file, narr_file = SETS[name]
    parse = json.loads((FIXTURES / parse_file).read_text())
    narr = json.loads((FIXTURES / narr_file).read_text())
    parse.setdefault("character", CHARACTER)
    if "calls_heldout_total" in parse:  # the held-out file counts its own parse and narration calls
        parse["calls_parse"] = parse["calls_heldout_total"]
    parse.setdefault("date", narr.get("date", ""))
    narr.setdefault("calls_total_this_run", 0)
    return parse, narr


def _engine_effects(text: str, mode: str, parsed: ParsedInput | None, character: dict) -> tuple:
    """Approximation of the typed effects the engine applies this turn (no engine run here).

    A supported grapple or prone maneuver is applied in combat; an executed condition spell (Blindness, Hold Person, Guiding Bolt) is assumed to have taken
    hold (its save is not in the fixture).
    """
    effects: list[str] = []
    if mode == "combat":
        maneuver = classify_combat_maneuver(text)
        if maneuver is not None and maneuver.kind in ("grappled", "prone"):
            effects.append(maneuver.kind)
    request = cast_request(text, parsed, character)
    if request is not None and request[2] is None and request[0].condition:
        effects.append(request[0].condition)  # a cast condition spell is assumed to have taken hold
    return tuple(effects)


def _prf(tp: int, fp: int, fn: int) -> tuple[float | None, float | None]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return precision, recall


def predict_kinds(item: dict, character: dict = CHARACTER) -> set[str]:
    """The unsupported kinds the classifier gives one labelled item (the production call shape)."""
    parsed = ParsedInput(**item["parsed"])
    effects = _engine_effects(item["text"], item["mode"], parsed, character)
    actions = classify_unsupported_actions(
        item["text"], parsed, character, applied=EngineApplied(effects=effects)
    )
    return {a.kind for a in actions}


def predict_lethality(item: dict) -> str | None:
    """The declared intent production uses for one labelled item (parser field, then words)."""
    return declared_lethality(ParsedInput(**item["parsed"]), item["text"])


def eval_classifier(parse: dict, examples: int) -> dict[str, Any]:
    character = parse["character"]
    counts = {k: {"tp": 0, "fp": 0, "fn": 0} for k in KINDS}
    exact = total = bait_total = bait_fp = 0
    by_lang: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [exact, total]
    fp_examples: dict[str, list[str]] = defaultdict(list)
    fn_examples: dict[str, list[str]] = defaultdict(list)
    for item in parse["items"]:
        if item["flag"] == "ambiguous" or item["parsed"] is None:
            continue
        predicted = predict_kinds(item, character)
        expected = set(item["expected"]["kinds"])
        total += 1
        by_lang[item["lang"]][1] += 1
        if predicted == expected:
            exact += 1
            by_lang[item["lang"]][0] += 1
        if not expected:
            bait_total += 1
            if predicted:
                bait_fp += 1
        for kind in KINDS:
            if kind in predicted and kind in expected:
                counts[kind]["tp"] += 1
            elif kind in predicted:
                counts[kind]["fp"] += 1
                fp_examples[kind].append(f"{item['id']} {item['text']!r}")
            elif kind in expected:
                counts[kind]["fn"] += 1
                fn_examples[kind].append(f"{item['id']} {item['text']!r}")
    rows = {}
    for kind in KINDS:
        c = counts[kind]
        precision, recall = _prf(c["tp"], c["fp"], c["fn"])
        rows[kind] = {**c, "precision": precision, "recall": recall}
    return {
        "items": total,
        "exact_match": exact,
        "bait_items": bait_total,
        "bait_with_any_ruling": bait_fp,
        "by_kind": rows,
        "by_lang": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_lang.items())},
        "examples": {
            "false_positive": {k: v[:examples] for k, v in fp_examples.items()},
            "false_negative": {k: v[:examples] for k, v in fn_examples.items()},
        },
    }


def eval_lethality(parse: dict, examples: int) -> dict[str, Any]:
    report: dict[str, Any] = {}
    channels = {
        "parser_field": lambda p, t: p.lethality,
        "production (field, then words)": lambda p, t: declared_lethality(p, t),
        "words_only (captives)": lambda p, t: declared_lethality(None, t),
    }
    for name, fn in channels.items():
        correct = fp = fn_count = wrong = 0
        examples_list: list[str] = []
        for item in parse["items"]:
            if item["flag"] == "ambiguous" or item["parsed"] is None:
                continue
            parsed = ParsedInput(**item["parsed"])
            got = fn(parsed, item["text"])
            want = item["expected"]["lethality"]
            if got == want:
                correct += 1
            elif want is None:
                fp += 1
                examples_list.append(f"{item['id']} {item['text']!r} -> {got}, expected none")
            elif got is None:
                fn_count += 1
                examples_list.append(f"{item['id']} {item['text']!r} -> none, expected {want}")
            else:
                wrong += 1
                examples_list.append(f"{item['id']} {item['text']!r} -> {got}, expected {want}")
        report[name] = {
            "correct": correct,
            "false_positive": fp,
            "false_negative": fn_count,
            "wrong_direction": wrong,
            "examples": examples_list[:examples],
        }
    return report


def _combatants(scenario: dict) -> list[dict]:
    return list((scenario.get("combat_state") or {}).get("combatants") or [])


def _scenario_actions(scenario: dict, parsed: ParsedInput | None) -> tuple:
    character = {"name": "Lyra", "class": "Wizard", "level": 5}
    state = {
        "player_input": scenario["player_input"],
        "parsed_input": parsed,
        "character": character,
        "engine_roll_mode": "normal",
        "engine_effects": _engine_effects(
            scenario["player_input"], scenario["mode"], parsed, character
        ),
    }
    applied = engine_applied_for_state(state)
    return actions_for_state(state), applied


def eval_roster(narr: dict, examples: int) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    flagged: list[str] = []
    for scenario in narr["scenarios"]:
        for index, sample in enumerate(scenario["samples"]):
            if not sample.get("text"):
                continue
            label = bool(sample["labels"]["revives_or_invents_attacker"])
            found = bool(roster_violations(sample["text"], _combatants(scenario)))
            if found and label:
                tp += 1
            elif found:
                fp += 1
                flagged.append(f"{scenario['id']}[{index}] {sample['text'][:160]!r}")
            elif label:
                fn += 1
            else:
                tn += 1
    precision, recall = _prf(tp, fp, fn)
    return {
        "samples": tp + fp + tn + fn,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "examples": flagged[:examples],
    }


def eval_narration(narr: dict, examples: int) -> dict[str, Any]:
    """Sentence-level: sentences the narration sanitizer removed, against the human labels."""
    removed_total = removed_benign = removed_claim = kept_claim = 0
    removed_examples: list[str] = []
    scenarios_seen = 0
    for scenario in narr["scenarios"]:
        parsed = ParsedInput(**scenario["parsed"]) if scenario.get("parsed") else None
        actions, applied = _scenario_actions(scenario, parsed)
        if not actions:
            continue
        scenarios_seen += 1
        for index, sample in enumerate(scenario["samples"]):
            text = sample.get("text")
            if not text:
                continue
            out = apply_narration_boundary(text, actions, applied=applied)
            claims = set(sample["labels"].get("claim_sentences") or [])
            kept_text = out
            for sentence in (s for s in _SENT.split(text) if s.strip()):
                kept = sentence.strip() in kept_text
                is_claim = sentence.strip() in claims
                if not kept:
                    removed_total += 1
                    if is_claim:
                        removed_claim += 1
                    else:
                        removed_benign += 1
                        removed_examples.append(f"{scenario['id']}[{index}] {sentence.strip()!r}")
                elif is_claim:
                    kept_claim += 1
    return {
        "scenarios_with_unsupported_action": scenarios_seen,
        "sentences_removed": removed_total,
        "removed_benign_false_positive": removed_benign,
        "removed_claim_true_positive": removed_claim,
        "kept_claim_false_negative": kept_claim,
        "examples": removed_examples[:examples],
    }


def eval_adjudication(narr: dict, examples: int) -> dict[str, Any]:
    """The engine's own roll lines in mechanical_summary must survive the sanitizer."""
    checked = lost = 0
    lost_examples: list[str] = []
    for scenario in narr["scenarios"]:
        parsed = ParsedInput(**scenario["parsed"]) if scenario.get("parsed") else None
        actions, applied = _scenario_actions(scenario, parsed)
        if not actions:
            continue
        summary = scenario["adjudication"]["mechanical_summary"]
        roll_sentences = [s.strip() for s in _SENT.split(summary) if ENGINE_ROLL_LINE.search(s)]
        adj = AdjudicationResult(**{"success": False, **scenario["adjudication"]})
        apply_adjudication_boundary(adj, actions, applied=applied)
        for sentence in roll_sentences:
            checked += 1
            if sentence not in adj.mechanical_summary:
                lost += 1
                lost_examples.append(f"{scenario['id']}: {sentence!r}")
    return {
        "roll_lines_checked": checked,
        "roll_lines_lost": lost,
        "examples": lost_examples[:examples],
    }


def evaluate(name: str = "in_sample", examples: int = 5) -> dict[str, Any]:
    parse, narr = load(name)
    return {
        "set": name,
        "model": parse["model"],
        "parse_date": parse["date"],
        "narration_date": narr["date"],
        "calls": parse.get("calls_parse", 0) + narr.get("calls_total_this_run", 0),
        "classifier": eval_classifier(parse, examples),
        "lethality": eval_lethality(parse, examples),
        "roster": eval_roster(narr, examples),
        "narration_sanitizer": eval_narration(narr, examples),
        "adjudication_sanitizer": eval_adjudication(narr, examples),
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _print_report(result: dict[str, Any]) -> None:
    print(
        f"== {result['set']}: model {result['model']} parse {result['parse_date']} "
        f"model calls {result['calls']}"
    )
    c = result["classifier"]
    print(
        f"classifier: {c['exact_match']}/{c['items']} exact; bait with a ruling "
        f"{c['bait_with_any_ruling']}/{c['bait_items']}; by lang {c['by_lang']}"
    )
    for kind, row in c["by_kind"].items():
        print(
            f"  {kind:15} tp={row['tp']} fp={row['fp']} fn={row['fn']} "
            f"precision={_fmt(row['precision'])} recall={_fmt(row['recall'])}"
        )
    for kind, rows in c["examples"]["false_positive"].items():
        for line in rows:
            print(f"    FP {kind}: {line}")
    for kind, rows in c["examples"]["false_negative"].items():
        for line in rows:
            print(f"    FN {kind}: {line}")
    print("lethality:")
    for name, row in result["lethality"].items():
        print(
            f"  {name:32} correct={row['correct']} fp={row['false_positive']} "
            f"fn={row['false_negative']} wrong={row['wrong_direction']}"
        )
        for line in row["examples"]:
            print(f"    {line}")
    r = result["roster"]
    print(
        f"roster (real narrations): samples={r['samples']} tp={r['tp']} fp={r['fp']} "
        f"tn={r['tn']} fn={r['fn']} precision={_fmt(r['precision'])} recall={_fmt(r['recall'])}"
    )
    for line in r["examples"]:
        print(f"    FP: {line}")
    n = result["narration_sanitizer"]
    print(
        f"narration sanitizer: scenarios={n['scenarios_with_unsupported_action']} "
        f"removed={n['sentences_removed']} benign_removed(FP)={n['removed_benign_false_positive']} "
        f"claim_removed(TP)={n['removed_claim_true_positive']} claim_kept(FN)={n['kept_claim_false_negative']}"
    )
    for line in n["examples"]:
        print(f"    FP: {line}")
    a = result["adjudication_sanitizer"]
    print(
        f"adjudication sanitizer: engine roll lines checked={a['roll_lines_checked']} "
        f"lost={a['roll_lines_lost']}"
    )
    for line in a["examples"]:
        print(f"    LOST: {line}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--set", choices=sorted(SETS), action="append")
    args = parser.parse_args()
    names = args.set or list(SETS)
    results = [evaluate(name, args.examples) for name in names]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return
    for result in results:
        _print_report(result)


if __name__ == "__main__":
    main()
