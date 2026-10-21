# Deploy the campaign preparation replacement from committed dev

The existing local runtime remains Compose project `edgar-main` at port 8000. Its
campaign data bind is `/Users/psmaniotto/.claude-worktrees/edgar-b/edgar-main-run/data`;
PostgreSQL and Qdrant retain `edgar-main_postgres_data` and
`edgar-main_qdrant_data`. Redis has no persistent mount, so this rollout preserves
the existing Redis container and its memory. The original untracked
`docker-compose.local-main.yml` and original runtime `.env` remain in use.

`tools/dev_rollout.py` builds from a temporary `git archive` of the committed `dev`
revision. It rejects uncommitted task changes; the two preexisting user deletions
are allowed and never staged or restored. All four owned targets (`api`,
`ingestion`, `migrate`, `rules_engine`) receive the same revision/branch image
labels and `/app/edgar-build.json`. A source hash manifest is copied into every
image. The API image includes the production frontend and versioned campaign
prompt files.

After committing and pushing the implementation with `[skip ci]`, run from the
primary repository:

```sh
.venv/bin/python tools/dev_rollout.py build \
  --output-dir /Users/psmaniotto/EdgarBackups/campaign-rework-dev-images

.venv/bin/python tools/dev_rollout.py deploy \
  --output-dir /Users/psmaniotto/EdgarBackups/campaign-rework-dev-images \
  --backup /Users/psmaniotto/EdgarBackups/campaign-rework-20261021

.venv/bin/python tools/dev_rollout.py verify \
  --output-dir /Users/psmaniotto/EdgarBackups/campaign-rework-dev-images
```

Choose a new output directory for a subsequent commit; existing image receipts
are never overwritten. The build command does not run the on-demand job images.
The deploy command verifies backup artifact checksums and all original campaign
file hashes, checks existing infrastructure mounts/container identities, writes
an explicit absolute-mount override outside Git, then executes the equivalent of:

```sh
docker compose --project-name edgar-main \
  --project-directory /Users/psmaniotto/.claude-worktrees/edgar-b/edgar-main-run \
  --env-file /Users/psmaniotto/.claude-worktrees/edgar-b/edgar-main-run/.env \
  -f /Users/psmaniotto/Desktop/MyProjects/Edgar/docker-compose.yml \
  -f /Users/psmaniotto/.claude-worktrees/edgar-b/edgar-main-run/docker-compose.local-main.yml \
  -f /Users/psmaniotto/EdgarBackups/campaign-rework-dev-images/docker-compose.dev-rollout.json \
  up --detach --no-deps --no-build --wait api
```

The generated override pins the four built image tags, disables pulling, maps
campaign/index/job binds to the original absolute directories, and declares the
original database/vector volumes external by exact name. Compose therefore
cannot silently create empty replacement volumes if one is missing. `--no-deps`
leaves the database, Redis and Qdrant containers untouched; `--no-build` uses the
already recorded image IDs. No migrations or ingestion jobs are invoked.

The deployment receipt records preserved file count and backup manifest hash,
unchanged infrastructure container IDs/mounts and the API image ID. Verification
checks the running API image ID, exact bind mounts, in-container revision and
SHA-256 of executable/source files against the committed archive. A repeated
`verify` is read-only except for its local receipt. Image labels alone do not
establish this evidence.

The predeployment backup at
`/Users/psmaniotto/EdgarBackups/campaign-rework-20261021` was taken with the API
stopped and no other application writers. Its private `RESTORE.md` describes
campaign, PostgreSQL, Qdrant and Redis recovery. Keep backup data and environment
values outside Git. Avoid printing expanded Compose configuration: the helper
never serializes service environments or provider credentials.

If a rollout must be reversed, stop the API writer and use the recorded original
API image ID with the same existing resources. Do not tear down the stack or
remove volumes. Any data restore requires the writer coordination and controlled
restore procedure recorded alongside the backup; an application-image rollback
alone does not authorize overwriting data created after the backup.

## Usage and pricing evidence

Official OpenAI documentation fetched on 21 October 2026 establishes the pricing
snapshot used by the new preparation usage report:

- [GPT-6 Luna model and limits](https://developers.openai.com/api/docs/models/gpt-6-luna):
  1,050,000-token context, 922,000 maximum input, 128,000 maximum output;
  Standard USD per million tokens: ordinary input `0.10`, cached input `0.01`,
  30-minute cache writes `0.125`, output `0.50`.
- [API pricing](https://developers.openai.com/api/docs/pricing): 12-hour Luna
  cache writes `0.25` per million tokens. Above 272,000 input tokens, the whole
  request uses twice the input/cache rates and 1.5 times the output rate.
- [Prompt cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching#monitor-cache-performance):
  `cached_tokens` are billed reads; `cache_write_tokens_details.ttl_30m_tokens`
  and `ttl_12h_tokens` are disjoint write counts that sum to `cache_write_tokens`.
  Subtract both reads and writes from total input before pricing ordinary input.
  Write rates replace ordinary input rates for those tokens.
- [text-embedding-3-small](https://developers.openai.com/api/docs/models/text-embedding-3-small):
  Standard USD `0.02` per million input tokens.

Tokens come from retained provider receipts. Costs are estimates from these
published rates, rounded upward to whole microUSD per call; they are never
represented as verified billed charges. Semantic calls and embeddings have
separate counts/token totals, with both included in known estimated cost.
Pending or ambiguous dispatches and unpriced models/tiers retain null per-call
costs. Aggregate `cost_microusd` is the backward-compatible sum of known estimates;
`known_cost_microusd`, `unknown_calls` and `cost_estimate_complete` make incomplete
accounting explicit. `billed_cost_microusd` remains null. Regional premiums,
account discounts, taxes and invoices have not been independently verified.
