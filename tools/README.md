# tools

Deterministic dice tool used by the agent's `rules_adjudicator` and the combat subgraph.

`roll("2d6+3")` returns a `DiceOutcome(expression, total, rolls, modifier)`. The LLM never picks the number; it requests an expression and gets back the authoritative result.

`roll(expression, rng=...)` accepts an injected `random.Random` for tests and replay. Default RNG is `random.SystemRandom()`. Limits: 1 to 100 dice, 2 to 100 sides per die.
