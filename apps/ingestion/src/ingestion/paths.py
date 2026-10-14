"""Output paths shared by the ingestion CLI and pipeline."""

from pathlib import Path

from edgar_core.config import EDGAR_ROOT


def default_markdown_dir() -> Path:
    """Use the Docker markdown mount when present, otherwise the repository data directory."""
    mounted = Path("/output/markdown")
    return mounted if mounted.exists() else EDGAR_ROOT / "data" / "markdown"
