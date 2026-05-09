def test_world_retriever_node_is_callable() -> None:
    from agent.nodes.world_retriever import world_retriever_node

    assert callable(world_retriever_node)
