# Ingestion

PDF ingestion pipeline for RAG: extract text with pymupdf4llm, chunk, embed (OpenAI), and upsert to Qdrant.

## Usage

### Local (from Edgar root)

Paths are relative to Edgar; PDFs live in `dnd/data/pdfs/`:

```bash
# Full ingestion (extract, chunk, embed, upsert) + save markdown to data/markdown
uv run --project apps/ingestion ingest ../data/pdfs/player_handbook.pdf --collection player_handbook

# Extract markdown only (no embedding)
uv run --project apps/ingestion ingest ../data/pdfs/player_handbook.pdf --extract-only

# Inspect Qdrant collections
uv run --project apps/ingestion ingest-inspect --sample
uv run --project apps/ingestion ingest-inspect --search "ability check"
```

### Docker

PDFs live in `dnd/data/pdfs/`. Markdown is written to `dnd/data/markdown/`. From Edgar:

```bash
# Full ingestion
docker compose run --rm ingestion /data/player_handbook.pdf -c player_handbook

# Extract markdown only
docker compose run --rm ingestion /data/player_handbook.pdf --extract-only
```

Options: `--collection` / `-c`, `--chunk-size`, `--overlap`, `--extract-only`, `--markdown-dir`, `--no-save-markdown`.

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
