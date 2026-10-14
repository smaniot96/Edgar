# Ingestion

PDF ingestion pipeline for RAG: select native geometry or OCR per page, validate and
preview the exact extracted text, chunk, embed (OpenAI), and upsert to Qdrant.

## Chunking

`chunk.py` is heading-aware: extracted markdown headings open new sections, "Chapter / Part /
Appendix" headings become the `chapter`, and every chunk's text is prefixed with its heading path
(`Chapter 2: The Temple > Area 3. Shrine`). Monster/NPC stat blocks are detected (size/type/alignment
line, `Armor Class` + `Hit Points`) and kept in one chunk together with their ACTIONS / REACTIONS /
LEGENDARY ACTIONS sub-headings. Complete stat records are indivisible by default;
programmatic callers can explicitly set `stat_block_max` when splitting is intended. Long sections are split at paragraph
boundaries under `--chunk-size` with `--overlap` characters of carry-over.

Payload per point: `text, page, page_end, source, chunk_index, section, chapter, heading_path,
stat_block, source_pages, source_sha256, extraction_sha256`. `source_pages` records actual
contributing text, headings and overlap rather than inferring coverage from page ranges.
Collections ingested before this change only have `text, page, source`; retrieval
handles both. New collections get a `page` payload index (used by the session intro).

## Canonical recipes

1. **Rules (core books)** — use `--collection rules_player_handbook` (or `rules_dm_guide`, `rules_monster_manual`) so names match `db.vector.collections` and shared RAG in the agent and `rules_engine`.

2. **Adventure module** — use `--collection chalice_of_the_mountain_god` (or another module slug). Bind that collection in the campaign source settings. Module play requires usable bound sources; persisted explicit homebrew is the generic fallback.

3. **Campaign-bound DM notes** — use `--campaign-id <id>` to ingest into `campaign_lore_<id>`. The retriever automatically searches that collection in the adventure bucket whenever agent state includes that `campaign_id`; no extra DB config.

If both `--campaign-id` and `--collection` are passed, `--campaign-id` wins (a warning is logged).

## Collection names

- **Rules:** canonical names above; example:

```bash
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --collection rules_player_handbook --reviewed-extraction data/markdown/player_handbook.extraction.json
```

- **Legacy:** If you already ingested into `player_handbook`, `dm_guide`, `monster_manual`, the retriever still searches those collections until you re-ingest into `rules_*` names.

- **Adventure modules:** collection slug must match `campaigns.adventure_collections` when set.

- **Campaign lore:** `--campaign-id 1` → collection `campaign_lore_1`.

## Usage

### Local (from Edgar root)

Paths are relative to Edgar; PDFs live in `Edgar/data/pdfs/` (the directory is gitignored):

```bash
# First extract the complete book; inspect the Markdown and diagnostic JSON against the PDF.
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --extract-only

# Index the exact inspected extraction; no second extraction occurs.
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --collection rules_player_handbook --reviewed-extraction data/markdown/player_handbook.extraction.json

# Campaign-bound notes → campaign_lore_<id> (matches plan / verification recipe)
uv run --project apps/ingestion ingest data/pdfs/my_dm_notes.pdf --campaign-id 1

# Sample-only QA goes to a separate directory; it cannot authorize full indexing.
uv run --project apps/ingestion ingest data/pdfs/player_handbook.pdf --extract-only --pages 73 258 --markdown-dir data/extraction-qa/phb

# Inspect Qdrant collections
uv run --project apps/ingestion ingest-inspect --sample
uv run --project apps/ingestion ingest-inspect --search "ability check"
```

### Docker

PDFs live in `Edgar/data/pdfs/` (bind-mounted into the container at `/data`). Markdown is written to `Edgar/data/markdown/` via `/output/markdown`. From Edgar:

```bash
# Extract the complete book, then inspect data/markdown/player_handbook.md and its JSON.
docker compose run --rm ingestion /data/player_handbook.pdf --extract-only

# Index the exact inspected extraction.
docker compose run --rm ingestion /data/player_handbook.pdf -c rules_player_handbook --reviewed-extraction /output/markdown/player_handbook.extraction.json

# Campaign-specific notes → campaign_lore_1 (any session under campaign 1 retrieves these)
docker compose run --rm ingestion /data/my_dm_notes.pdf --campaign-id 1

# Sample-only QA uses a separate output directory.
docker compose run --rm ingestion /data/player_handbook.pdf --extract-only --pages 73 258 --markdown-dir /output/markdown/phb-samples
```

Options: `--collection` / `-c`, `--campaign-id`, `--chunk-size`, `--overlap`, `--extract-only`, `--markdown-dir`, `--no-save-markdown`, `--replace`, `--pages`, `--reviewed-extraction`.

## Re-ingesting after the heading-aware chunker change

Point ids hash the chunk boundaries, so a plain re-ingest would leave the old page-cut chunks next to
the new ones. Use `--replace`: after **all** new chunks are embedded and upserted, points of the same
`source` (PDF stem) that this run did not write are deleted. An embedding/upsert failure skips stale deletion, and other
sources sharing the collection are untouched. Re-embedding costs OpenAI tokens (a core book is a few
thousand chunks).

Extract and inspect a complete bundle first. A changed PDF, changed preview, sampled subset or
failed structural checks cannot be indexed as a reviewed source. For example:

```bash
uv run --project apps/ingestion ingest data/pdfs/monster_manual.pdf --extract-only
# Compare data/markdown/monster_manual.md and monster_manual.extraction.json to the source.
uv run --project apps/ingestion ingest data/pdfs/monster_manual.pdf -c rules_monster_manual --replace --reviewed-extraction data/markdown/monster_manual.extraction.json

# Docker paths refer to the same bind-mounted preview.
docker compose run --rm ingestion /data/monster_manual.pdf -c rules_monster_manual --replace --reviewed-extraction /output/markdown/monster_manual.extraction.json
```

Check the result with `uv run --project apps/ingestion ingest-inspect --sample` (samples now show
`heading_path`).

## Extraction quality and readiness

One canonical `source.md` / `source.extraction.json` bundle contains selected text, physical
page numbers, source/text hashes, per-page diagnostics and nested readiness evidence. Uploads
retain it beside existing adventure metadata; generation uses the same quality/coverage gate.
Detected duplicated layers, mixed/incomplete stat fields, missing source pages or lost chunk
pages reject indexing before embeddings or vector writes. The API also checks live vectors,
source/extraction identity, exact chunk count and actual contributing pages before reporting
newly validated sources ready. Legacy sources retain the existing availability checks.

Native text remains authoritative for mechanics. Missing raster stat headings use targeted
Tesseract OCR; sparse native pages use explicit full-page OCR fallback. Install Tesseract with
English language data for local extraction; API/ingestion images share this runtime dependency.
Reports flag numeric glyph ambiguity and raster completeness for inspection. Automatic checks
are structural checks, not full transcription, table semantics or approved opening selection.
Unknown layouts can fail closed and require source review.

Representative text, stat records and **unreviewed** scene candidates are available from the
library/recovery preview, with a plain-text full Markdown download. Failed PDF recovery can
index an explicitly inspected current preview; exact acknowledged source/text hashes are
rechecked by the worker. Generated recovery prunes stale same-source chunks after a complete
validated upsert. Replacement retains existing post-upsert pruning; atomic promotion remains
separate work. No live index is replaced by extraction-only inspection.

For sampled offline QA, `--extract-only --pages 73 258` selects physical one-based pages.
Sample bundles are inspectable but cannot be used for full indexing. See
[EXTRACTION_QA.md](EXTRACTION_QA.md) for the five real-page checks and transcription limits.

## Migrating Qdrant data from legacy names

Either **re-ingest** each PDF with `--collection rules_*`, or use a one-off script with the Qdrant client to copy points from e.g. `player_handbook` → `rules_player_handbook` (same vectors/payloads).

## Structure

```
apps/ingestion/
├── src/ingestion/
│   ├── main.py    # CLI entrypoint
│   ├── extract.py # Canonical per-page extraction / review bundle
│   ├── chunk.py  # Heading-aware chunking (stat blocks kept whole)
│   ├── paths.py  # Shared Docker/local output path selection
│   └── embed.py  # Embed + upsert to Qdrant
├── tests/
└── pyproject.toml
```
