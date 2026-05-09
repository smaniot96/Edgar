# Ingestion

PDF ingestion pipeline for RAG: extract text with pymupdf4llm, chunk, embed (OpenAI), and upsert to Qdrant.

## Canonical recipes

1. **Rules (core books)** — use `--collection rules_player_handbook` (or `rules_dm_guide`, `rules_monster_manual`) so names match `db.vector.collections` and shared RAG in the agent and `rules_engine`.

2. **Adventure module** — use `--collection chalice_of_the_mountain_god` (or another module slug). Ensure `campaigns.adventure_collections` in Postgres lists that collection (or leave it empty to use the default seeded adventure).

3. **Campaign-bound DM notes** — use `--campaign-id <id>` to ingest into `campaign_lore_<id>`. The retriever automatically searches that collection in the adventure bucket whenever agent state includes that `campaign_id`; no extra DB config.

If both `--campaign-id` and `--collection` are passed, `--campaign-id` wins (a warning is logged).

## Collection names

- **Rules:** canonical names above; example:

```bash
uv run --project apps/ingestion ingest ../data/pdfs/player_handbook.pdf --collection rules_player_handbook
```

- **Legacy:** If you already ingested into `player_handbook`, `dm_guide`, `monster_manual`, the retriever still searches those collections until you re-ingest into `rules_*` names.

- **Adventure modules:** collection slug must match `campaigns.adventure_collections` when set.

- **Campaign lore:** `--campaign-id 1` → collection `campaign_lore_1`.

## Usage

### Local (from Edgar root)

Paths are relative to Edgar; PDFs live in `dnd/data/pdfs/`:

```bash
# Full ingestion (extract, chunk, embed, upsert) + save markdown to data/markdown
uv run --project apps/ingestion ingest ../data/pdfs/player_handbook.pdf --collection rules_player_handbook

# Campaign-bound notes → campaign_lore_<id> (matches plan / verification recipe)
uv run --project apps/ingestion ingest ../data/pdfs/my_dm_notes.pdf --campaign-id 1

# Extract markdown only (no embedding)
uv run --project apps/ingestion ingest ../data/pdfs/player_handbook.pdf --extract-only

# Inspect Qdrant collections
uv run --project apps/ingestion ingest-inspect --sample
uv run --project apps/ingestion ingest-inspect --search "ability check"
```

### Docker

PDFs live in `dnd/data/pdfs/`. Markdown is written to `dnd/data/markdown/`. From Edgar:

```bash
# Full ingestion (map collection to rules_player_handbook)
docker compose run --rm ingestion /data/player_handbook.pdf -c rules_player_handbook

# Campaign-specific notes → campaign_lore_1 (any session under campaign 1 retrieves these)
docker compose run --rm ingestion /data/my_dm_notes.pdf --campaign-id 1

# Extract markdown only
docker compose run --rm ingestion /data/player_handbook.pdf --extract-only
```

Options: `--collection` / `-c`, `--campaign-id`, `--chunk-size`, `--overlap`, `--extract-only`, `--markdown-dir`, `--no-save-markdown`.

## Migrating Qdrant data from legacy names

Either **re-ingest** each PDF with `--collection rules_*`, or use a one-off script with the Qdrant client to copy points from e.g. `player_handbook` → `rules_player_handbook` (same vectors/payloads).

## Structure

```
apps/ingestion/
├── src/ingestion/
│   ├── main.py    # CLI entrypoint
│   ├── extract.py # PDF → per-page text (pymupdf4llm)
│   ├── chunk.py  # Sliding window chunking
│   └── embed.py  # Embed + upsert to Qdrant
├── tests/
└── pyproject.toml
```
