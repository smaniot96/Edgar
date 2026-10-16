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
one immutable snapshot through sync graph calls, SSE streaming, and chapter fanout. A file
edit affects the next operation; it does not change a running one or rewrite an existing
campaign. Durable authoring workers should persist `snapshot.to_dict()` and restore with
`ModelProfileSnapshot.from_dict()` so a restart uses the commissioned settings rather than a
new file read. The snapshot digest is SHA-256 over its canonical JSON representation.

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

Each is enforced as `gpt-6.1-sol`, `high`, and the Responses API. Attempting to override its
model, effort, or API fails configuration validation. The effective profiles carry distinct
output budgets and may use stage-specific timeouts and retries. This offline policy does not
make provider calls or certify account access.

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
