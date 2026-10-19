"""CLI entrypoint for the ingestion pipeline."""

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from db.vector.collections import campaign_lore_collection
from edgar_core.log_safety import error_summary, redact_text
from edgar_core.model_profiles import ModelProfileError, validate_provider_configuration
from ingestion.embed import run_pipeline
from ingestion.extract import (
    EXCLUSION_REASONS,
    apply_text_corrections,
    extract_document,
    save_extraction,
    validate_exclusions,
)
from ingestion.paths import default_markdown_dir

CORRECTION_SEPARATOR = "=>"


def parse_text_correction(item: str) -> dict:
    """Parse one `PAGE:OLD=>NEW` argument; the first colon and the only `=>` delimit it.

    Quote the whole argument in the shell when the text has spaces, e.g.
    `--correct-text '5:Armor Class l3=>Armor Class 13'`. Text containing `=>` belongs in a
    --corrections-file. Raises ValueError with a message that never echoes the text.
    """
    number, separator, rest = item.partition(":")
    old, arrow, new = rest.partition(CORRECTION_SEPARATOR)
    if not separator or not number.isdigit():
        raise ValueError("--correct-text needs PAGE:OLD=>NEW with a physical page number.")
    if not arrow or CORRECTION_SEPARATOR in new or not old or not new:
        raise ValueError(
            "--correct-text needs exactly one '=>' between non-empty old and new text; "
            "use --corrections-file for text that contains '=>'."
        )
    return {"page": int(number), "old": old, "new": new}


def load_corrections_file(path: Path) -> list[object]:
    """A JSON list of {page, old, new, note?} objects; never echoes its contents on error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            f"--corrections-file must be readable UTF-8 JSON ({type(exc).__name__})."
        ) from None
    if not isinstance(data, list):
        raise ValueError("--corrections-file must contain a JSON list of corrections.")
    return data


def _write_redacted(message: str) -> None:
    # Same credential redaction as the API sink; applies to every formatted line.
    sys.stderr.write(redact_text(message))


def main() -> None:
    # Configure only the CLI process; importing it must not replace API logging sinks.
    logger.remove()
    logger.add(
        _write_redacted,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        colorize=sys.stderr.isatty(),
        backtrace=False,
        diagnose=False,
    )
    parser = argparse.ArgumentParser(
        description="Ingest PDFs into Qdrant for RAG (extract, chunk, embed, upsert)"
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help="Path to PDF file (required for ingestion and --extract-only)",
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
        "--pages",
        type=int,
        nargs="+",
        help="Physical, one-based PDF pages for --extract-only sample review (full indexing requires every page)",
    )
    parser.add_argument(
        "--exclude-pages",
        nargs="+",
        metavar="PAGE:REASON",
        help="Reviewed non-content pages left out of indexing, e.g. 2:cover 3:credits. "
        f"Reasons: {', '.join(EXCLUSION_REASONS)}. Requires --extract-only; the bundle records them.",
    )
    parser.add_argument(
        "--correct-text",
        action="append",
        default=[],
        metavar="PAGE:OLD=>NEW",
        help="Reviewed text correction on one physical page: the exact old text must occur once "
        "on that page and is replaced by the new text (quote the argument when it has spaces). "
        "Requires --extract-only; the bundle records every correction and its digest.",
    )
    parser.add_argument(
        "--corrections-file",
        type=Path,
        metavar="PATH",
        help='JSON list of reviewed corrections, each {"page", "old", "new"} with an optional '
        '"note". Same rules as --correct-text; requires --extract-only.',
    )
    parser.add_argument(
        "--reviewed-extraction",
        type=Path,
        help="JSON extraction bundle whose selected Markdown text you have visually reviewed; required for rulebooks and --replace",
    )
    parser.add_argument(
        "--rules-edition",
        choices=("2014", "2024", "unspecified"),
        help="Declared source edition; omission preserves a reviewed bundle's declaration",
    )
    parser.add_argument(
        "--markdown-dir",
        type=Path,
        default=None,
        help="Directory for markdown output (default: data/markdown)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Explicit PDF replacement requires a reviewed extraction. All ingests activate a "
        "validated source version atomically and remove only its obsolete points.",
    )
    parser.add_argument(
        "--no-save-markdown",
        action="store_true",
        help="Skip saving markdown when running full ingestion",
    )
    args = parser.parse_args()
    if args.pages and not args.extract_only:
        parser.error("--pages is supported only with --extract-only")
    exclusions = []
    for item in args.exclude_pages or []:
        number, separator, reason = item.partition(":")
        if not args.extract_only or not separator or not number.isdigit():
            parser.error("--exclude-pages needs --extract-only and entries like PAGE:REASON")
        exclusions.append({"page": int(number), "reason": reason})
    if (args.correct_text or args.corrections_file) and not args.extract_only:
        parser.error("--correct-text and --corrections-file are supported only with --extract-only")
    corrections: list[object] = []
    try:
        corrections = [parse_text_correction(item) for item in args.correct_text]
        if args.corrections_file is not None:
            corrections.extend(load_corrections_file(args.corrections_file))
    except ValueError as exc:
        parser.error(str(exc))

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

    if not args.path:
        mode = "--extract-only" if args.extract_only else "full ingestion"
        parser.error(f"path is required for {mode}")
    if not args.path.exists():
        logger.error("File not found: {}", args.path)
        sys.exit(1)

    if args.extract_only:
        try:
            out_dir = args.markdown_dir or default_markdown_dir()
            extraction = extract_document(args.path, pages=args.pages)
            extraction.rules_edition = args.rules_edition or "unspecified"
            extraction.excluded_pages = validate_exclusions(
                exclusions, extraction.pages, extraction.page_count
            )
            # Corrections are checked against the exclusions above and refused before any write.
            try:
                apply_text_corrections(extraction, corrections)
            except ValueError as exc:
                # Correction messages name pages and counts only, never the corrected text.
                logger.error("Correction refused: {}", exc)
                sys.exit(1)
            # A failed run never replaces the previous preview or bundle; it is saved beside them.
            failed = bool(extraction.failing_pages())
            stem = f"{extraction.source}.failed" if failed else extraction.source
            out_path, report_path = save_extraction(extraction, out_dir, name=stem)
            summary = extraction.quality_summary()
            (out_dir / f"{stem}.summary.txt").write_text(
                "\n".join(summary) + "\n", encoding="utf-8"
            )
            if failed:
                for line in summary:
                    logger.error("{}", line)
                logger.error(
                    "Extract failed: previous preview left unchanged; inspect {}", report_path
                )
                sys.exit(1)
            logger.info("Saved markdown to {} and diagnostics to {}", out_path, report_path)
            for line in summary:
                logger.info("{}", line)
        except Exception as e:
            logger.error("Extract failed: {}", error_summary(e))
            sys.exit(1)
        return

    try:
        validate_provider_configuration()
    except ModelProfileError as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
    collection_name = resolved_collection_name()
    try:
        n = run_pipeline(
            pdf_path=args.path,
            collection_name=collection_name,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            save_markdown=not args.no_save_markdown,
            markdown_dir=args.markdown_dir,
            replace=args.replace,
            reviewed_extraction=args.reviewed_extraction,
            rules_edition=args.rules_edition,
        )
        logger.info("Ingested {} chunks from {} into {}", n, args.path.name, collection_name)
    except Exception as e:
        logger.error("Ingestion failed: {}", error_summary(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
