"""PDF text extraction via pymupdf4llm.

We disable headers and footers because page chrome (running titles, page numbers) pollutes
embeddings and citations. `extract_text_by_page` returns 1-based page numbers so payloads can
quote "PHB p.42" directly.
"""

from pathlib import Path

import pymupdf4llm


def extract_full_markdown(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    return pymupdf4llm.to_markdown(str(path), page_chunks=False, header=False, footer=False)


def extract_and_save_markdown(pdf_path: str | Path, output_dir: str | Path) -> Path:
    """Write the full markdown alongside the PDF and return its path."""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pdf_path.stem}.md"
    out_path.write_text(extract_full_markdown(pdf_path), encoding="utf-8")
    return out_path


def extract_text_by_page(path: str | Path) -> list[tuple[int, str]]:
    """List of `(page_number_1_based, markdown_text)` tuples; one entry per page."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True, header=False, footer=False)
    return [(c["metadata"]["page_number"], c["text"]) for c in chunks]
