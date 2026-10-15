"""Negative readiness type contract: invalid discriminator and digest types fail."""

from ingestion.extract import Extraction
from ingestion.readiness import ReadinessChunk, prepare_report


def reject_bad_values(extraction: Extraction) -> None:
    chunks: list[ReadinessChunk] = [{"page": 1, "text": "A complete encounter."}]
    report = prepare_report(extraction, source_kind="image", chunks=chunks)
    report["source_sha256"] = 123
