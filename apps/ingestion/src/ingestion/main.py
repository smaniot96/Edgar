"""CLI entrypoint for the ingestion pipeline."""
import argparse
import sys
from pathlib import Path

from loguru import logger

from db.vector.collections import campaign_lore_collection
from ingestion.embed import run_pipeline
from ingestion.extract import extract_and_save_markdown

logger.remove()
logger.add(sys.stderr, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>")


def _default_markdown_dir() -> Path:
    """Default markdown output dir: /output/markdown (Docker) or ../data/markdown (local)."""
    if Path("/output/markdown").exists():
        return Path("/output/markdown")
    return Path("../data/markdown").resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest PDFs into Qdrant for RAG (extract, chunk, embed, upsert)"
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help="Path to PDF file (required unless --extract-only with no path)",
    )
    parser.add_argument(
        "--collection",
        "-c",
        default=None,
        metavar="NAME",
        help="Qdrant collection name (default: dnd_rules unless --campaign-id is set)",
    )
    parser.add_argument(
        "--campaign-id",
        type=int,
        default=None,
        help="If set, ingest into campaign_lore_<id> instead of --collection",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Characters per chunk (default: 1000)",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=100,
        help="Overlap between chunks (default: 100)",
    )
    parser.add_argument(
        "--extract-only",
        action="store_true",
        help="Only extract markdown to file; skip chunking and embedding",
    )
    parser.add_argument(
        "--markdown-dir",
        type=Path,
        default=None,
        help="Directory for markdown output (default: data/markdown)",
    )
    parser.add_argument(
        "--no-save-markdown",
        action="store_true",
        help="Skip saving markdown when running full ingestion",
    )
    args = parser.parse_args()

    def resolved_collection_name() -> str:
        if args.campaign_id is not None:
            target = campaign_lore_collection(args.campaign_id)
            if args.collection is not None:
                logger.warning(
                    "--campaign-id overrides --collection {}; using {}",
                    args.collection,
                    target,
                )
            return target
        if args.collection is not None:
            return args.collection
        return "dnd_rules"

    if args.extract_only:
        if not args.path:
            parser.error("path is required for --extract-only")
        if not args.path.exists():
            logger.error("File not found: {}", args.path)
            sys.exit(1)
        try:
            out_dir = args.markdown_dir or _default_markdown_dir()
            out_path = extract_and_save_markdown(args.path, out_dir)
            logger.info("Saved markdown to {}", out_path)
        except Exception:
            logger.exception("Extract failed")
            sys.exit(1)
        return

    if not args.path:
        parser.error("path is required for full ingestion")
    if not args.path.exists():
        logger.error("File not found: {}", args.path)
        sys.exit(1)

    collection_name = resolved_collection_name()
    try:
        n = run_pipeline(
            pdf_path=args.path,
            collection_name=collection_name,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            save_markdown=not args.no_save_markdown,
            markdown_dir=args.markdown_dir,
        )
        logger.info("Ingested {} chunks from {} into {}", n, args.path.name, collection_name)
    except Exception:
        logger.exception("Ingestion failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
