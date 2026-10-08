# Ingestion

PDF ingestion pipeline for RAG: extract text with pymupdf4llm, chunk, embed (OpenAI), and upsert to Qdrant.

## Chunking

`chunk.py` is heading-aware: markdown headings from pymupdf4llm open new sections, "Chapter / Part /
Appendix" headings become the `chapter`, and every chunk's text is prefixed with its heading path
(`Chapter 2: The Temple > Area 3. Shrine`). Monster/NPC stat blocks are detected (size/type/alignment
line, `Armor Class` + `Hit Points`) and kept in one chunk together with their ACTIONS / REACTIONS /
LEGENDARY ACTIONS sub-headings (up to `4 × --chunk-size`). Long sections are split at paragraph
boundaries under `--chunk-size` with `--overlap` characters of carry-over.

Payload per point: `text, page, page_end, source, chunk_index, section, chapter, heading_path,
stat_block`. Collections ingested before this change only have `text, page, source`; retrieval
handles both. New collections get a `page` payload index (used by the session intro).

## Canonical recipes

1. **Rules (core books)** — use `--collection rules_player_handbook` (or `rules_dm_guide`, `rules_monster_manual`) so names match `db.vector.collections` and shared RAG in the agent and `rules_engine`.

2. **Adventure module** — use `--collection chalice_of_the_mountain_god` (or another module slug). Ensure `campaigns.adventure_collections` in Postgres lists that collection (or leave it empty to use the default seeded adventure).

3. **Campaign-bound DM notes** — use `--campaign-id <id>` to ingest into `campaign_lore_<id>`. The retriever automatically searches that collection in the adventure bucket whenever agent state includes that `campaign_id`; no extra DB config.

If both `--campaign-id` and `--collection` are passed, `--campaign-id` wins (a warning is logged).

## Collection names

- **Rules:** canonical names above; example:

```bash
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --collection rules_player_handbook
```

- **Legacy:** If you already ingested into `player_handbook`, `dm_guide`, `monster_manual`, the retriever still searches those collections until you re-ingest into `rules_*` names.

- **Adventure modules:** collection slug must match `campaigns.adventure_collections` when set.

- **Campaign lore:** `--campaign-id 1` → collection `campaign_lore_1`.

## Usage

### Local (from Edgar root)

Paths are relative to Edgar; PDFs live in `Edgar/data/pdfs/` (the directory is gitignored):

```bash
# Full ingestion (extract, chunk, embed, upsert) + save markdown to Edgar/data/markdown
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --collection rules_player_handbook

# Campaign-bound notes → campaign_lore_<id> (matches plan / verification recipe)
uv run --project apps/ingestion ingest data/pdfs/my_dm_notes.pdf --campaign-id 1

# Extract markdown only (no embedding)
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --extract-only

# Inspect Qdrant collections
uv run --project apps/ingestion ingest-inspect --sample
uv run --project apps/ingestion ingest-inspect --search "ability check"
```

### Docker

PDFs live in `Edgar/data/pdfs/` (bind-mounted into the container at `/data`). Markdown is written to `Edgar/data/markdown/` via `/output/markdown`. From Edgar:

```bash
# Full ingestion (map collection to rules_player_handbook)
docker compose run --rm ingestion /data/player_handbook.pdf -c rules_player_handbook

# Campaign-specific notes → campaign_lore_1 (any session under campaign 1 retrieves these)
docker compose run --rm ingestion /data/my_dm_notes.pdf --campaign-id 1

# Extract markdown only
docker compose run --rm ingestion /data/player_handbook.pdf --extract-only
```

Options: `--collection` / `-c`, `--campaign-id`, `--chunk-size`, `--overlap`, `--extract-only`, `--markdown-dir`, `--no-save-markdown`, `--replace`.

## Re-ingesting after the heading-aware chunker change

Point ids hash the chunk boundaries, so a plain re-ingest would leave the old page-cut chunks next to
the new ones. Use `--replace`: after **all** new chunks are embedded and upserted, points of the same
`source` (PDF stem) that this run did not write are deleted. A failed run deletes nothing, and other
sources sharing the collection are untouched. Re-embedding costs OpenAI tokens (a core book is a few
thousand chunks).

```bash
# Local, from Edgar/
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf  -c rules_player_handbook --replace
uv run --project apps/ingestion ingest data/pdfs/dm_guide.pdf         -c rules_dm_guide        --replace
uv run --project apps/ingestion ingest data/pdfs/monster_manual.pdf   -c rules_monster_manual  --replace
uv run --project apps/ingestion ingest data/pdfs/chalice_of_the_mountain_god.pdf -c chalice_of_the_mountain_god --replace
uv run --project apps/ingestion ingest data/pdfs/d_d_5e_lost_mine_of_phandelver.pdf -c d_d_5e_lost_mine_of_phandelver --replace

# Docker
docker compose run --rm ingestion /data/monster_manual.pdf -c rules_monster_manual --replace
```

Check the result with `uv run --project apps/ingestion ingest-inspect --sample` (samples now show
`heading_path`).

## Migrating Qdrant data from legacy names

Either **re-ingest** each PDF with `--collection rules_*`, or use a one-off script with the Qdrant client to copy points from e.g. `player_handbook` → `rules_player_handbook` (same vectors/payloads).

## Structure

```
apps/ingestion/
├── src/ingestion/
│   ├── main.py    # CLI entrypoint
│   ├── extract.py # PDF → per-page text (pymupdf4llm)
│   ├── chunk.py  # Heading-aware chunking (stat blocks kept whole)
│   └── embed.py  # Embed + upsert to Qdrant
├── tests/
└── pyproject.toml
```
