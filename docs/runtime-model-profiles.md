# Runtime model profiles

Edgar resolves text settings from [`config/model-profiles.toml`](../config/model-profiles.toml)
through `edgar_core.model_profiles`. The file contains no credentials. Keep `OPENAI_API_KEY`
and any image-provider credential in the process environment or the deployment secret store.

Set `MODEL_PROFILES_PATH` to mount an external TOML file; by default Edgar reads
`config/model-profiles.toml` from the repository root. The loader validates the whole file
locally before a provider client is created. Unknown stages, option keys, invalid values, and
unsupported model/option combinations raise `ModelProfileError` with a safe diagnostic. A
model identifier is only syntax-checked; offline validation does not prove account access or
model availability.

## Precedence and lifecycle

For `play.*`, a stage value in the TOML file takes precedence over the compatibility
environment settings `LLM_MODEL` and `LLM_REASONING_EFFORT`. If a stage omits `model`, it
inherits `LLM_MODEL`; if that selected model supports reasoning and the stage omits
`reasoning_effort`, it inherits `LLM_REASONING_EFFORT`. Call-site temperature and output-token
values are fallback defaults only; configured profile values win. Per-stage timeout and retry
settings default to 60 seconds and 3 retries for text and may be overridden in TOML.

The profile file is read when a turn or campaign-generation run starts. That operation keeps
one immutable snapshot through sync graph calls, SSE streaming, and chapter fanout. Durable
workers persist `snapshot.to_dict()` and restore with `ModelProfileSnapshot.from_dict()`.
Historical snapshots and their digests remain unchanged when campaign model policy changes.
`effective_profile()` applies the current code-owned campaign model and effort to each new
semantic call, including resumed runs, while preserving commissioned budgets, timeouts and
retries. Play and image settings remain frozen as saved. The snapshot digest is SHA-256 over
its canonical JSON representation and identifies the historical commission.

Changing process environment values such as API credentials, `LLM_MODEL`,
`LLM_REASONING_EFFORT`, or `MODEL_PROFILES_PATH` requires the normal service restart. The
profile TOML itself is reread per new operation.

## Startup validation

`validate_provider_configuration()` runs wherever a provider can be reached: API startup, before
any background work, and the `ingestion` full-ingest path, `rules_engine`, and
`inspect_collections --search` CLIs. It requires a real `OPENAI_API_KEY` (not blank or the
`.env.example` placeholder), a syntactically valid `EMBEDDING_MODEL`, and a loadable profile set.
Errors name the setting but never print its value. It makes no provider call, so model
availability stays unchecked. Imports and `--help` skip it. At API startup Edgar logs the
effective settings, profile digest, and required credential names once.

## Text stages

The checked-in file names these actual play profiles:

| Profile | Caller |
| --- | --- |
| `play.parser` | Player-input parser node |
| `play.consequence_planner` | Rules and consequence adjudicator |
| `play.narrator` | Ordinary narration, including the SSE narrator |
| `play.combat_builder` | Encounter roster builder |
| `play.combat_narrator` | Narration inside the combat graph |
| `play.memory_summarizer` | Post-turn story summary |
| `play.session_intro` | New-session opening narration |

`play.default` is the factory fallback for callers without an explicit stage. Retrieval,
rules-engine calculations, and other deterministic nodes do not receive fictional model
profiles.

Campaign semantics always use the following policy-bound roles, independent of ordinary play
settings:

- `authoring.outline`, `authoring.chapter`, `authoring.coherence`, and `authoring.manifest`
- `preparation.p01` through `preparation.p12`

Each is enforced as `gpt-6-luna`, `medium`, and the Responses API at every complexity, for
AI manuscripts, PDF studies, design, blueprint, semantic/visual review and corrections.
Attempting to override its
model, effort, or API fails configuration validation. The effective profiles carry distinct
output budgets and may use stage-specific timeouts and retries. This offline policy does not
make provider calls or certify account access.

Retained proposals, corrections and completed/in-flight Responses jobs keep their original
model attribution and accounting. A historical response is recovered only when its request
hash proves the exact same logical task, evidence and schema (allowing the prior model/effort
only). Every fresh submission uses Luna/medium, with no
Sol fallback. An old HTTP dispatch with no saved response ID remains unresolved rather than
being resubmitted under a different model. Rejected historical output remains retained for
diagnostics and narrowly scoped correction under the current policy.

New Luna text accounting uses standard input/output rates of $0.10/$0.50 per million tokens,
$0.01 per million cached input tokens, and the documented long-context multipliers above
272,000 input tokens. Integer micro-USD totals round upwards once per response. Historical
Sol receipts retain their original rates and stored totals. These are accounting estimates,
not spending limits. Image generation and embeddings use their separate model types.

## Input budgets

A text profile may set `max_input_tokens`, an estimated prompt budget (not the provider's hard
limit; leave headroom for output). The checked-in file sets 32000 for `play.parser`,
`play.consequence_planner`, `play.narrator`, `play.combat_narrator` and
`play.memory_summarizer`; a stage without it is unbounded, as before. The effective value is part
of `profile_diagnostics()`. Tokens are estimated as one per three UTF-8 bytes
(`edgar_core.token_budget`), which over-counts English prose, so no tokenizer is needed.

The parser, planner and narrator prompts bound only their continuity context (story summary plus
verbatim exchanges after the summary boundary). Everything else is fixed: system prompt, committed
story facts with their exact quotations and sources, recorded combat outcomes, character and
world state, and retrieved rules and adventure excerpts. Degradation order when the estimate
exceeds the budget:

1. Drop the oldest verbatim messages, newest kept, replaced by a marker line stating how many
   were omitted.
2. Never drop the story summary, committed facts or recorded combat outcomes. If the fixed
   content alone exceeds the budget, the prompt is sent over budget with no verbatim history.

Each trimmed prompt logs `continuity_trimmed` with stage, budget, estimated token counts and
`over_budget`, never text. Omitted verbatim turns are not lost: the post-turn summarizer treats
verbatim history above half of the tightest parser/planner/narrator budget as aged out and
folds it into the summary even before `SUMMARY_EVERY_N_TURNS` have accumulated, and a single
summarizer call takes only as many turns as `play.memory_summarizer` can accept (at least one;
the rest follows on later turns).

## Image stages

Image settings are separate `ImageProfile` records resolved with
`effective_image_profile(stage, snapshot)`. Current names are `image.adventure_map`,
`image.illustration`, and `image.creature_art`; they support model, size, quality, output
format, timeout, and retry settings. They do not accept text reasoning or temperature
settings. A consumer should pass only image-supported options to its image adapter. The
profiles name a desired tool configuration and do not guarantee model access.

## Python API

```python
from edgar_core.model_profiles import (
    ModelProfileSnapshot,
    effective_profile,
    load_model_profiles,
    model_profile_snapshot,
)
from agent.llm import make_chat_model

snapshot = load_model_profiles()
frozen_record = snapshot.to_dict()  # persist with a durable authoring run
restored = ModelProfileSnapshot.from_dict(frozen_record)  # no env/file read
profile = effective_profile("preparation.p03", restored)

with model_profile_snapshot(restored):
    model = make_chat_model(profile="preparation.p03")
```

`profile.to_dict()` and `snapshot.to_dict()` contain non-secret effective settings only.
`profile_diagnostics()` provides the same settings with version and digest plus
`availability_checked: false` for safe local/developer diagnostics.
