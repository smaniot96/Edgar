"""Reviewed exact-source recovery for stat records omitted or mislabeled by an index.

This is a supplemental source receipt, not a replacement index or a monster catalog
invented by a model. Each recipe is bound to an immutable PDF checksum and visually
reviewed physical bounds. Statistics are read verbatim from that PDF at resolution
time. A missing text-layer heading may be supplied only by the recipe's explicitly
reviewed raster label. Explicit source glyph corrections retain their original text,
PDF bounds and reviewed replacement; no statistic is inferred or substituted here.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db.vector.references import normalize_reference
from edgar_core.artifacts import text_digest

_MM_2014_SHA256 = "51cde09adade4186028088798c9d5bc4bf2b63e1b274080d9a975943b79069f5"
_PHANDELVER_2014_SHA256 = "c3095b66e2f690d8a16b43a6b73c0aa6209c2e913714df5a65b3d2d0481aec07"


def canonical_source_creature_name(name: str, *, source_sha256: str, rules_edition: str) -> str:
    """Only the visually verified spelling on this exact source's physical page 135."""
    if (source_sha256 == _MM_2014_SHA256 and rules_edition == "2014"
            and normalize_reference(name) == "flameskull"):
        return "Flame Skull"
    return name


# Explicit raster review of the original PDF on 2026-10-20; these are not OCR
# heuristics. Each replacement is restricted by source bytes, page, creature and
# exact original token. Keep the original extraction immutable and retain proof.
_MODULE_PARSING_CORRECTIONS = {
    ("evilmage", 57): (
        ("l st Level", "1st Level", (70.0, 430.0, 310.0, 739.0)),
        ("ld8", "1d8", (70.0, 430.0, 310.0, 739.0)),
    ),
    ("mormesk the wraith", 59): (
        ("6dS + lS", "6d8 + 18", (70.0, 500.0, 310.0, 735.0)),
        ("3dS + 3", "3d8 + 3", (325.0, 85.0, 570.0, 170.0)),
    ),
    ("nezznar the black spider", 59): (
        ("6dS", "6d8", (325.0, 220.0, 570.0, 735.0)),
        ("ray offrost", "ray of frost", (325.0, 220.0, 570.0, 735.0)),
        ("invisihility", "invisibility", (325.0, 220.0, 570.0, 735.0)),
    ),
    ("sildar hallwinter", 61): (
        ("ld8", "1d8", (325.0, 305.0, 570.0, 685.0)),
        ("ldl0", "1d10", (325.0, 305.0, 570.0, 685.0)),
    ),
}


def reviewed_module_parsing_text(
    text: str, *, name: str, source_sha256: str, rules_edition: str, page: int,
) -> tuple[str, list[dict[str, Any]]]:
    """Apply the same exact reviewed source glyphs to preparation and runtime parsing."""
    if source_sha256 != _PHANDELVER_2014_SHA256 or rules_edition != "2014":
        return text, []
    corrections = []
    for original, reviewed, bounds in _MODULE_PARSING_CORRECTIONS.get((normalize_reference(name), page), ()):
        # A newer verified extraction can already have the correct glyph. Never
        # replace another spelling or disambiguate repeated occurrences by order.
        if original not in text:
            continue
        if text.count(original) != 1:
            from db.vector.creatures import CreatureResolutionError
            raise CreatureResolutionError(f"Reviewed source glyph {original!r} is ambiguous in {name!r}.")
        start = text.index(original)
        corrections.append({"start": start, "end": start + len(original),
            "original": original, "reviewed": reviewed, "physical_page": page,
            "bounds": list(bounds), "reviewed_by": "original PDF raster review 2026-10-20"})
    corrected = text
    for correction in sorted(corrections, key=lambda row: row["start"], reverse=True):
        corrected = corrected[:correction["start"]] + correction["reviewed"] + corrected[correction["end"]:]
    return corrected, sorted(corrections, key=lambda row: row["start"])


@dataclass(frozen=True)
class _PdfRegion:
    bounds: tuple[float, float, float, float]
    first_line: str | None = None
    reviewed_suffix: str | None = None
    reviewed_prefix: str | None = None


@dataclass(frozen=True)
class _PdfRecord:
    name: str
    page: int
    regions: tuple[_PdfRegion, ...]
    reviewed_labels: tuple[str, ...] = ()


# Reviewed against rendered physical PDF pages on 2026-10-20. Rectangles contain
# exactly one complete stat block, including every action, and no neighboring block.
# Physical 149 / 329 were excluded by the older whole-page extraction checks.
# Physical 317's raster headings disappeared from its text layer, causing both
# Zombie and Ogre Zombie to be indexed as Zombie. Preserve their distinct identities.
_MM_2014_RECORDS = {
    "ghoul": _PdfRecord("Ghoul", 149, (_PdfRegion((300.0, 430.0, 540.0, 735.0)),)),
    "giant spider": _PdfRecord("Giant Spider", 329, (_PdfRegion((297.0, 30.0, 540.0, 465.0)),)),
    "zombie": _PdfRecord(
        "Zombie", 317, (_PdfRegion((49.0, 115.0, 286.0, 418.0)),),
        ("Zombie", "Medium undead, neutral evil"),
    ),
    "ogre zombie": _PdfRecord(
        "Ogre Zombie", 317, (_PdfRegion((49.0, 427.0, 286.0, 725.0)),), ("Ogre Zombie",),
    ),
    "skeleton": _PdfRecord(
        "Skeleton", 273,
        (_PdfRegion((49.0, 485.0, 290.0, 640.0)),
         _PdfRegion((49.0, 647.0, 290.0, 720.0), reviewed_prefix="Actions")),
        ("Skeleton", "Medium undead, lawful evil"),
    ),
    "young green dragon": _PdfRecord(
        "Young Green Dragon", 95,
        (_PdfRegion((50.0, 515.0, 290.0, 720.0)),
         _PdfRegion((300.0, 515.0, 540.0, 720.0))),
    ),
    "flame skull": _PdfRecord(
        "Flame Skull", 135,
        (_PdfRegion((50.0, 465.0, 290.0, 730.0), "FLAME SKULL", "an action."),
         _PdfRegion((300.0, 465.0, 540.0, 730.0), "Magic Resistance.")),
    ),
}


def _source_pdf(source_sha256: str) -> bytes:
    """Locate the retained original, verifying its bytes rather than trusting its name."""
    from db.vector.creatures import CreatureResolutionError
    from edgar_core.config import EDGAR_ROOT

    configured = os.environ.get("EDGAR_SOURCE_PDF_DIR")
    directories = [Path(configured)] if configured else []
    directories.extend([Path("/data/pdfs"), EDGAR_ROOT / "data/pdfs"])
    found = False
    for directory in dict.fromkeys(directories):
        candidate = directory / "monster_manual.pdf"
        if candidate.is_file():
            found = True
            content = candidate.read_bytes()
            if hashlib.sha256(content).hexdigest() == source_sha256:
                # Extract from these same verified bytes, even if the path is replaced.
                return content
    raise CreatureResolutionError(
        "Reviewed exact-source recovery requires the original Monster Manual PDF "
        + ("with the frozen checksum; the retained file differs." if found else
           "in the retained PDF directory; it is unavailable.")
    )


def _pdf_record(recipe: _PdfRecord, source: str, receipt: dict, collection: str) -> dict:
    from db.vector.creatures import CreatureResolutionError, identified_creature

    try:
        import pymupdf
    except ImportError as exc:
        raise CreatureResolutionError(
            f"Exact source recovery for {recipe.name!r} requires the local PDF reader."
        ) from exc
    content = _source_pdf(receipt["source_sha256"])
    with pymupdf.open(stream=content, filetype="pdf") as document:
        if recipe.page > len(document):
            raise CreatureResolutionError(f"The verified PDF lacks physical page {recipe.page}.")
        page = document[recipe.page - 1]
        parts, selections, region_receipts = [], [], []
        for region in recipe.regions:
            rectangle = pymupdf.Rect(*region.bounds)
            if rectangle != rectangle & page.rect:
                raise CreatureResolutionError("Reviewed creature bounds extend beyond the source page.")
            # sort=True preserves the printed ability-table row instead of interleaving
            # separate PDF text objects. Keep multi-column stat blocks in reading order.
            extracted = page.get_text(clip=rectangle, sort=True)
            start = 0
            if region.first_line:
                lines = extracted.splitlines(keepends=True)
                for line in lines:
                    if line.strip().casefold().startswith(region.first_line.casefold()):
                        break
                    start += len(line)
                else:
                    raise CreatureResolutionError(
                        f"Exact source region for {recipe.name!r} lacks {region.first_line!r}."
                    )
            raw_selected = extracted[start:]
            start += len(raw_selected) - len(raw_selected.lstrip())
            end = len(extracted.rstrip())
            selected = extracted[start:end]
            selections.append(selected)
            if region.reviewed_prefix:
                selected = region.reviewed_prefix + "\n" + selected
            if region.reviewed_suffix:
                selected += "\n" + region.reviewed_suffix
            parts.append(selected)
            region_receipts.append({
                "bounds": list(region.bounds), "text_sha256": text_digest(extracted),
                "selected_start": start, "selected_end": end,
                "reviewed_suffix": region.reviewed_suffix,
                "reviewed_prefix": region.reviewed_prefix,
            })
        text = "\n".join((*recipe.reviewed_labels, *parts)).strip()
        bounds = [min(r.bounds[0] for r in recipe.regions), min(r.bounds[1] for r in recipe.regions),
                  max(r.bounds[2] for r in recipe.regions), max(r.bounds[3] for r in recipe.regions)]
        evidence = {
            "pdf_sha256": receipt["source_sha256"],
            "physical_page": recipe.page,
            "bounds": bounds,
            "regions": region_receipts,
            "parser": "pymupdf.get_text(sort=True, clip=reviewed_bounds)",
            "parser_version": pymupdf.VersionBind,
            "page_text_sha256": text_digest(page.get_text(sort=True)),
            "extracted_text_sha256": text_digest("\n".join(selections)),
            "record_text_sha256": text_digest(text),
            "reviewed_labels": list(recipe.reviewed_labels),
            "reviewed_by": "exact PDF page/column visual review 2026-10-20",
        }
    record = identified_creature({
        "collection": collection,
        "source": source,
        "source_sha256": receipt["source_sha256"],
        "extraction_sha256": receipt["extraction_sha256"],
        "rules_edition": receipt["rules_edition"],
        "index_version": receipt["index_version"],
        "section": recipe.name,
        "page": recipe.page,
        "page_end": recipe.page,
        "source_pages": [recipe.page],
        "stat_block": True,
        "text": text,
        "evidence_origin": "source_pdf",
        "source_evidence": evidence,
    })
    if record is None:
        raise CreatureResolutionError(
            f"Reviewed PDF bounds for {recipe.name!r} no longer yield one complete named stat block."
        )
    return record


def recover_creature_source(
    name: str,
    *,
    client: Any = None,
    rules_edition: str,
    candidates: Sequence[dict] = (),
    collections: list[str] | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Resolve only a reviewed spelling or exact source boundary under the active binding.

    An unrelated source's same-named record remains ambiguous. The caller's frozen
    collection/checksum constraints apply to both ordinary and supplemental evidence.
    No provider, embedding, vector search, index write or campaign write occurs.
    """
    if rules_edition != "2014" or source_sha256 not in {None, _MM_2014_SHA256}:
        return None
    if any(row.get("source_sha256") != _MM_2014_SHA256 for row in candidates):
        return None
    key = normalize_reference(name)
    if len(candidates) == 1 and key not in {"flameskull", "flame skull", "skeleton", "young green dragon"}:
        return None
    if key == "flameskull":
        # Physical 135 heads the same stat block "Flame Skull" and repeatedly names
        # it "flameskull" in its own traits. This explicit source spelling is the
        # only alias; do not broadly remove spaces from creature names.
        key = "flame skull"
    recipe = _MM_2014_RECORDS.get(key)
    if recipe is None:
        return None

    from db.vector.client import get_qdrant_client
    from db.vector.versions import active_index_read

    qdrant = client if client is not None else get_qdrant_client()
    matches = []
    for collection in dict.fromkeys(collections or ["rules_monster_manual", "monster_manual"]):
        if not qdrant.collection_exists(collection):
            continue
        state: dict[str, Any] = {}
        with active_index_read(qdrant, collection, state_out=state):
            for source, entry in (state.get("sources") or {}).items():
                receipt = entry.get("active") or {}
                if (
                    receipt.get("source_sha256") == _MM_2014_SHA256
                    and receipt.get("rules_edition") == rules_edition
                    and all(isinstance(receipt.get(field), str) and len(receipt[field]) == 64
                            for field in ("extraction_sha256", "index_version"))
                ):
                    matches.append(_pdf_record(recipe, source, receipt, collection))
    # Collection aliases can share source bytes; distinct source/extraction identities
    # remain separate and must never be selected by order.
    unique = {(r["source"], r["source_sha256"], r["extraction_sha256"], r["text_sha256"]): r
              for r in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None
