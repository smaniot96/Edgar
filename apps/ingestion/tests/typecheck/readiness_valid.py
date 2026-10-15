"""Positive readiness type contract: the production extraction satisfies the protocol."""

from ingestion.extract import Extraction
from ingestion.readiness import ReadinessChunk, prepare_report


def check_real_extraction(extraction: Extraction) -> None:
    chunks: list[ReadinessChunk] = [{"page": 1, "text": "A complete encounter."}]
    report = prepare_report(extraction, source_kind="pdf", chunks=chunks)
    report["source_sha256"] = extraction.source_sha256
