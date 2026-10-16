"""EDG-054's frozen-source retrieval and applied-ruling regression harness."""

from __future__ import annotations

from tools.eval_fidelity import CORPUS_PATH, load_corpus, run_evaluation, validate_provenance


def test_frozen_fidelity_fixture_has_pinned_provenance_and_excerpt_hashes() -> None:
    corpus = load_corpus()
    validate_provenance(corpus)

    assert corpus["provider_calls"] == 0
    assert corpus["documents"]["phb"]["pages"] == [73, 258]
    assert corpus["documents"]["mm"]["pages"] == [99, 167, 292]
    assert "incomplete_stat_fields" in corpus["documents"]["mm"]["quality"]
    assert (
        corpus["documents"]["lost_mine"]["source_sha256"]
        == corpus["documents"]["lost_mine"]["extraction_sha256"]
    )
    assert {passage["id"] for passage in corpus["passages"]} >= {
        "phb-second-wind",
        "phb-magic-missile",
        "mm-goblin",
        "mm-troll",
        "mm-adult-red-dragon",
        "module-dc13",
    }
    assert CORPUS_PATH.is_file()


def test_offline_fidelity_regression_reports_retrieval_and_ruling_accuracy_separately() -> None:
    report = run_evaluation()

    assert report["provider_calls"] == 0
    assert report["all_passed"] is True
    assert report["retrieval"]["relevant_hits"] == 6
    assert report["retrieval"]["positive_cases"] == 6
    assert report["retrieval"]["identity_correct"] == 6
    assert report["retrieval"]["negative_passes"] == 5
    assert report["retrieval"]["negative_cases"] == 5
    assert report["applied_rulings"]["correct"] == 6
    assert report["applied_rulings"]["cases"] == 6
    assert report["retry_replacement"]["passed"] is True
    assert report["retry_replacement"]["before_retry"]["hp"] == 84
    assert report["retry_replacement"]["after_retry"]["hp"] == 85
