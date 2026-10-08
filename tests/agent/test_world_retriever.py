import pytest


@pytest.mark.asyncio
async def test_world_retriever_accepts_fake_client(monkeypatch) -> None:
    from agent.nodes import world_retriever as wr

    def _rules(q, **_kw):
        assert "hello" in q
        return [{"text": "r", "source": "PHB", "kind": "rules"}]

    def _adv(q, cols, **_kw):
        return [{"text": "a", "source": "mod", "kind": "adventure"}]

    monkeypatch.setattr(wr, "search_rules_context", _rules)
    monkeypatch.setattr(wr, "search_adventure_context", _adv)

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
    assert len(out["adventure_context"]) == 1
    assert out["adventure_context"][0]["kind"] == "adventure"


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
async def test_world_retriever_uses_retrieval_query_for_both_buckets(monkeypatch) -> None:
    from agent.models.parsed_input import ParsedInput
    from agent.nodes import world_retriever as wr

    seen: list[str] = []

    def _rules(q, **_kw):
        seen.append(q)
        return []

    def _adv(q, cols, **_kw):
        seen.append(q)
        return []

    monkeypatch.setattr(wr, "search_rules_context", _rules)
    monkeypatch.setattr(wr, "search_adventure_context", _adv)
    parsed = ParsedInput(intent="combat", retrieval_query="goblin ambush trail grapple rules")
    await wr.world_retriever_node({"player_input": "I grab him", "parsed_input": parsed})
    # Identical string for both buckets -> embedded once by the memoised embed_query.
    assert seen == ["goblin ambush trail grapple rules"] * 2


@pytest.mark.asyncio
async def test_world_retriever_degrades_instead_of_failing(monkeypatch) -> None:
    from agent.nodes import world_retriever as wr

    def _boom(*_a, **_kw):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(wr, "search_rules_context", _boom)
    monkeypatch.setattr(wr, "search_adventure_context", lambda *_a, **_kw: [{"text": "a", "kind": "adventure"}])
    out = await wr.world_retriever_node({"player_input": "hello", "parsed_input": None})
    assert "error" not in out
    assert out["retrieval_degraded"] is True
    assert out["rules_context"] == [] and len(out["adventure_context"]) == 1
