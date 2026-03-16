"""PDF text extraction using pymupdf4llm."""
from pathlib import Path

import pymupdf4llm


def extract_full_markdown(path: str | Path) -> str:
    """
    Extract full markdown from a PDF (all pages concatenated).

    Args:
        path: Path to the PDF file.

    Returns:
        Full markdown string.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    return pymupdf4llm.to_markdown(
        str(path),
        page_chunks=False,
        header=False,
        footer=False,
    )


def extract_and_save_markdown(
    pdf_path: str | Path,
    output_dir: str | Path,
) -> Path:
    """
    Extract full markdown from a PDF and save to output_dir/{stem}.md.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Directory to write the markdown file.

    Returns:
        Path to the written markdown file.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pdf_path.stem}.md"
    md = extract_full_markdown(pdf_path)
    out_path.write_text(md, encoding="utf-8")
    return out_path


def extract_text_by_page(path: str | Path) -> list[tuple[int, str]]:
    """
    Extract text from a PDF, one tuple (page_number, text) per page.

    Args:
        path: Path to the PDF file.

    Returns:
        List of (page_number, text) tuples. page_number is 1-based.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    chunks = pymupdf4llm.to_markdown(
        str(path),
        page_chunks=True,
        header=False,
        footer=False,
    )
    return [(c["metadata"]["page_number"], c["text"]) for c in chunks]
