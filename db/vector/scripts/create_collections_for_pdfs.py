"""
Create Qdrant collections for all PDFs in Edgar/data/pdfs. Collection name = PDF filename (without .pdf).
Uses the same logic as new_collection.py.

Run from Edgar:
  uv run python db/vector/scripts/create_collections_for_pdfs.py
"""
import sys

from edgar_core.config import EDGAR_ROOT

PDFS_DIR = (EDGAR_ROOT / "data" / "pdfs").resolve()


def main() -> None:
    pdfs_path = PDFS_DIR.resolve()
    if not pdfs_path.exists():
        print(f"Error: PDFs directory not found: {pdfs_path}")
        sys.exit(1)

    pdfs = sorted(pdfs_path.glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs found in {pdfs_path}")
        sys.exit(0)

    from db.vector import VECTOR_SIZE, ensure_collection, get_qdrant_client

    client = get_qdrant_client()

    for pdf in pdfs:
        name = pdf.stem
        if ensure_collection(client, name):
            print(f"Created collection '{name}' (vector_size={VECTOR_SIZE}, distance=COSINE).")
        else:
            print(f"Collection '{name}' already exists. Skipping.")

    print(f"\nDone. Created/verified {len(pdfs)} collections.")


if __name__ == "__main__":
    main()
