# Agent

LangGraph state machine that processes one player turn. Imported in-process by the API; not a separate service.

## Flow

```
input_parser -> [combat?] -> combat subgraph -> memory_summarizer -> END
                  |
                  +-> world_retriever -> rules_adjudicator -> world_state_updater -> narrator -> memory_summarizer -> END
```

`input_parser` classifies intent (`combat`, `rp`, `exploration`) and may emit a `dice_expression`. The conditional edge after it routes combat into the subgraph (initiative + per-turn loop) and everything else through the linear path.

`world_retriever` runs two Qdrant searches concurrently (`asyncio.gather`): rules collections (PHB, DMG, MM, plus legacy aliases) and adventure collections (the campaign's `adventure_collections` plus `campaign_lore_{id}` if a campaign id is in state). Results live under `rules_context` and `adventure_context`; the legacy `retrieved_context` key is the merged list.

`rules_adjudicator` builds a structured `AdjudicationResult` from rules excerpts and the optional dice roll. The LLM does not pick the dice number; the `tools.dice.roll(...)` outcome is injected before the call and copied onto the result.

`world_state_updater` writes a telemetry row to `event_log` (`event_type="adjudication"`). Source-of-truth writes (HP, world flags, scene changes, inventory) are applied by `apps/api/services/world_writes.apply_adjudication` inside the API's transaction, not here.

`narrator` consumes the prompt built by `build_narrator_prompt` (also used by the SSE streaming path) and produces the player-facing text.

`memory_summarizer` collapses old turns once `messages` exceeds 10 entries.

## State

`AgentState` (TypedDict, all keys optional) is the contract every node honours. The API populates `player_input`, `session_id`, `campaign_id`, `messages` (rebuilt from `event_log`), `adventure_collections`, `world_flags`, `current_scene_id`, `character`, and an existing `combat_state` when one is loaded from the DB. Nodes only write the keys they own.

## Running

The agent is invoked from `apps/api/routers/session.py` via `await app.ainvoke(initial_state)`. There is also a `apps/agent/main.py` for ad-hoc graph runs from the shell. From Edgar:

```
uv run python -m apps.agent.main
```

## Tests

`tests/agent/` covers `input_parser` and `world_retriever` with the LLM and Qdrant stubbed via `monkeypatch`. The full turn flow is exercised in `tests/api/test_turn.py`.
