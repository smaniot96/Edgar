# Agent

LangGraph state machine that processes one player turn. Imported in-process by the API; not a separate service.

## Flow

```
input_parser -> [combat?] -> combat subgraph (narrates the round itself) -> END
                  |
                  +-> world_retriever -> rules_adjudicator -> world_state_updater -> narrator -> END
```

Any node that sets `error` short-circuits to `END`, so no further LLM calls run and the first error is the one the API sees. `graph.py` compiles two graphs over this topology: `app` (the whole turn, used by the sync endpoint) and `prepare_app` (stops before the linear-path narrator so the SSE runner can stream the narrator itself while parsing the input exactly once).

**The LLM proposes, the engine decides.** `resolution.py` is the shared rules engine:

- `input_parser` proposes the intent (`combat` | `rp` | `exploration`), which skill or ability is tested (`check`), a `difficulty` tier (easy/medium/hard/very_hard → DC 10/15/20/25), the `target`, any item being used, and a `retrieval_query`. Player text is wrapped in `<player_action>` tags and treated as untrusted. If structured output fails, the turn falls back to exploration with no check.
- The engine computes the modifier from the character sheet, rolls with `tools.dice`, and decides success. Attacks roll d20 + attack bonus against the target's AC.
- `rules_adjudicator` is told the outcome and only describes the consequences. It sees the world flags, the current scene and the rolling memory summary. It retries once and then falls back to a neutral outcome. The engine then enforces its proposed side effects: damage only after a failed check (engine-rolled and capped), healing only from a consumable actually in inventory or a rest, official 5e conditions only, capped and cleaned item grants, snake_case flags, and validated scene ids.
- Combat rolls d20 + DEX initiative for everyone and resolves one full round per HTTP turn in that order. Only engine dice change HP. Friendly NPCs are never added to the enemy roster, and stat blocks come from `db.vector.retrieve_monster`.
- Campaign completion: an LLM proposal alone only sets a pending flag. The campaign ends after a victory over a boss, or when the player confirms on a later turn (`decide_completion`).

`world_retriever` queries the rules and adventure collections, using the parser's `retrieval_query` when there is one. Results come back ranked globally by score, deduplicated and capped (see `db/vector/README.md`). If retrieval fails, the turn continues without context and sets `retrieval_degraded`.

`world_state_updater` collects telemetry into `state["telemetry_events"]`. The API writes it in the same transaction as everything else (`apps/api/services/turn_runner.persist_turn`).

`narrator` builds its prompt with `build_narrator_prompt` (shared with the SSE path), sees the memory summary and known NPCs, is capped by `NARRATOR_MAX_TOKENS`, and is told not to print HP or status blocks, because the UI renders state.

The rolling memory summary is not a graph node. After a turn commits, the API folds older turns into a stored `memory_summary` event (`apps/api/services/session_messages.py`) and loads the latest one into `state["memory_summary"]` on the next turn.

## State

`AgentState` (TypedDict, all keys optional) is the contract every node honours. The API populates `player_input`, `session_id`, `campaign_id`, `messages` (rebuilt from `event_log`), `memory_summary`, `adventure_collections`, `world_flags`, `current_scene_id`, `character`, `npcs`, and an existing `combat_state` when one is loaded from the DB. Nodes only write the keys they own.

## Running

The agent is invoked from `apps/api/services/turn_runner.py` (`run_turn` for the sync endpoint, `stream_session_turn` for SSE).

## Tests

`tests/agent/` covers the parser, retriever, adjudicator, the rules engine (`test_resolution.py`) and combat (`test_combat.py`), using the `fake_llm_schema` fixture (a fake LLM keyed by output schema) and seeded dice. The full turn flow, including streaming, locking and persistence, is in `tests/api/test_turn.py` and `tests/api/test_turn_pipeline.py`.
