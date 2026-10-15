import pytest


@pytest.fixture(autouse=True)
def available_corpus(monkeypatch):
    monkeypatch.setattr(
        "agent.nodes.world_retriever.inspect_source",
        lambda slug, **_kw: {"slug": slug, "status": "ready", "usable_chunks": 1},
    )


@pytest.mark.asyncio
async def test_world_retriever_accepts_fake_client_and_does_not_certify_legacy_context(
    monkeypatch,
) -> None:
    from agent.nodes import world_retriever as wr

    def _rules(q, **_kw):
        assert "hello" in q
        return [{"text": "r", "source": "PHB", "kind": "rules"}]

    monkeypatch.setattr(wr, "search_rules_context", _rules)

    fake_client = object()
    out = await wr.world_retriever_node(
        {
            "player_input": "hello",
            "parsed_input": None,
            "campaign_id": 1,
            "adventure_collections": [],
            "qdrant_client": fake_client,
        }
    )
    assert len(out["rules_context"]) == 1
    assert out["rules_context"][0]["kind"] == "rules"
    assert out["adventure_context"] == []
    assert out["manifest_status"] == "review_required"


@pytest.mark.asyncio
async def test_world_retriever_returns_empty_without_input() -> None:
    from agent.nodes import world_retriever as wr

    out = await wr.world_retriever_node({"player_input": "", "parsed_input": None})
    assert out == {
        "rules_context": [],
        "adventure_context": [],
        "retrieved_context": [],
    }


@pytest.mark.asyncio
async def test_world_retriever_uses_retrieval_query_for_rules_only(monkeypatch) -> None:
    from agent.models.parsed_input import ParsedInput
    from agent.nodes import world_retriever as wr

    seen: list[str] = []

    def _rules(q, **_kw):
        seen.append(q)
        return []

    monkeypatch.setattr(wr, "search_rules_context", _rules)
    parsed = ParsedInput(intent="combat", retrieval_query="goblin ambush trail grapple rules")
    await wr.world_retriever_node({"player_input": "I grab him", "parsed_input": parsed})
    # Adventure scope comes from canonical scene state, never a broad semantic query.
    assert seen == ["goblin ambush trail grapple rules"]


@pytest.mark.asyncio
async def test_world_retriever_degrades_instead_of_failing(monkeypatch) -> None:
    from agent.nodes import world_retriever as wr

    def _boom(*_a, **_kw):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(wr, "search_rules_context", _boom)
    out = await wr.world_retriever_node({"player_input": "hello", "parsed_input": None})
    assert "error" not in out
    assert out["retrieval_degraded"] is True
    assert out["rules_context"] == [] and out["adventure_context"] == []


@pytest.mark.parametrize("status", ["missing", "empty", "partial", "unavailable"])
async def test_bound_unusable_source_marks_degraded_with_rule_hits(monkeypatch, status):
    from agent.nodes import world_retriever as wr

    monkeypatch.setattr(
        wr,
        "inspect_source",
        lambda slug, **kw: {"slug": slug, "status": status, "usable_chunks": 0},
    )
    monkeypatch.setattr(wr, "search_rules_context", lambda *a, **kw: [{"text": "Rule evidence"}])
    result = await wr.world_retriever_node(
        {"player_input": "Explore", "adventure_collections": ["lost"]}
    )
    assert result["rules_context"] and result["adventure_context"] == []
    assert result["retrieval_degraded"]
    assert result["source_status"][0]["status"] == status


async def test_module_source_loss_and_retrieval_failure_block_narration(monkeypatch):
    from agent.nodes import world_retriever as wr

    monkeypatch.setattr(wr, "search_rules_context", lambda *a, **kw: [{"text": "Rule evidence"}])

    def failed_query(*a, **kw):
        raise RuntimeError("temporary adventure query failure")

    monkeypatch.setattr(wr, "retrieve_active_scene", failed_query)
    result = await wr.world_retriever_node(
        {
            "player_input": "Explore",
            "source_mode": "module",
            "adventure_collections": ["module"],
            "adventure_manifests": {"module": {}},
        }
    )
    assert result["error_code"] == "campaign_sources_unavailable"
    assert result["retrieval_degraded"] and result["source_status"][0]["status"] == "unavailable"


async def test_explicit_homebrew_has_no_default_module_probe(monkeypatch):
    from agent.nodes import world_retriever as wr

    def no_probe(*a, **kw):
        raise AssertionError("homebrew must not inspect a default module")

    monkeypatch.setattr(wr, "inspect_source", no_probe)
    monkeypatch.setattr(wr, "search_rules_context", lambda *a, **kw: [])

    result = await wr.world_retriever_node(
        {
            "player_input": "Explore",
            "source_mode": "homebrew",
            "adventure_collections": [],
            "retrieval_degraded": True,
            "source_status": [{"slug": "lost", "status": "missing"}],
        }
    )
    assert result["retrieval_degraded"] and "error" not in result
    assert result["source_status"][0]["slug"] == "lost"


async def test_combat_propagates_source_error_without_narrating(monkeypatch):
    from agent import graph_combat

    async def unavailable(state):
        return {
            "error": "Missing module",
            "error_code": "campaign_sources_unavailable",
            "retrieval_degraded": True,
            "source_status": [{"slug": "lost", "status": "missing"}],
        }

    monkeypatch.setattr(graph_combat, "world_retriever_node", unavailable)
    result = await graph_combat.combat_round_node(
        {"player_input": "Attack", "source_mode": "module"}
    )
    assert result["error_code"] == "campaign_sources_unavailable"
    assert result["retrieval_degraded"]
    assert "narration" not in result and "combat_state" not in result


async def test_reviewed_public_private_context_and_state_stay_separate(monkeypatch):
    from agent.nodes import world_retriever as wr

    monkeypatch.setattr(wr, "search_rules_context", lambda *args, **kwargs: [])

    def active(manifests, scene, flags, **kwargs):
        assert manifests == {"module": {}} and scene == "village"
        assert flags == {"map_found": "discovered"} and kwargs["rules_edition"] == "2024"
        return {
            "adventure_context": [{"text": "Safe village", "audience": "player"}],
            "planner_context": [{"text": "Hidden guardian", "audience": "planner"}],
            "allowed_scene_ids": ["village", "forest"],
            "discovery_entities": [],
        }

    monkeypatch.setattr(wr, "retrieve_active_scene", active)
    out = await wr.world_retriever_node(
        {
            "player_input": "Explore",
            "source_mode": "module",
            "adventure_collections": ["module"],
            "adventure_manifests": {"module": {}},
            "current_scene_id": "village",
            "world_flags": {"map_found": "discovered"},
            "rules_edition": "2024",
        }
    )
    assert out["manifest_status"] == "reviewed"
    assert out["planner_context"][0]["text"] == "Hidden guardian"
    assert "Hidden guardian" not in str(out["adventure_context"] + out["retrieved_context"])


async def test_module_manifest_error_is_distinct_from_vector_unavailability(monkeypatch):
    from agent.nodes import world_retriever as wr

    monkeypatch.setattr(wr, "search_rules_context", lambda *args, **kwargs: [])
    out = await wr.world_retriever_node(
        {
            "player_input": "Explore",
            "source_mode": "module",
            "adventure_collections": ["module"],
        }
    )
    assert out["error_code"] == "adventure_manifest_review_required"
    assert out["source_status"][0]["status"] == "ready"
    assert not out["adventure_context"] and not out["planner_context"]
