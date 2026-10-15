"""EDG-147: the deterministic boundary heuristics against frozen real-model fixtures.

The fixtures (tests/fixtures/real_model/) hold real parser outputs and real narrator outputs from the
configured model, with human labels. No model is called here. The thresholds are the measured
behaviour after the EDG-147 fixes; every fixed false positive and false negative is pinned by id.
The rows marked synthetic below are hand-written regression cases, not model outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.combatants import declared_lethality, roster_violations
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.supported_rules import (
    DECLARATION_LANGUAGE,
    apply_adjudication_boundary,
    apply_narration_boundary,
    classify_unsupported_actions,
    contract_payload,
)
from tools.eval_boundary import CHARACTER, SETS, evaluate, load, predict_kinds, predict_lethality

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "real_model"

# Rows whose baseline (pre-EDG-147) prediction was wrong, and the rows added to pin the fixes.
# Each id is checked against its human label for both the classifier and the lethality channel.
PINNED = {
    "in_sample": [
        "S07",
        "S09",
        "I10",
        "I11",
        "I15",
        "L15",
        "U18",
        "B04",
        "B05",
        "B11",
        "B18",
        "B19",
        "B12",
        "B15",
        "U12",
        "U13",
        "I02",
        "I06",
        "I03",
        "I07",
        "L04",
        "L17",
        "L18",
        "L21",
        "L23",
        "I16",
        "I17",
        "I18",
        "I19",
        "C02",
        "C06",
        "U09",
        "U29",
        "U01",
    ],
    "held_out": [
        "H15",
        "H16",
        "H19",
        "H25",
        "H27",
        "H21",
        "H22",
        "H23",
        "H24",
        "H29",
    ],
}
# The one known production lethality false positive per set: a parser-declared nonlethal with no word
# cue, kept because the EDG-022 contract pins that a parser-declared nonlethal stands alone.
KNOWN_NONLETHAL_FALSE_POSITIVE = {"in_sample": "U27", "held_out": "H11"}


@pytest.fixture(scope="module")
def results() -> dict:
    return {name: evaluate(name, examples=50) for name in SETS}


@pytest.fixture(scope="module")
def items() -> dict:
    out = {}
    for name in SETS:
        parse, _ = load(name)
        out[name] = {item["id"]: item for item in parse["items"]}
    return out


def test_fixture_provenance_budget_and_no_secret() -> None:
    parse = json.loads((FIXTURES / "parse_fixtures.json").read_text())
    narr = json.loads((FIXTURES / "narration_fixtures.json").read_text())
    held = json.loads((FIXTURES / "heldout_fixtures.json").read_text())
    assert parse["model"] == narr["model"] == held["model"] == "gpt-6-luna"
    assert parse["parser_max_tokens"] <= 700 and narr["narrator_max_tokens"] <= 700
    total = parse["calls_parse"] + narr["calls_total_this_run"] + held["calls_heldout_total"]
    assert total <= 300  # the owner's budget
    text = "".join(p.read_text() for p in FIXTURES.glob("*.json"))
    assert "sk-" not in text and "OPENAI_API_KEY" not in text  # no key material in fixtures
    assert parse["labelling_rule"] and held["labelling_rule"]


@pytest.mark.parametrize("name", list(SETS))
def test_classifier_thresholds_on_real_inputs(results: dict, name: str) -> None:
    c = results[name]["classifier"]
    assert c["bait_with_any_ruling"] == 0  # idioms and figurative uses never get a ruling
    assert c["exact_match"] / c["items"] >= 0.98
    for kind, row in c["by_kind"].items():
        if row["precision"] is not None:
            assert row["precision"] >= 0.95, kind
        if row["recall"] is not None:
            assert row["recall"] >= 0.95, kind


@pytest.mark.parametrize("name", list(SETS))
def test_lethality_channel_has_no_wrong_or_missed_declaration(results: dict, name: str) -> None:
    production = results[name]["lethality"]["production (field, then words)"]
    assert production["wrong_direction"] == 0 and production["false_negative"] == 0
    flagged = [e.split()[0] for e in production["examples"]]
    assert flagged == [KNOWN_NONLETHAL_FALSE_POSITIVE[name]] if flagged else False
    captives = results[name]["lethality"]["words_only (captives)"]
    assert captives["false_positive"] == 0 and captives["false_negative"] == 0


@pytest.mark.parametrize("name", list(SETS))
def test_roster_check_never_flags_a_real_narration(results: dict, name: str) -> None:
    roster = results[name]["roster"]
    assert roster["fp"] == 0
    assert roster["tp"] == 0  # no revived or invented attacker occurred in the frozen real samples


@pytest.mark.parametrize("name", list(SETS))
def test_narration_sanitizer_removes_no_benign_sentence(results: dict, name: str) -> None:
    sanitizer = results[name]["narration_sanitizer"]
    assert sanitizer["removed_benign_false_positive"] == 0
    assert sanitizer["kept_claim_false_negative"] == 0


@pytest.mark.parametrize("name", list(SETS))
def test_adjudication_sanitizer_keeps_every_engine_roll_line(results: dict, name: str) -> None:
    adjudication = results[name]["adjudication_sanitizer"]
    assert adjudication["roll_lines_checked"] > 0
    assert adjudication["roll_lines_lost"] == 0


@pytest.mark.parametrize("name", list(SETS))
def test_pinned_real_rows_match_their_labels(items: dict, name: str) -> None:
    for item_id in PINNED[name]:
        item = items[name][item_id]
        assert predict_kinds(item) == set(item["expected"]["kinds"]), item_id
        assert predict_lethality(item) == item["expected"]["lethality"], item_id


def test_parser_lethal_is_not_inferred_without_words() -> None:
    # Pinned from the real parser output (EDG-147): "stab through the heart" has no declaration.
    assert (
        declared_lethality(
            ParsedInput(lethality="lethal"), "I stab the goblin captain through the heart."
        )
        is None
    )
    assert (
        declared_lethality(ParsedInput(lethality="nonlethal"), "I stab the goblin") == "nonlethal"
    )
    assert declared_lethality(ParsedInput(lethality="lethal"), "I knock him out") == "lethal"


# --- synthetic regression cases (hand-written wording; not model outputs) ---------------------------


def _foe(name: str, disposition: str) -> dict:
    return {
        "name": name,
        "is_player": False,
        "hp_current": 0 if disposition in ("dead", "unconscious") else 7,
        "disposition": disposition,
        "alive": disposition != "dead",
        "effects": [],
    }


def test_synthetic_inactive_foe_cannot_act_but_its_defeat_is_narrated() -> None:
    roster = [_foe("Goblin", "dead"), _foe("Goblin Captain", "active")]
    assert roster_violations("The Goblin lunges at you.", roster) == ["Goblin"]
    assert (
        roster_violations("Your blade cuts the goblin down; it staggers and falls dead.", roster)
        == []
    )
    assert (
        roster_violations(
            "Your sword strikes the goblin, and it collapses dead. The Goblin Captain lunges at you, "
            "but its attack cuts through empty air.",
            roster,
        )
        == []
    )


def test_synthetic_another_attacker_needs_a_second_active_creature() -> None:
    both_active = [_foe("Goblin", "active"), _foe("Goblin Captain", "active")]
    assert roster_violations("The other goblin remains poised nearby.", both_active) == []
    one_active = [_foe("Raider", "active"), _foe("Raider Captain", "dead")]
    assert len(roster_violations("Another raider lunges at you.", one_active)) == 1


def test_synthetic_idioms_and_figurative_uses_are_not_declarations() -> None:
    for text in (
        "I take advantage of the guard's distraction to slip past.",
        "I cast my net into the river.",
        "I cast an eye over the shelves.",
        "I rage against the machine.",
        "I block the door with the table.",
        "I kill the lights and sneak in.",
    ):
        assert classify_unsupported_actions(text, None, CHARACTER) == (), text
    assert declared_lethality(None, "I kill time reading a book.") is None


def test_synthetic_non_english_words_are_read_and_english_names_are_required() -> None:
    def kinds(text: str, parsed: ParsedInput | None = None) -> list[str]:
        return [a.kind for a in classify_unsupported_actions(text, parsed, CHARACTER)]

    assert kinds("Attacco il goblin con vantaggio.") == ["advantage"]
    assert kinds("Ataco con ventaja.") == ["advantage"]
    assert kinds("Mi metto a copertura dietro la colonna.") == ["positioning"]
    assert kinds("Mi lancio sul goblin con la spada.") == []  # "lancio" alone is a throw
    named = ParsedInput(intent="combat", entities={"spell": "Magic Missile"})
    (action,) = classify_unsupported_actions("Lancio Dardo Magico sul goblin.", named, CHARACTER)
    assert action.kind == "spell" and "not resolved" in action.ruling


def test_synthetic_negated_spell_ruling_is_kept_and_a_claim_is_removed() -> None:
    actions = classify_unsupported_actions(
        "I cast Eldritch Blast at the goblin captain.", None, CHARACTER
    )
    narration = (
        "You speak the words of *Eldritch Blast*, but the magic finds no purchase. "
        "The captain's blow lands, and the captain is paralyzed in place."
    )
    cleaned = apply_narration_boundary(narration, actions)
    assert "finds no purchase" in cleaned
    assert "paralyzed" not in cleaned and "Narrative ruling" in cleaned


def test_synthetic_engine_roll_line_survives_while_prose_claim_is_stripped() -> None:
    actions = classify_unsupported_actions("I cast Eldritch Blast at the goblin.", None, CHARACTER)
    adj = AdjudicationResult(
        success=False,
        mechanical_summary=(
            "Eldritch blast hits the goblin for 3d4 force damage. "
            "Athletics check: 1d20+2 = 15 vs DC 15, success."
        ),
    )
    apply_adjudication_boundary(adj, actions)
    assert "Athletics check: 1d20+2 = 15 vs DC 15, success." in adj.mechanical_summary
    assert "3d4" not in adj.mechanical_summary


def test_declaration_language_is_declared_in_the_contract_and_the_readme() -> None:
    assert contract_payload()["declaration_language"] == DECLARATION_LANGUAGE
    assert "English" in DECLARATION_LANGUAGE
    assert DECLARATION_LANGUAGE in (ROOT / "README.md").read_text()
