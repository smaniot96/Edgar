# Rules engine

A small CLI that demonstrates rules-only RAG using the same `db.vector` retrieval helpers the agent uses. It searches the `rules_*` collections (PHB, DMG, MM) and the legacy aliases.

```
uv run --project apps/rules_engine python -m rules_engine.main "ability check proficiency"
```

The agent's `rules_adjudicator` calls `search_rules_context` directly; this CLI exists so you can poke the rules bucket without spinning up the agent.
