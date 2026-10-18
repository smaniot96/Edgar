"""Anonymized captured layout regressions plus isolated generated PDF scenarios."""

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf
import pytest

from ingestion import embed, extract
from ingestion.chunk import chunk_pages
from ingestion.extract import (
    ExtractedPage,
    Extraction,
    extract_document,
    load_reviewed_extraction,
    save_extraction,
)
from ingestion.layout import Line, native_lines, render_native_page
from ingestion.quality import check_text, require_separate_stat_chunks

_FIXTURES = Path(__file__).parent / "fixtures" / "extraction"


@pytest.mark.parametrize(
    "filename,records",
    [
        ("player_handbook-73.json", 0),
        ("player_handbook-258.json", 0),
        ("monster_manual-167.json", 2),
        ("monster_manual-188.json", 1),
        ("monster_manual-99.json", 3),
    ],
)
def test_captured_layout_preserves_column_heading_and_record_boundaries(filename, records):
    fixture = json.loads((_FIXTURES / filename).read_text())
    lines = [Line(**line) for line in fixture["lines"]]
    # Extraction order in the PDF stream is deliberately shuffled: geometry owns order.
    lines.reverse()
    headers = [Line(**line) for line in fixture["raster_headers"]]

    def raster_header(bbox):
        return [
            line
            for line in headers
            if bbox[0] <= line.bbox[0] <= bbox[2] and bbox[1] <= line.bbox[1] < bbox[3]
        ]

    text, _warnings = render_native_page(lines, fixture["width"], header_ocr=raster_header)
    positions = [text.index(needle) for needle in fixture["expected_order"]]
    assert positions == sorted(positions)
    assert check_text(text)["errors"] == []
    chunks = chunk_pages(
        [(fixture["provenance"]["physical_page"], text)], source="anonymized", chunk_size=80
    )
    require_separate_stat_chunks(chunks)
    stat_chunks = [c for c in chunks if c["stat_block"]]
    assert len(stat_chunks) == records
    for chunk in stat_chunks:
        assert chunk["text"].count("Armor Class") == chunk["text"].count("Hit Points") == 1
        assert chunk["page"] == fixture["provenance"]["physical_page"]
    # Numeric/glyph strings captured as a source ambiguity remain unchanged, never guessed.
    for line in lines:
        if any(char.isdigit() for char in line.text) and not line.text.isupper():
            assert line.text in text
    if filename == "monster_manual-188.json":
        assert all(
            needle in stat_chunks[0]["text"]
            for needle in ("left_action", "right_action", "Leadership.", "Parry.")
        )


def test_identical_overlapping_native_layer_removed_without_dropping_repeated_table_values():
    spans = [
        {"text": "Prepared", "bbox": [20, 40, 60, 50], "size": 10},
        {"text": "Prepared", "bbox": [20.4, 40, 60.4, 50], "size": 10},
        {"text": "Prepared", "bbox": [90, 40, 130, 50], "size": 10},
    ]
    lines, removed = native_lines(
        {"blocks": [{"lines": [{"bbox": [20, 40, 130, 50], "spans": spans}]}]}, 800
    )
    assert removed == 1
    assert lines[0].text == "PreparedPrepared"


@pytest.mark.parametrize(
    "text,error",
    [
        (" ".join(["layer layer" for _ in range(30)]), "duplicate_word_layer"),
        (
            "## Creature\n\nArmor Class 14\nArmor Class 18\nHit Points 9\nHit Points 25",
            "interleaved_stat_records",
        ),
        ("## Creature\n\nMedium construct, unaligned\nArmor Class 14", "incomplete_stat_fields"),
        ("## Creature\n\nMedium construct, unaligned\nHit Points 9", "incomplete_stat_fields"),
        ("Armor Class 14\nHit Points 9", "missing_stat_heading"),
        (
            "## Creature\n\nMedium construct, unaligned\n\nArmor Class 14\nHit Points 9\n\n"
            "## ACTIONS\n\nBite. The creature hits.\n\n"
            "Armor Class 16\nHit Points 23",
            "interleaved_stat_records",
        ),
        (
            "## Creature\n\nMedium construct, unaligned\n\nArmor Class 14\n\n"
            "## ACTIONS\n\nHealing Touch. The creature regains 20 hit points.",
            "incomplete_stat_fields",
        ),
    ],
)
def test_corrupt_layers_and_stat_boundaries_detected(text, error):
    assert error in check_text(text)["errors"]


def test_stat_field_anchors_ignore_class_tables_and_healing_prose():
    class_table = (
        "HIT POINTS\n\nHit Points at 1st Level: 10 + your Constitution modifier.\n"
        "Hit Points at Higher Levels: 1d10 (or 6) + your Constitution modifier per level.\n"
        "Life Domain: restore hit points equal to 1d6 + the spell's level."
    )
    assert check_text(class_table)["errors"] == []
    healing = (
        "## Deva\n\nMedium celestial, lawful good\n\nArmor Class 17\nHit Points 136\n\n"
        "## ACTIONS\n\nHealing Touch. The deva touches another creature and it regains "
        "20 hit points.\n\nMultiattack. The deva makes two attacks."
    )
    result = check_text(healing)
    assert result["errors"] == []
    assert result["armor_class_fields"] == result["hit_points_fields"] == 1


def test_colon_style_object_lines_are_not_stat_fields():
    objects = "\n\n".join(
        f"Siege Object {i}\nArmor Class: 15\nHit Points: {50 + i}" for i in range(4)
    )
    result = check_text(f"## Siege Weapons\n\n{objects}")
    assert "interleaved_stat_records" not in result["errors"]
    assert result["errors"] == []
    assert result["armor_class_fields"] == result["hit_points_fields"] == 0


def test_bold_stat_field_blocks_keep_their_checks():
    block = (
        "## Creature\n\nMedium beast, unaligned\n\n"
        "**Armor Class** 15 (natural armor)\n**Hit Points** 50 (6d10+18)\n\n"
        "## ACTIONS\n\nBite. The creature hits."
    )
    assert check_text(block)["errors"] == []
    concatenated = block.replace(
        "Bite. The creature hits.",
        "Bite. The creature hits.\n\n**Armor Class** 16 (natural armor)\n**Hit Points** 60",
    )
    assert "interleaved_stat_records" in check_text(concatenated)["errors"]
    missing_hp = block.replace("**Hit Points** 50 (6d10+18)", "Speed 30 ft.")
    assert "incomplete_stat_fields" in check_text(missing_hp)["errors"]


def test_require_separate_stat_chunks_allows_prose_and_rejects_concatenated_records():
    prose = {"index": 0, "page": 1, "stat_block": True, "text": "Healing. Regains 20 hit points."}
    require_separate_stat_chunks([prose])
    concatenated = {
        "index": 1,
        "page": 2,
        "stat_block": True,
        "text": "Armor Class 14\nHit Points 9\n\nArmor Class 16\nHit Points 23",
    }
    with pytest.raises(ValueError, match="Mixed stat records in chunk 1 on page 2"):
        require_separate_stat_chunks([concatenated])


def test_repeated_trait_in_separate_creatures_is_not_a_duplicate_layer():
    trait = "A prepared shared trait. " * 6
    text = f"## First\n\nArmor Class 14\nHit Points 9\n\n{trait}\n\n## Second\n\nArmor Class 16\nHit Points 23\n\n{trait}"
    assert check_text(text)["errors"] == []
    assert check_text(f"## First\n\n{trait}\n\n{trait}")["errors"] == ["duplicate_text_layer"]


def test_ambiguous_prose_columns_fail_instead_of_interleaving():
    lines = [Line("left prose", (30, 50 + i * 14, 270, 60 + i * 14), 10) for i in range(8)]
    lines += [Line("right prose", (220, 50 + i * 14, 470, 60 + i * 14), 10) for i in range(8)]
    with pytest.raises(ValueError, match="ambiguous_column_gutter"):
        render_native_page(lines, 500)


def test_spanning_prose_across_two_columns_fails_closed():
    lines = [Line("left prose", (30, 50 + i * 14, 230, 60 + i * 14), 10) for i in range(8)]
    lines += [Line("right prose", (280, 50 + i * 14, 480, 60 + i * 14), 10) for i in range(8)]
    lines.append(Line("wide middle paragraph", (30, 85, 480, 95), 10))
    with pytest.raises(ValueError, match="ambiguous_spanning_column_text"):
        render_native_page(lines, 500)


@pytest.fixture
def native_pdf(tmp_path):
    path = tmp_path / "prepared.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "PREPARED RULES", fontsize=16)
        page.insert_text(
            (40, 90),
            "\n".join(
                f"Prepared paragraph {i}: preserve this source value 2d8 + 3." for i in range(10)
            ),
        )
        doc.save(path)
    return path


def test_native_extraction_does_not_invoke_ocr_and_preserves_provenance(native_pdf, monkeypatch):
    monkeypatch.setattr(
        extract.pymupdf4llm, "to_markdown", lambda *_a, **_k: pytest.fail("unexpected OCR")
    )
    result = extract_document(native_pdf)
    result.require_valid()
    assert result.pages[0].method == "native_geometry"
    assert result.text_pages()[0][0] == 1
    assert "2d8 + 3" in result.pages[0].text
    assert result.page_count == 1 and len(result.source_sha256) == 64


def _sparse_pdf(tmp_path, text=""):
    path = tmp_path / "sparse.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(40, 40, 300, 300))
        if text:
            page.insert_text((40, 60), text)
        doc.save(path)
    return path


def test_sparse_native_uses_explicit_ocr_and_checks_retained_numbers(tmp_path, monkeypatch):
    path = _sparse_pdf(tmp_path, "Native value 13")
    calls = []

    def ocr(*_args, **kwargs):
        calls.append(kwargs)
        return "## Prepared rules\n\nOCR recovers the complete source body with native value 13 and a prepared paragraph."

    monkeypatch.setattr(extract.pymupdf4llm, "to_markdown", ocr)
    result = extract_document(path)
    result.require_valid()
    assert result.pages[0].method == "layout_ocr"
    assert calls[0]["use_ocr"] is True and calls[0]["force_ocr"] is True
    assert calls[0]["pages"] == [0]
    monkeypatch.setattr(
        extract.pymupdf4llm,
        "to_markdown",
        lambda *_a, **_kw: (
            "OCR body with a different numeric value 15 and enough prepared prose to inspect."
        ),
    )
    assert "partial_native_numbers_lost_in_ocr" in extract_document(path).pages[0].quality["errors"]


@pytest.mark.parametrize("ocr_text", ["", "~ ? xx", "word word " * 40])
def test_nonblank_sparse_page_rejects_empty_garbage_or_duplicate_ocr(
    tmp_path, monkeypatch, ocr_text
):
    monkeypatch.setattr(extract.pymupdf4llm, "to_markdown", lambda *_a, **_kw: ocr_text)
    result = extract_document(_sparse_pdf(tmp_path))
    with pytest.raises(ValueError, match="Extraction quality"):
        result.require_valid()
    markdown, report = save_extraction(result, tmp_path / "preview")
    assert markdown.exists() and json.loads(report.read_text())["pages"][0]["quality"]["errors"]


def test_genuinely_blank_page_is_classified_without_ocr(tmp_path, monkeypatch):
    path = tmp_path / "blank.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    monkeypatch.setattr(
        extract.pymupdf4llm, "to_markdown", lambda *_a, **_kw: pytest.fail("unexpected OCR")
    )
    result = extract_document(path)
    assert result.pages[0].method == "blank_page"
    assert result.pages[0].quality["blank_page"] is True


def test_ocr_exception_still_has_inspectable_actionable_report(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("no language data")

    monkeypatch.setattr(extract.pymupdf4llm, "to_markdown", unavailable)
    result = extract_document(_sparse_pdf(tmp_path))
    assert "install Tesseract" in result.pages[0].quality["errors"][0]
    assert save_extraction(result, tmp_path / "preview")[1].exists()


def test_review_bundle_is_exact_and_preserves_shared_readiness_metadata(native_pdf, tmp_path):
    result = extract_document(native_pdf)
    markdown, report = save_extraction(
        result, tmp_path / "preview", readiness={"status": "review_required"}
    )
    assert json.loads(report.read_text())["readiness"]["status"] == "review_required"
    reviewed = load_reviewed_extraction(native_pdf, report)
    assert asdict(reviewed) == asdict(result)
    markdown.write_text(markdown.read_text() + " changed")
    with pytest.raises(ValueError, match="Markdown preview changed"):
        load_reviewed_extraction(native_pdf, report)


def test_stale_pdf_and_sample_only_preview_cannot_index(native_pdf, tmp_path):
    result = extract_document(native_pdf)
    _markdown, report = save_extraction(result, tmp_path / "preview")
    data = json.loads(report.read_text())
    data["pages"] = []
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="every physical PDF page"):
        load_reviewed_extraction(native_pdf, report)
    save_extraction(result, tmp_path / "preview")
    with pymupdf.open(native_pdf) as doc:
        doc[0].insert_text((40, 400), "Changed source")
        doc.saveIncr()
    with pytest.raises(ValueError, match="does not match this PDF"):
        load_reviewed_extraction(native_pdf, report)


@pytest.mark.parametrize(
    "collection,replace",
    [
        ("rules_player_handbook", False),
        ("monster_manual", False),
        ("dnd_rules", False),
        ("adventure", True),
    ],
)
def test_pdf_review_gate_saves_preview_before_any_embedding_or_client(
    native_pdf, tmp_path, monkeypatch, collection, replace
):
    monkeypatch.setattr(
        embed, "_embed_and_upsert", lambda *_a, **_k: pytest.fail("review gate bypassed")
    )
    with pytest.raises(ValueError, match="reviewed extraction preview"):
        embed.run_pipeline(
            native_pdf,
            collection,
            markdown_dir=tmp_path / "preview",
            replace=replace,
            save_markdown=False,
        )
    assert (tmp_path / "preview" / "prepared.md").exists()
    assert (tmp_path / "preview" / "prepared.extraction.json").exists()


def test_reviewed_pipeline_indexes_the_exact_preview_once(native_pdf, tmp_path, monkeypatch):
    result = extract_document(native_pdf)
    _markdown, report = save_extraction(result, tmp_path / "preview")
    monkeypatch.setattr(
        embed, "extract_document", lambda *_a, **_k: pytest.fail("reviewed text was re-extracted")
    )
    calls = []

    def index(chunks, collection, **kwargs):
        calls.append((chunks, collection, kwargs))
        return len(chunks)

    monkeypatch.setattr(embed, "_embed_and_upsert", index)
    count = embed.run_pipeline(
        native_pdf,
        "rules_player_handbook",
        replace=True,
        reviewed_extraction=report,
        save_markdown=False,
    )
    assert count == len(calls[0][0])
    assert calls[0][2] == {"replace": True, "publication_guard": None}
    expected = chunk_pages(result.text_pages(), "prepared")
    for chunk in expected:
        chunk["source_sha256"] = result.source_sha256
        chunk["extraction_sha256"] = result.text_sha256()
        chunk["rules_edition"] = "unspecified"
    assert calls[0][0] == expected


def test_changed_preview_after_review_is_rejected_before_indexing(
    native_pdf, tmp_path, monkeypatch
):
    extraction = extract_document(native_pdf)
    acknowledged = (extraction.source_sha256, extraction.text_sha256())
    extraction.pages[0].text += "\n\nA later local edit changes the selected extraction."
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    monkeypatch.setattr(embed, "get_qdrant_client", lambda: pytest.fail("client called"))
    monkeypatch.setattr(embed, "get_embeddings", lambda *_: pytest.fail("embedding called"))
    with pytest.raises(ValueError, match="inspected extraction changed"):
        embed.run_pipeline(
            native_pdf,
            "prepared",
            reviewed_extraction=report,
            reviewed_identity=acknowledged,
            replace=True,
            save_markdown=False,
        )


def test_complete_large_stat_record_is_indivisible_by_default():
    text = "## Prepared Sentinel\n\nMedium construct, unaligned\n\nArmor Class 14\nHit Points 9\n\n"
    text += "\n\n".join(f"Prepared trait {i}: " + "Long source body. " * 50 for i in range(8))
    text += "\n\n## ACTIONS\n\nFINAL_ACTION\n\n## Next Creature\n\nMore prose."
    chunks = chunk_pages([(1, text)], "prepared", chunk_size=100)
    stat_chunks = [c for c in chunks if c["stat_block"]]
    assert len(stat_chunks) == 1
    assert "FINAL_ACTION" in stat_chunks[0]["text"]


def test_raster_header_ocr_invokes_tesseract_language_and_tsv_coordinates(monkeypatch):
    from types import SimpleNamespace

    from ingestion import layout

    calls = []
    monkeypatch.setattr(layout.shutil, "which", lambda name: "/isolated/tesseract")
    tsv = (
        b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        b"5\t1\t1\t1\t1\t1\t10\t20\t100\t40\t96.0\tSENTINEL\n"
        b"5\t1\t1\t1\t1\t2\t120\t20\t30\t40\t20.0\tnoise\n"
    )

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=tsv)

    monkeypatch.setattr(layout.subprocess, "run", run)
    page = SimpleNamespace(
        get_pixmap=lambda **kwargs: SimpleNamespace(tobytes=lambda fmt: b"prepared-png")
    )
    lines = layout.ocr_stat_header(page, (20, 40, 220, 100))
    assert calls[0][0] == [
        "/isolated/tesseract",
        "stdin",
        "stdout",
        "-l",
        "eng",
        "--psm",
        "6",
        "tsv",
    ]
    assert calls[0][1] == {
        "input": b"prepared-png",
        "capture_output": True,
        "timeout": 45,
        "check": True,
    }
    assert lines[0].text == "SENTINEL"
    assert lines[0].bbox == pytest.approx((21.2, 42.4, 33.2, 47.2))


def test_raster_header_failure_is_saved_in_native_page_diagnostics(tmp_path, monkeypatch):
    import subprocess

    path = tmp_path / "stat.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text(
            (40, 80),
            "\n".join(
                [
                    "Armor Class 14",
                    "Hit Points 9",
                    "Speed 20 ft.",
                    *[
                        f"Prepared trait {i} preserves its native mechanics and original value."
                        for i in range(10)
                    ],
                ]
            ),
        )
        doc.save(path)

    def failure(*_args):
        raise subprocess.CalledProcessError(1, ["tesseract"], stderr=b"missing English data")

    monkeypatch.setattr(extract, "ocr_stat_header", failure)
    result = extract_document(path)
    assert result.pages[0].method == "native_geometry"
    assert result.pages[0].quality["errors"]
    assert save_extraction(result, tmp_path / "preview")[1].exists()


def test_recomputed_quality_rejects_forged_clean_report(native_pdf, tmp_path):
    from ingestion.artifacts import text_digest

    result = extract_document(native_pdf)
    result.pages[0].text = "duplicate duplicate " * 40
    result.pages[0].quality["errors"] = []
    _markdown, report = save_extraction(result, tmp_path / "preview")
    assert text_digest(result.pages[0].text)
    with pytest.raises(ValueError, match="duplicate_word_layer"):
        load_reviewed_extraction(native_pdf, report)


def test_reviewed_source_edition_is_stamped_and_cannot_be_relabelled(
    native_pdf, tmp_path, monkeypatch
):
    extraction = extract_document(native_pdf)
    extraction.rules_edition = "2014"
    _, report = save_extraction(extraction, tmp_path / "edition")
    writes = []
    monkeypatch.setattr(
        embed,
        "_embed_and_upsert",
        lambda chunks, *args, **kwargs: writes.append(chunks) or len(chunks),
    )
    with pytest.raises(ValueError, match="different rules edition"):
        embed.run_pipeline(
            native_pdf,
            "rules_phb",
            reviewed_extraction=report,
            rules_edition="2024",
            save_markdown=False,
        )
    assert not writes
    embed.run_pipeline(
        native_pdf,
        "rules_phb",
        reviewed_extraction=report,
        rules_edition="2014",
        save_markdown=False,
    )
    assert writes and {c["rules_edition"] for c in writes[0]} == {"2014"}
    assert load_reviewed_extraction(native_pdf, report).rules_edition == "2014"


def _extraction_with_failing_pages(page_count: int, failing: set[int]) -> Extraction:
    pages = [
        ExtractedPage(
            number,
            f"PRIVATE page {number} text",
            "native_geometry",
            {"errors": ["interleaved_stat_records"] if number in failing else []},
        )
        for number in range(1, page_count + 1)
    ]
    return Extraction("sample", "0" * 64, page_count, pages)


def test_quality_summary_caps_pages_and_totals_codes_without_page_text():
    lines = _extraction_with_failing_pages(30, set(range(1, 26))).quality_summary()
    page_lines = [line for line in lines if line.startswith("page ")]
    assert len(page_lines) == extract.SUMMARY_PAGE_LIMIT
    assert page_lines[0].startswith("page 1: interleaved_stat_records (")
    assert "25 of 30 pages failed quality checks." in lines
    assert "... 5 more pages; see the extraction bundle." in lines
    assert "Totals: interleaved_stat_records x25" in lines
    assert "PRIVATE" not in "\n".join(lines)


# Runs the real CLI in a subprocess: loguru's sink configuration is process-wide.
_CLI_SCRIPT = """
import sys
import ingestion.main as cli
from ingestion.extract import Extraction, ExtractedPage

PAGE_TWO_ERRORS = sys.argv[3:]

def fake_extract(path, pages=None):
    return Extraction("sample", "0" * 64, 2, [
        ExtractedPage(1, "PRIVATE page one text", "native_geometry", {"errors": []}),
        ExtractedPage(2, "PRIVATE page two text", "native_geometry", {"errors": PAGE_TWO_ERRORS}),
    ])

cli.extract_document = fake_extract
sys.argv = ["ingest", sys.argv[1], "--extract-only", "--markdown-dir", sys.argv[2]]
cli.main()
"""


def _run_extract_cli(tmp_path, *page_two_errors):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return out, subprocess.run(
        [sys.executable, "-c", _CLI_SCRIPT, str(pdf), str(out), *page_two_errors],
        capture_output=True,
        text=True,
    )


def test_failed_extract_cli_reports_pages_and_keeps_previous_preview(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    previous = {
        name: f"previous good {name}\n"
        for name in ("sample.md", "sample.extraction.json", "sample.summary.txt")
    }
    for name, text in previous.items():
        (out / name).write_text(text, encoding="utf-8")

    out, result = _run_extract_cli(tmp_path, "interleaved_stat_records")

    assert result.returncode == 1
    for name, text in previous.items():
        assert (out / name).read_text(encoding="utf-8") == text
    assert (out / "sample.failed.md").exists()
    assert json.loads((out / "sample.failed.extraction.json").read_text())["source"] == "sample"
    assert "page 2: interleaved_stat_records" in (out / "sample.failed.summary.txt").read_text()
    assert "page 2: interleaved_stat_records" in result.stderr
    assert "Totals: interleaved_stat_records x1" in result.stderr
    assert "previous preview left unchanged" in result.stderr
    assert "PRIVATE" not in result.stderr
    assert "ValueError" not in result.stderr


def test_clean_extract_cli_replaces_preview_and_writes_summary(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "sample.md").write_text("previous good preview\n", encoding="utf-8")

    out, result = _run_extract_cli(tmp_path)

    assert result.returncode == 0
    assert "PRIVATE" not in result.stderr
    assert (out / "sample.md").read_text(encoding="utf-8") != "previous good preview\n"
    assert "0 of 2 pages failed quality checks." in (out / "sample.summary.txt").read_text()
    assert not (out / "sample.failed.md").exists()
