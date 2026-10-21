"""Focused provider accounting checks: measured tokens, estimates and unknown receipts."""

from __future__ import annotations

import pytest

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep.store import Store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    value = Store("usage-probe")
    value.initialize({"source_kind": "ai"})
    return value


def semantic(store, usage, *, dispatch="semantic-1", request=None, response=None):
    store.dispatch(
        dispatch,
        "review",
        request
        or {"model": "gpt-6-luna", "reasoning": {"effort": "medium"}, "service_tier": "default"},
    )
    store.response(
        dispatch,
        {"id": "resp-" + dispatch, "status": "completed", "usage": usage, **(response or {})},
    )
    return store.usage()


def tokens(input_tokens, output_tokens, *, cached=0, writes_30m=0, writes_12h=0):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_tokens_details": {
            "cached_tokens": cached,
            "cache_write_tokens": writes_30m + writes_12h,
            "cache_write_tokens_details": {
                "ttl_30m_tokens": writes_30m,
                "ttl_12h_tokens": writes_12h,
            },
        },
        "output_tokens_details": {"reasoning_tokens": output_tokens // 2},
    }


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (tokens(1000, 200), 200),
        (tokens(10000, 1000, cached=4000), 1140),
        (tokens(2000, 0, writes_30m=1000), 225),
        (tokens(2000, 0, writes_12h=1000), 350),
        (tokens(12000, 1000, cached=4000, writes_30m=3000, writes_12h=4000), 2015),
        (tokens(1, 1), 1),
    ],
)
def test_standard_input_cache_reads_and_write_lifetimes(store, usage, expected):
    result = semantic(store, usage)
    call = result["calls"][0]
    assert call["usage"] == usage
    assert call["cost_microusd"] == call["cost_estimate_microusd"] == expected
    assert result["cost_microusd"] == result["known_cost_microusd"] == expected
    assert result["semantic_cost_microusd"] == expected
    assert result["cost_estimate_complete"] and result["unknown_calls"] == 0
    assert call["reasoning"] == "medium"
    assert result["billed_cost_microusd"] is None and call["billed_cost_microusd"] is None
    assert result["pricing"]["kind"] == "published_rate_estimate_not_billed"
    assert result["pricing"]["verified_date"] == "2026-10-21"
    assert result["pricing"]["semantic"]["sources"][0].endswith("/models/gpt-6-luna")


@pytest.mark.parametrize(
    ("usage", "expected", "long_context"),
    [
        (tokens(272000, 1000), 27700, False),
        (tokens(272001, 1000), 55151, True),
        (tokens(272001, 1000, cached=1000, writes_30m=400, writes_12h=600), 55171, True),
    ],
)
def test_long_context_rates_apply_to_entire_request_over_threshold(
    store, usage, expected, long_context
):
    result = semantic(store, usage)
    assert result["cost_microusd"] == expected
    assert result["calls"][0]["long_context_rates"] is long_context


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 1000},
        {"input_tokens": True, "output_tokens": 100},
        {"input_tokens": -1, "output_tokens": 100},
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "input_tokens_details": {"cache_write_tokens": 500},
        },
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "input_tokens_details": {"cached_tokens": 1001},
        },
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "input_tokens_details": {
                "cache_write_tokens": 500,
                "cache_write_tokens_details": {"ttl_30m_tokens": 400, "ttl_12h_tokens": 200},
            },
        },
    ],
)
def test_unknown_or_inconsistent_usage_retains_unknown_cost(store, usage):
    result = semantic(store, usage)
    assert result["calls"][0]["cost_microusd"] is None
    assert result["unknown_calls"] == result["unknown_semantic_calls"] == 1
    assert not result["cost_estimate_complete"]
    assert result["known_cost_microusd"] == 0
    assert result["cost_basis"].endswith("known_usage_only")
    assert result["billed_cost_microusd"] is None


def test_ambiguous_submission_stays_unknown_alongside_known_cost(store):
    semantic(store, tokens(1000, 200))
    store.dispatch("ambiguous", "chapters", {"model": "gpt-6-luna"})
    result = Store(store.slug).usage()
    assert result["call_count"] == 2
    assert result["cost_microusd"] == result["known_cost_microusd"] == 200
    assert result["unknown_calls"] == 1 and not result["cost_estimate_complete"]
    assert result["calls"][1]["cost_microusd"] is None
    assert result["calls"][1]["response_id"] is None


@pytest.mark.parametrize("response", [{"model": "unpriced-model"}, {"service_tier": "priority"}])
def test_different_model_or_tier_is_never_priced_as_standard_luna(store, response):
    result = semantic(store, tokens(1000, 200), response=response)
    assert result["calls"][0]["cost_microusd"] is None
    assert result["unknown_calls"] == 1


def test_embedding_receipts_are_separate_from_semantic_calls_and_share_total_cost(store):
    semantic(store, tokens(1000, 200))
    store.dispatch("embed-1", "embedding", {"model": "text-embedding-3-small", "input": ["text"]})
    store.response(
        "embed-1",
        {
            "status": "completed",
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 10000, "total_tokens": 10000},
        },
    )
    result = store.usage()
    assert result["call_count"] == 1 and result["embedding_call_count"] == 1
    assert result["input_tokens"] == 1000 and result["output_tokens"] == 200
    assert result["embedding_input_tokens"] == 10000
    assert result["semantic_cost_microusd"] == result["embedding_cost_microusd"] == 200
    assert result["cost_microusd"] == result["known_cost_microusd"] == 400
    assert result["cost_estimate_complete"] and result["unknown_calls"] == 0
    assert result["embedding_calls"][0]["model"] == "text-embedding-3-small"
    assert result["pricing"]["embedding"]["usd_per_million_tokens"] == "0.02"


def test_unpriced_embedding_model_and_ambiguous_embedding_remain_unknown(store):
    store.dispatch("embed-1", "embedding", {"model": "other-embedding-model"})
    store.response("embed-1", {"status": "completed", "usage": {"prompt_tokens": 1000}})
    store.dispatch("embed-2", "embedding", {"model": "text-embedding-3-small"})
    result = store.usage()
    assert result["call_count"] == 0 and result["embedding_call_count"] == 2
    assert result["embedding_input_tokens"] == 1000
    assert result["unknown_calls"] == result["unknown_embedding_calls"] == 2
    assert result["unknown_semantic_calls"] == 0 and not result["cost_estimate_complete"]
    assert all(call["cost_microusd"] is None for call in result["embedding_calls"])


def test_embedding_total_fallback_and_whole_microusd_rounding(store):
    store.dispatch("embed-1", "embedding", {"model": "text-embedding-3-small"})
    store.response("embed-1", {"status": "completed", "usage": {"total_tokens": 51}})
    result = store.usage()
    assert result["embedding_input_tokens"] == 51
    assert result["embedding_cost_microusd"] == 2
    assert result["cost_estimate_complete"]


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("usage_probe")
    store.initialize({"source_kind": "ai"})
    return store


def receipt(store, *, details, input_tokens=1000, output_tokens=100, tier="default"):
    request = {"model": "gpt-6-luna", "reasoning": {"effort": "medium"}, "service_tier": tier}
    store.dispatch("probe", "spine", request)
    store.response(
        "probe",
        {
            "id": "resp_probe",
            "model": "gpt-6-luna",
            "status": "completed",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_tokens_details": details,
            },
        },
    )
    return store.usage()


def test_missing_write_lifetime_has_finite_published_bounds_but_no_exact_cost(usage_store):
    usage = receipt(usage_store, details={"cached_tokens": 100, "cache_write_tokens": 800})
    call = usage["calls"][0]
    # 100 ordinary tokens + 100 cached reads + 800 writes, plus 100 output tokens.
    assert call["cost_estimate_lower_microusd"] == 161
    assert call["cost_estimate_upper_microusd"] == 261
    assert call["cost_microusd"] is None
    assert call["cache_write_30m_tokens"] is None and call["cache_write_12h_tokens"] is None
    assert call["cost_bounds_reason"] == "cache_write_lifetime_unreported"
    assert usage["cost_estimate_complete"] is False
    assert usage["cost_bounds_complete"] is True
    assert usage["cost_estimate_lower_microusd"] == 161
    assert usage["cost_estimate_upper_microusd"] == 261
    assert usage["billed_cost_microusd"] is None


def test_reported_lifetime_split_prices_disjoint_reads_writes_and_ordinary_input(usage_store):
    usage = receipt(
        usage_store,
        details={
            "cached_tokens": 100,
            "cache_write_tokens": 800,
            "cache_write_tokens_details": {"ttl_30m_tokens": 600, "ttl_12h_tokens": 200},
        },
    )
    assert usage["calls"][0]["cost_microusd"] == 186
    assert usage["cost_estimate_complete"] is True
    assert usage["cost_estimate_lower_microusd"] == usage["cost_estimate_upper_microusd"] == 186


def test_long_context_uses_whole_request_input_and_output_multipliers(usage_store):
    usage = receipt(
        usage_store,
        details={"cached_tokens": 0, "cache_write_tokens": 280000},
        input_tokens=280000,
        output_tokens=100,
    )
    assert usage["cost_estimate_lower_microusd"] == 70075
    assert usage["cost_estimate_upper_microusd"] == 140075


@pytest.mark.parametrize(
    "details",
    [
        {"cached_tokens": 100, "cache_write_tokens": 1000},
        {
            "cached_tokens": 0,
            "cache_write_tokens": 800,
            "cache_write_tokens_details": {"ttl_30m_tokens": 700, "ttl_12h_tokens": 0},
        },
        {"cached_tokens": 0, "cache_write_tokens": 800, "cache_write_tokens_details": "unknown"},
    ],
)
def test_invalid_usage_never_gets_a_complete_range(usage_store, details):
    usage = receipt(usage_store, details=details)
    assert usage["cost_estimate_lower_microusd"] is None
    assert usage["cost_estimate_upper_microusd"] is None
    assert usage["cost_bounds_complete"] is False
    assert usage["unbounded_calls"] == 1


def test_unresolved_dispatch_prevents_whole_run_range_and_retains_known_subtotal(usage_store):
    receipt(usage_store, details={"cached_tokens": 0, "cache_write_tokens": 0})
    usage_store.dispatch("ambiguous", "chapters", {"model": "gpt-6-luna"})
    usage = usage_store.usage()
    assert usage["cost_estimate_lower_microusd"] is None
    assert usage["cost_estimate_upper_microusd"] is None
    assert usage["known_cost_estimate_lower_microusd"] == 150
    assert usage["known_cost_estimate_upper_microusd"] == 150
    assert usage["unbounded_calls"] == 1


def test_supported_embedding_cost_is_included_in_whole_run_bounds(usage_store):
    receipt(usage_store, details={"cached_tokens": 0, "cache_write_tokens": 0})
    usage_store.dispatch("embed", "embedding", {"model": "text-embedding-3-small"})
    usage_store.response(
        "embed",
        {
            "id": "embed_probe",
            "model": "text-embedding-3-small",
            "status": "completed",
            "usage": {"prompt_tokens": 1000, "total_tokens": 1000},
        },
    )
    usage = usage_store.usage()
    assert usage["embedding_cost_microusd"] == 20
    assert usage["cost_estimate_lower_microusd"] == usage["cost_estimate_upper_microusd"] == 170
