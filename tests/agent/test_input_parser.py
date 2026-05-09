import pytest
from langchain_core.messages import AIMessage

from agent.models.parsed_input import ParsedInput


@pytest.mark.asyncio
async def test_input_parser_returns_parsed(monkeypatch) -> None:
    from agent.nodes import input_parser as ip

    class _S:
        def __init__(self, v) -> None:
            self._v = v

        async def ainvoke(self, *_a, **_kw):
            return self._v

    class _F:
        def with_structured_output(self, schema, **_kw):
            assert schema is ParsedInput
            return _S(ParsedInput(intent="rp", entities={}, dice_expression=None))

        async def ainvoke(self, *_a, **_kw) -> AIMessage:
            return AIMessage(content="x")

    def _factory(temperature: float = 0) -> _F:  # noqa: ARG001
        return _F()

    monkeypatch.setattr(ip, "make_chat_model", _factory)
    out = await ip.input_parser_node({"player_input": "I greet the guard."})
    assert out["parsed_input"].intent == "rp"


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
