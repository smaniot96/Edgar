"""Anonymized captured layout regressions plus isolated generated PDF scenarios."""

import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pymupdf
import pytest

from ingestion import embed, extract
from ingestion.artifacts import corrections_digest, exclusions_digest, source_digest, text_digest
from ingestion.chunk import chunk_page_runs, chunk_pages
from ingestion.extract import (
    RASTER_RECOVERED_KEY,
    ExtractedPage,
    Extraction,
    apply_text_corrections,
    extract_document,
    load_reviewed_extraction,
    save_extraction,
    validate_exclusions,
)
from ingestion.layout import Line, native_lines, render_native_page
from ingestion.quality import check_text, diagnostic_message, require_separate_stat_chunks
from ingestion.readiness import prepare_report, require_ready

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


def test_repeated_numbers_and_list_markers_are_not_a_duplicate_layer():
    # Spell-slot columns repeat digits, and option lists repeat one-letter markers.
    slots = "\n".join(f"{level} 4 3 3 3 2 1" for level in range(4, 24))
    options = "(a) a mace or (b) a warhammer\n(a) a light crossbow or (b) any simple weapon"
    text = f"## The Bard\n\nCantrips Known\nSpells Known\n\n{slots}\n\n{options}\n\n{options}"
    assert check_text(text)["errors"] == []


def test_repeated_words_still_fail_beside_numeric_table_rows():
    slots = "\n".join(f"{level} 4 3 3 3 2" for level in range(4, 14))
    layer = " ".join(["Bardic Bardic Inspiration"] * 12)
    assert "duplicate_word_layer" in check_text(f"{slots}\n\n{layer}")["errors"]


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


def _two_column_block(top, rows, left="left prose", right="right prose"):
    lines = [
        Line(f"{left} {i}", (30, top + i * 14, 230, top + i * 14 + 10), 10) for i in range(rows)
    ]
    lines += [
        Line(f"{right} {i}", (280, top + i * 14, 480, top + i * 14 + 10), 10) for i in range(rows)
    ]
    return lines


def test_full_width_table_band_reads_as_rows_between_column_sections():
    # Mirrors PHB p.78 / DMG p.22: a table band spanning the gutter sits between column text.
    table = []
    for i in range(4):
        y = 130 + i * 14
        table += [
            Line(f"Level {i + 1}", (30, y, 70, y + 10), 9),
            Line(f"feature {i + 1} spans the gutter", (200, y, 380, y + 10), 9),
            Line(f"+{i + 2}", (420, y, 440, y + 10), 9),
        ]
    lines = _two_column_block(50, 4, "top left", "top right") + table
    lines += _two_column_block(200, 4, "bottom left", "bottom right")
    lines.reverse()
    text, _ = render_native_page(lines, 500)
    assert "Level 1 feature 1 spans the gutter +2" in text
    assert text.index("top right 3") < text.index("Level 1") < text.index("Level 4")
    assert text.index("Level 4") < text.index("bottom left 0") < text.index("bottom right 0")


def test_decorative_bullet_is_omitted_and_column_order_is_kept():
    # Mirrors DMG p.13: a 31-pt bullet has no letters and crosses the gutter beside the text.
    lines = _two_column_block(50, 4) + _two_column_block(120, 4, "later left", "later right")
    # Wide enough to straddle every candidate gutter, so the old splitter sees it as spanning.
    lines.append(Line("\u2022", (215, 50, 315, 102), 31))
    text, _ = render_native_page(lines, 500)
    assert "\u2022" not in text
    assert text.index("left prose 3") < text.index("later left 0") < text.index("right prose 0")


def test_tiny_label_strip_above_columns_is_one_row_not_a_gutter_failure():
    # Mirrors DMG p.21: faction labels inside an illustration sit on one baseline above the text.
    labels = [
        Line("ORDER OF THE GAUNTLET", (30, 40, 120, 46), 5),
        Line("EMERALD ENCLAVE", (200, 40, 290, 46), 5),
        Line("ZHENTARIM", (380, 40, 440, 46), 5),
    ]
    text, _ = render_native_page(labels + _two_column_block(60, 6), 500)
    assert "## " not in text
    assert "ORDER OF THE GAUNTLET EMERALD ENCLAVE ZHENTARIM" in text
    assert text.index("ZHENTARIM") < text.index("left prose 0") < text.index("right prose 0")


def test_single_column_intro_below_a_left_only_block_reads_in_order():
    # Mirrors PHB p.46: a full-width intro sits under a left-only block that sets the column top.
    lines = [Line("Sidebar note", (30, 30, 120, 40), 9)]
    lines += [Line(f"intro line {i}", (30, 60 + i * 12, 480, 70 + i * 12), 10) for i in range(3)]
    lines += _two_column_block(110, 6)
    text, _ = render_native_page(lines, 500)
    assert (
        text.index("Sidebar note")
        < text.index("intro line 0")
        < text.index("left prose 0")
        < text.index("right prose 0")
    )


def test_interleaved_two_column_page_with_wide_lines_still_fails_with_band_bounds():
    lines = _two_column_block(50, 8)
    lines.append(Line("wide middle paragraph", (30, 85, 480, 95), 10))
    lines.append(Line("wide second line", (30, 100, 480, 110), 10))
    with pytest.raises(ValueError, match=r"ambiguous_spanning_column_text band=\(") as error:
        render_native_page(lines, 500)
    x0, y0, x1, y1 = (float(v) for v in re.findall(r"[\d.]+", str(error.value))[-4:])
    assert y0 <= 85 and y1 >= 110 and x0 <= 30 and x1 >= 480


def test_layout_codes_with_band_bounds_keep_their_diagnostic_message():
    code = "ambiguous_spanning_column_text band=(30.0,50.0,480.0,158.0)"
    assert diagnostic_message(code) == diagnostic_message("ambiguous_spanning_column_text")


def _single_record(name, size_text="Medium plant, neutral evil", top=60, name_size=8.5):
    """One single-column stat record; name/size_text of None leave that line to raster OCR."""
    lines = []
    if name is not None:
        lines.append(Line(name, (40, top, 200, top + 10), name_size))
    y = top + 11
    for text in (size_text, "Armor Class 14", "Hit Points 9", "Speed 20 ft."):
        if text is not None:
            lines.append(Line(text, (40, y, 200, y + 10), 8.5))
        y += 11
    return lines


def test_native_name_directly_above_size_line_is_promoted_to_heading():
    # Title case at body size fails layout._title; the tight name/size pair still promotes it.
    text, _ = render_native_page(_single_record("Twig Blight"), 500, header_ocr=lambda bbox: [])
    assert text.startswith("## Twig Blight\n\nMedium plant, neutral evil")


def test_prose_sentence_above_size_line_is_not_promoted_to_heading():
    lines = _single_record("Beware the hound.")
    with pytest.raises(ValueError, match="missing_stat_heading"):
        render_native_page(lines, 500, header_ocr=lambda bbox: [])


def test_distant_caption_above_native_name_is_not_promoted_to_heading():
    caption = Line("Dungeon survival", (40, 20, 150, 26), 5.9)
    text, _ = render_native_page(
        [caption, *_single_record("Twig Blight", top=60)], 500, header_ocr=lambda bbox: []
    )
    assert "## Twig Blight" in text
    assert "## Dungeon" not in text
    assert "Dungeon survival" in text


def test_image_only_name_above_native_size_uses_raster_name_with_review_warning():
    # Mirrors MM p.25/p.29: the name exists only in the raster header, the size line is native.
    lines = _single_record(None, size_text="Large aberration, lawful evil")
    headers = [
        Line("BEHOLDER", (40, 50, 110, 64)),
        Line("aberration, lawful evil", (40, 71, 150, 81)),
    ]
    text, warnings = render_native_page(
        lines, 500, header_ocr=lambda bbox: [h for h in headers if h.bbox[1] < bbox[3]]
    )
    assert text.startswith("## Beholder\n\nLarge aberration, lawful evil")
    assert "raster_stat_header_ocr_review_required" in warnings


def test_raster_name_keeps_ocr_spelling_and_never_takes_the_native_size_line():
    # Mirrors MM p.33: OCR reads "TWIG" as "Twic"; the re-read native size line is not a name.
    lines = _single_record(None, size_text="Small plant, neutral evil")
    headers = [
        Line("Twic BLIGHT", (40, 52, 150, 68)),
        Line("Small plant, neutral evil", (40, 71, 150, 81)),
    ]
    text, warnings = render_native_page(
        lines, 500, header_ocr=lambda bbox: [h for h in headers if h.bbox[1] < bbox[3]]
    )
    assert text.startswith("## Twic Blight\n\nSmall plant, neutral evil")
    assert "Twig" not in text
    assert "raster_stat_header_ocr_review_required" in warnings


def test_native_name_with_raster_size_line_keeps_native_heading():
    # Mirrors MM p.21 RUG OF SMOTHERING: native name, image-only size/type line.
    lines = _single_record("RUG OF SMOTHERING", size_text=None, name_size=12)
    headers = [Line("Medium object, unaligned", (40, 71, 150, 81))]
    text, warnings = render_native_page(
        lines, 500, header_ocr=lambda bbox: [h for h in headers if h.bbox[1] < bbox[3]]
    )
    assert text.startswith("## Rug Of Smothering\n\nMedium object, unaligned")
    assert "raster_stat_header_ocr_review_required" in warnings


def test_image_only_size_line_fails_with_page_specific_reason_instead_of_guessing():
    # Mirrors MM p.21 FLYING SWORD: OCR drops the italic alignment word, so no type is invented.
    lines = _single_record(None, size_text=None)
    headers = [
        Line("FLYING SWORD", (40, 52, 150, 66)),
        Line("'Small construct,", (40, 67, 150, 79)),
    ]
    with pytest.raises(ValueError, match="missing_stat_type"):
        render_native_page(
            lines, 500, header_ocr=lambda bbox: [h for h in headers if h.bbox[1] < bbox[3]]
        )


def test_uppercase_armor_class_prose_heading_is_not_a_stat_record():
    # Mirrors MM p.8: an "ARMOR CLASS" section in prose has no number after it, so it needs no name.
    lines = [Line("ARMOR CLASS", (30, 60, 150, 72), 12)]
    lines += [
        Line(f"A monster that wears armor line {i}.", (30, 80 + i * 12, 480, 90 + i * 12), 10)
        for i in range(3)
    ]
    text, _ = render_native_page(lines, 500)
    assert "A monster that wears armor line 2." in text


def test_raster_name_is_retried_in_a_wider_crop_when_the_tight_crop_misses():
    # Mirrors MM p.37 and p.65: OCR drops a name that sits close to the first crop's edge.
    lines = _single_record(None, size_text="Medium fiend, any evil alignment")
    calls = []

    def header(bbox):
        calls.append(bbox)
        return [Line("CAMBION", (40, 48, 110, 58))] if bbox[0] < 36 else []

    text, warnings = render_native_page(lines, 500, header_ocr=header)
    assert text.startswith("## Cambion\n\nMedium fiend, any evil alignment")
    assert len(calls) == 2
    assert "raster_stat_header_ocr_review_required" in warnings


def test_raster_name_just_above_native_size_line_is_found_in_a_size_anchored_crop():
    # Mirrors MM p.36 and p.58: the name sits directly above a native size line, inside a crop
    # that ends at the size line rather than at the Armor Class line.
    lines = _single_record(None, size_text="Medium humanoid, neutral evil")

    def header(bbox):
        inside = bbox[1] >= 30 and bbox[3] <= 72
        return [Line("BULLYWUG", (40, 40, 120, 52))] if inside else []

    text, warnings = render_native_page(lines, 500, header_ocr=header)
    assert text.startswith("## Bullywug\n\nMedium humanoid, neutral evil")
    assert "raster_stat_header_ocr_review_required" in warnings


def test_raster_name_is_still_required_when_no_crop_reads_one():
    # Negative: a size line alone, with no readable name in any crop, still fails closed.
    lines = _single_record(None, size_text="Medium humanoid, neutral evil")
    with pytest.raises(ValueError, match="missing_stat_heading"):
        render_native_page(lines, 500, header_ocr=lambda bbox: [])


def test_stat_field_with_a_stray_leading_glyph_still_takes_its_raster_name():
    # Mirrors MM p.209: the Armor Class line starts with a stray glyph, so it must still count.
    lines = _single_record(None, size_text="Medium humanoid, neutral good")
    lines[1] = Line(". Armor Class 11 (natural armor)", lines[1].bbox, 8.5)

    def header(bbox):
        return [Line("WEREBEAR", (40, 40, 120, 52))] if bbox[1] < 60 else []

    text, warnings = render_native_page(lines, 500, header_ocr=header)
    assert text.startswith("## Werebear\n\nMedium humanoid, neutral good")
    assert check_text(text)["errors"] == []
    assert "raster_stat_header_ocr_review_required" in warnings


def test_punctuation_strip_on_the_column_top_is_an_ornament_not_text():
    # Mirrors MM p.187: an ornament read as punctuation crosses the gutter inside a run of column
    # text. It must not make the band ambiguous, and it carries no rules text.
    lines = _two_column_block(60, 6)
    lines.append(Line("=;....!:====_,;~;::ci!::bj~~~~~:;", (150, 80, 330, 90), 9))
    text, _ = render_native_page(lines, 500)
    assert "====" not in text
    assert text.index("left prose 0") < text.index("right prose 0")


def test_table_rows_with_blank_cells_read_as_a_table_band():
    # Mirrors MM p.69: most rows of the Layers table leave one cell blank, so only three rows
    # carry the spanning cell. Every row still holds three or more cells, so rows read in order.
    table = []
    for i in range(12):
        y = 100 + i * 14
        table += [
            Line(f"row{i} first", (30, y, 70, y + 10), 9),
            Line(f"row{i} second", (80, y, 120, y + 10), 9),
            Line(f"row{i} last", (420, y, 460, y + 10), 9),
        ]
        if i % 4 == 0:
            table.append(Line(f"row{i} spanning", (200, y, 380, y + 10), 9))
    text, _ = render_native_page(table, 500)
    assert text.index("row0 first") < text.index("row6 first") < text.index("row11 first")
    assert "row0 spanning" in text and "row8 spanning" in text


def test_table_with_one_gutter_crossing_cell_in_two_rows_reads_row_by_row():
    # Mirrors PHB p.91 and p.101: a full-width class table whose wide feature cell crosses the
    # gutter on only two rows. Every row still holds three or more cells, so the rows read in order.
    table = []
    for i in range(10):
        y = 100 + i * 14
        table += [
            Line(f"lvl{i}", (30, y, 60, y + 10), 9),
            Line(f"bonus{i}", (80, y, 110, y + 10), 9),
            Line(f"feature{i}", (130, y, 180, y + 10), 9),
            Line(f"slot{i}", (360, y, 380, y + 10), 9),
            Line(f"slot2_{i}", (420, y, 440, y + 10), 9),
        ]
    table += [Line("feature crossing one", (200, 100, 320, 110), 9)]
    table += [Line("feature crossing two", (200, 114, 320, 124), 9)]
    text, _ = render_native_page(table, 500)
    assert text.index("lvl0") < text.index("lvl5") < text.index("lvl9")
    assert "lvl0 bonus0 feature0" in text or "lvl0 bonus0" in text


def test_mixed_two_column_rows_with_few_spanning_cells_still_fail_closed():
    # Negative: prose rows of two cells with a few spanning fragments stay ambiguous.
    lines = [Line(f"left {i}", (30, 50 + i * 14, 230, 60 + i * 14), 10) for i in range(12)]
    lines += [Line(f"right {i}", (280, 50 + i * 14, 480, 60 + i * 14), 10) for i in range(12)]
    lines += [Line(f"spanning {i}", (200, 60 + i * 56, 330, 70 + i * 56), 10) for i in range(3)]
    with pytest.raises(ValueError, match="ambiguous_spanning_column_text"):
        render_native_page(lines, 500)


def test_raster_recovered_name_is_recorded_with_confidence_and_crop():
    # Operator review: a name read from an image is listed with its OCR text, confidence and crop.
    lines = _single_record(None, size_text="Medium fiend, any evil alignment")
    recovered: list[dict] = []
    text, warnings = render_native_page(
        lines,
        500,
        header_ocr=lambda bbox: [Line("CAMBION", (40, 48, 110, 58), confidence=41.0)],
        recovered=recovered,
    )
    assert text.startswith("## Cambion\n\nMedium fiend, any evil alignment")
    assert recovered == [{"text": "CAMBION", "kind": "name", "confidence": 41, "crop_index": 0}]
    assert "raster_stat_header_ocr_review_required" in warnings


def test_extraction_lists_raster_reads_and_writes_a_full_sidecar(tmp_path):
    page = ExtractedPage(
        3,
        "## Devil\n\nSmall fiend (devil), lawful evil",
        "native_geometry+raster_headers",
        {
            "errors": [],
            "raster_recovered": [
                {"text": "Devil", "kind": "name", "confidence": 41, "crop_index": 3},
                {"text": "Small fiend", "kind": "size_type", "confidence": 96, "crop_index": 3},
            ],
        },
        ["raster_stat_header_ocr_review_required"],
    )
    extraction = Extraction("book", "a" * 64, 3, [page])
    summary = "\n".join(extraction.quality_summary())
    assert "Raster-read headings to verify against the page image (1 pages, 2 items" in summary
    assert 'page 3: name "Devil" (conf 41, crop 3)' in summary
    markdown, report = save_extraction(extraction, tmp_path)
    sidecar = json.loads((tmp_path / "book.raster-review.json").read_text(encoding="utf-8"))
    assert [e["text"] for e in sidecar["entries"]] == ["Devil", "Small fiend"]
    assert sidecar["entries"][0]["page"] == 3


def test_bundles_without_raster_reads_have_no_review_lines_or_sidecar(tmp_path):
    # Additive: a page with no raster-read text adds no key, no summary line and no sidecar.
    page = ExtractedPage(1, "Plain prose.", "native_geometry", {"errors": []}, [])
    extraction = Extraction("book", "a" * 64, 1, [page])
    assert not any("Raster-read" in line for line in extraction.quality_summary())
    assert "raster_recovered" not in extraction.pages[0].quality
    save_extraction(extraction, tmp_path)
    assert not (tmp_path / "book.raster-review.json").exists()


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


# Reviewed exclusions: non-content pages leave the index while every other page keeps its gates.


@pytest.fixture
def two_page_pdf(tmp_path):
    path = tmp_path / "book.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_text((40, 60), "BOOK COVER")
        doc.new_page().insert_text((40, 60), "Opening paragraph of the rules.")
        doc.save(path)
    return path


def _book(pdf, *, cover_errors=(), body_errors=(), excluded=None):
    pages = [
        ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": list(cover_errors)}),
        ExtractedPage(
            2, "Opening paragraph of the rules.", "native_geometry", {"errors": list(body_errors)}
        ),
    ]
    return Extraction("book", source_digest(pdf), 2, pages, excluded_pages=list(excluded or []))


@pytest.mark.parametrize(
    "entries,message",
    [
        ([{"page": 1, "reason": "novel"}], "reason must be one of"),
        ([{"page": 9, "reason": "cover"}], "not an extracted physical PDF page"),
        ([{"page": 0, "reason": "cover"}], "not an extracted physical PDF page"),
        ([{"page": 1, "reason": "cover"}, {"page": 1, "reason": "art"}], "more than once"),
        ([{"page": "1", "reason": "cover"}], "physical page number"),
        ([{"page": True, "reason": "cover"}], "physical page number"),
        ([{"page": 1, "reason": "cover", "note": "hidden"}], "physical page number"),
        ([{"reason": "cover"}], "physical page number"),
        ([{"page": 2, "reason": "credits"}], "contains a stat record"),
    ],
)
def test_unreviewed_or_invalid_exclusions_are_rejected(two_page_pdf, entries, message):
    pages = _book(two_page_pdf, body_errors=[]).pages
    pages[1].text = "Goblin\nSmall humanoid, neutral evil\nArmor Class 15\nHit Points 7"
    with pytest.raises(ValueError, match=message):
        validate_exclusions(entries, pages, 2)


def test_excluded_cover_passes_while_other_pages_still_gate(two_page_pdf):
    excluded = validate_exclusions([{"page": 1, "reason": "cover"}], _book(two_page_pdf).pages, 2)
    book = _book(two_page_pdf, cover_errors=["duplicate_word_layer"], excluded=excluded)
    book.require_valid()
    assert book.failing_pages() == {}
    chunks = chunk_page_runs(book.text_pages(), "book")
    assert [c["page"] for c in chunks] == [2]
    result = prepare_report(book, source_kind="pdf", chunks=chunks)
    require_ready(result)
    assert result["required_pages"] == [2]
    assert result["excluded_pages"] == [{"page": 1, "reason": "cover"}]
    assert result["exclusions_sha256"] == exclusions_digest(excluded)


def test_exclusions_never_hide_failures_on_non_excluded_pages(two_page_pdf):
    excluded = [{"page": 1, "reason": "cover"}]
    book = _book(
        two_page_pdf,
        cover_errors=["duplicate_word_layer"],
        body_errors=["interleaved_stat_records"],
        excluded=excluded,
    )
    assert book.failing_pages() == {2: ["interleaved_stat_records"]}
    chunks = chunk_page_runs(book.text_pages(), "book")
    result = prepare_report(book, source_kind="pdf", chunks=chunks)
    assert not result["quality_passed"]
    assert [(i["code"], i.get("page")) for i in result["issues"]] == [("extraction_quality", 2)]
    with pytest.raises(ValueError, match="quality checks failed"):
        book.require_valid()


def test_excluded_pages_are_never_chunked_or_merged_across_their_gap():
    pages = [
        (1, "## Chapter One\n\nAlpha paragraph on the first page."),
        (2, "Cover-only words that must not appear."),
        (3, "Omega paragraph on the third page."),
    ]
    excluded = [{"page": 2, "reason": "cover"}]
    extraction = Extraction(
        "book",
        "0" * 64,
        3,
        [ExtractedPage(n, t, "native_geometry", {"errors": []}) for n, t in pages],
        excluded_pages=excluded,
    )
    chunks = chunk_page_runs(extraction.text_pages(), "book")
    assert all("Cover-only" not in c["text"] for c in chunks)
    assert not any(c["page"] <= 2 <= c["page_end"] for c in chunks)
    assert not any("Alpha" in c["text"] and "Omega" in c["text"] for c in chunks)
    assert [c["index"] for c in chunks] == list(range(len(chunks)))


def test_readiness_rejects_any_chunk_citing_an_excluded_page():
    chunks = [
        {"page": 1, "page_end": 3, "source_pages": [1, 3], "text": "Alpha Omega", "section": "S"}
    ]
    extraction = Extraction(
        "book",
        "0" * 64,
        3,
        [ExtractedPage(n, f"text {n}", "native_geometry", {"errors": []}) for n in (1, 2, 3)],
        excluded_pages=[{"page": 2, "reason": "art"}],
    )
    result = prepare_report(extraction, source_kind="pdf", chunks=chunks)
    assert not result["quality_passed"]
    assert "excluded_page_cited" in [i["code"] for i in result["issues"]]


def test_exclusion_list_is_recorded_and_edits_require_a_new_review(two_page_pdf, tmp_path):
    excluded = [{"page": 1, "reason": "cover"}]
    with_exclusion = _book(two_page_pdf, cover_errors=["duplicate_word_layer"], excluded=excluded)
    without = _book(two_page_pdf, cover_errors=["duplicate_word_layer"])
    assert with_exclusion.text_sha256() != without.text_sha256()
    assert exclusions_digest(excluded) != exclusions_digest([])

    _markdown, report = save_extraction(with_exclusion, tmp_path / "preview")
    assert "excluded: cover (not indexed)" in (tmp_path / "preview" / "book.md").read_text()
    reviewed = load_reviewed_extraction(two_page_pdf, report)
    assert reviewed.excluded_pages == excluded

    data = json.loads(report.read_text())
    data["excluded_pages"] = []
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Excluded pages changed"):
        load_reviewed_extraction(two_page_pdf, report)

    data["exclusions_sha256"] = exclusions_digest([])
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Markdown preview|Selected text differs"):
        load_reviewed_extraction(two_page_pdf, report)


def test_exclusion_cannot_be_added_to_a_reviewed_bundle_without_new_review(two_page_pdf, tmp_path):
    _markdown, report = save_extraction(
        _book(two_page_pdf, cover_errors=["duplicate_word_layer"]), tmp_path / "preview"
    )
    data = json.loads(report.read_text())
    data["excluded_pages"] = [{"page": 1, "reason": "cover"}]
    data["exclusions_sha256"] = exclusions_digest(data["excluded_pages"])
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Markdown preview|Selected text differs"):
        load_reviewed_extraction(two_page_pdf, report)


_EXCLUDE_CLI_SCRIPT = """
import sys
import ingestion.main as cli
from ingestion.extract import Extraction, ExtractedPage

def fake_extract(path, pages=None):
    return Extraction("sample", "0" * 64, 2, [
        ExtractedPage(1, "PRIVATE page one text", "native_geometry", {"errors": []}),
        ExtractedPage(2, "PRIVATE cover text", "native_geometry", {"errors": ["duplicate_word_layer"]}),
    ])

cli.extract_document = fake_extract
sys.argv = ["ingest", sys.argv[1], "--extract-only", "--markdown-dir", sys.argv[2], "--exclude-pages", *sys.argv[3:]]
cli.main()
"""


def _run_exclude_cli(tmp_path, *exclusions):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return out, subprocess.run(
        [sys.executable, "-c", _EXCLUDE_CLI_SCRIPT, str(pdf), str(out), *exclusions],
        capture_output=True,
        text=True,
    )


def test_extract_cli_excludes_reviewed_cover_and_reports_it(tmp_path):
    out, result = _run_exclude_cli(tmp_path, "2:cover")
    assert result.returncode == 0
    summary = (out / "sample.summary.txt").read_text()
    assert "0 of 2 pages failed quality checks." in summary
    assert "Excluded from indexing (1): page 2 (cover)" in summary
    assert json.loads((out / "sample.extraction.json").read_text())["excluded_pages"] == [
        {"page": 2, "reason": "cover"}
    ]
    assert "PRIVATE" not in result.stderr


@pytest.mark.parametrize("exclusion", ["2:novel", "2", "9:cover", "2:cover:extra"])
def test_extract_cli_refuses_unreviewed_exclusions_before_writing(tmp_path, exclusion):
    out, result = _run_exclude_cli(tmp_path, exclusion)
    assert result.returncode != 0
    assert not (out / "sample.md").exists()
    assert "PRIVATE" not in result.stderr


# Reviewed text corrections: exact, unique, recorded and re-checked; never silent.

_CORRECTION = {"page": 2, "old": "Opening paragraph", "new": "Opening passage"}


def _text_book(pdf, *, body="Opening paragraph of the rules.", errors=(), excluded=None):
    pages = [
        ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": []}),
        ExtractedPage(2, body, "native_geometry", {"errors": list(errors)}),
    ]
    return Extraction("book", source_digest(pdf), 2, pages, excluded_pages=list(excluded or []))


@pytest.mark.parametrize(
    "entries,message",
    [
        ([{"page": 2, "old": "absent", "new": "x"}], "exactly once; found 0"),
        ([{"page": 2, "old": "a", "new": "x"}], "exactly once; found [1-9]"),
        ([{"page": 9, "old": "Opening", "new": "x"}], "not an extracted physical PDF page"),
        ([{"page": 2, "old": "Opening", "new": "Opening"}], "does not change the text"),
        ([{"page": 2, "old": "", "new": "x"}], "non-empty old and new"),
        ([{"page": 2, "old": "Opening", "new": ""}], "non-empty old and new"),
        ([{"page": 2, "old": "Opening", "new": "a\nb"}], "control characters or markup"),
        ([{"page": 2, "old": "Opening", "new": "<b>x</b>"}], "control characters or markup"),
        ([{"page": 2, "old": "Opening", "new": "x" * 201}], "exceeds 200 characters"),
        ([{"page": 2, "old": "Opening", "new": "x", "reason": "no"}], "needs a physical page"),
        ([{"page": "2", "old": "Opening", "new": "x"}], "needs a physical page"),
        (
            [
                {"page": 2, "old": "Opening", "new": "A"},
                {"page": 2, "old": "Opening", "new": "B"},
            ],
            "more than once",
        ),
    ],
)
def test_invalid_text_corrections_are_refused_before_recording(two_page_pdf, entries, message):
    extraction = _text_book(two_page_pdf)
    with pytest.raises(ValueError, match=message):
        apply_text_corrections(extraction, entries)
    assert extraction.text_corrections == []
    assert extraction.pages[1].text == "Opening paragraph of the rules."


def test_ambiguous_overlapping_matches_are_refused(two_page_pdf):
    extraction = _text_book(two_page_pdf, body="aaa rules")
    with pytest.raises(ValueError, match="exactly once; found 2"):
        apply_text_corrections(extraction, [{"page": 2, "old": "aa", "new": "bb"}])


def test_overlapping_corrections_on_one_page_are_refused(two_page_pdf):
    extraction = _text_book(two_page_pdf)
    entries = [
        {"page": 2, "old": "Opening paragraph", "new": "Opening"},
        {"page": 2, "old": "paragraph of", "new": "text for"},
    ]
    with pytest.raises(ValueError, match="overlap"):
        apply_text_corrections(extraction, entries)


def test_excluded_page_cannot_be_corrected(two_page_pdf):
    excluded = [{"page": 2, "reason": "art"}]
    extraction = _text_book(two_page_pdf, excluded=excluded)
    with pytest.raises(ValueError, match="excluded from indexing"):
        apply_text_corrections(extraction, [_CORRECTION])


def test_correction_is_applied_once_and_recorded_in_the_bundle(two_page_pdf, tmp_path):
    extraction = _text_book(two_page_pdf, errors=["missing_stat_type"])
    apply_text_corrections(extraction, [{**_CORRECTION, "note": "checked p.2"}])
    page = extraction.pages[1]
    assert page.text == "Opening passage of the rules."
    assert page.quality["errors"] == []
    assert page.quality["cleared_by_text_correction"] == ["missing_stat_type"]
    assert "reviewed_text_correction" in page.warnings
    with pytest.raises(ValueError, match="already applied"):
        apply_text_corrections(extraction, [_CORRECTION])

    markdown, report = save_extraction(extraction, tmp_path / "preview")
    text = markdown.read_text()
    assert "text corrected by review: 1 correction(s)" in text
    assert corrections_digest(extraction.text_corrections) in text
    data = json.loads(report.read_text())
    assert data["text_corrections"] == [
        {"page": 2, "old": "Opening paragraph", "new": "Opening passage", "note": "checked p.2"}
    ]
    assert data["corrections_sha256"] == corrections_digest(extraction.text_corrections)
    reviewed = load_reviewed_extraction(two_page_pdf, report)
    assert reviewed.text_corrections == extraction.text_corrections
    reviewed.require_valid()


def test_correction_digest_changes_with_the_list(two_page_pdf):
    base = _text_book(two_page_pdf)
    apply_text_corrections(base, [_CORRECTION])
    other = _text_book(two_page_pdf)
    apply_text_corrections(other, [{**_CORRECTION, "note": "different review"}])
    plain = _text_book(two_page_pdf)
    assert base.text_sha256() != other.text_sha256()
    assert corrections_digest(base.text_corrections) != corrections_digest(other.text_corrections)
    assert base.text_sha256() != plain.text_sha256()


def test_hand_edited_corrected_text_is_rejected_on_reload(two_page_pdf, tmp_path):
    extraction = _text_book(two_page_pdf)
    apply_text_corrections(extraction, [_CORRECTION])
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    data = json.loads(report.read_text())
    data["pages"][1]["text"] = "Opening paragraph of the rules."
    data["pages"][1]["text_sha256"] = text_digest(data["pages"][1]["text"])
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Corrected text changed"):
        load_reviewed_extraction(two_page_pdf, report)


def test_hand_edited_corrections_list_is_rejected_on_reload(two_page_pdf, tmp_path):
    extraction = _text_book(two_page_pdf)
    apply_text_corrections(extraction, [_CORRECTION])
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    data = json.loads(report.read_text())
    data["text_corrections"] = []
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Text corrections changed"):
        load_reviewed_extraction(two_page_pdf, report)

    # Recomputing only the list digest still changes the Markdown identity.
    data["corrections_sha256"] = corrections_digest([])
    report.write_text(json.dumps(data))
    with pytest.raises(
        ValueError, match="Markdown preview|Selected text differs|Corrected text changed"
    ):
        load_reviewed_extraction(two_page_pdf, report)


def test_correction_cannot_be_added_to_a_reviewed_bundle_without_new_review(two_page_pdf, tmp_path):
    _markdown, report = save_extraction(_text_book(two_page_pdf), tmp_path / "preview")
    data = json.loads(report.read_text())
    data["text_corrections"] = [_CORRECTION]
    data["corrections_sha256"] = corrections_digest(data["text_corrections"])
    report.write_text(json.dumps(data))
    with pytest.raises(
        ValueError, match="Markdown preview|Selected text differs|Corrected text changed"
    ):
        load_reviewed_extraction(two_page_pdf, report)


def test_ordinary_bundles_keep_their_shape_and_still_load(two_page_pdf, tmp_path):
    extraction = _text_book(two_page_pdf)
    markdown, report = save_extraction(extraction, tmp_path / "preview")
    assert "corrected by review" not in markdown.read_text()
    data = json.loads(report.read_text())
    assert "text_corrections" not in data and "corrections_sha256" not in data
    assert all("reviewed_text_correction" not in p["warnings"] for p in data["pages"])
    assert load_reviewed_extraction(two_page_pdf, report).text_corrections == []


def test_corrected_page_that_still_fails_its_checks_stays_failing(two_page_pdf):
    body = "Alpha " * 10 + "Beta gamma delta."
    extraction = _text_book(two_page_pdf, body=body)
    apply_text_corrections(extraction, [{"page": 2, "old": "Beta", "new": "Omega"}])
    assert extraction.failing_pages() == {2: ["duplicate_word_layer"]}
    with pytest.raises(ValueError, match="quality checks failed"):
        extraction.require_valid()


def test_correction_clears_extraction_failure_only_when_corrected_checks_pass(two_page_pdf):
    body = "The creature Armor Class l3 is recorded here."
    extraction = _text_book(two_page_pdf, body=body, errors=["missing_stat_type"])
    assert extraction.failing_pages() == {2: ["missing_stat_type"]}
    apply_text_corrections(extraction, [{"page": 2, "old": "l3", "new": "13"}])
    assert extraction.failing_pages() == {}
    assert extraction.pages[1].text == "The creature Armor Class 13 is recorded here."


def test_raster_read_heading_is_targeted_and_annotated(two_page_pdf, tmp_path):
    page2 = ExtractedPage(
        2,
        "# Devil\n\nA small fiend rests here.",
        "native_geometry+raster_headers",
        {"errors": [], RASTER_RECOVERED_KEY: [{"text": "Devil"}]},
    )
    extraction = Extraction(
        "book",
        source_digest(two_page_pdf),
        2,
        [ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": []}), page2],
    )
    apply_text_corrections(
        extraction, [{"page": 2, "old": "Devil", "new": "Spined Devil", "note": "raster heading"}]
    )
    record = extraction.pages[1].quality[RASTER_RECOVERED_KEY][0]
    assert record == {
        "text": "Devil",
        "reviewed_correction": {"old": "Devil", "new": "Spined Devil"},
    }
    _markdown, report = save_extraction(extraction, tmp_path / "preview")
    assert (
        load_reviewed_extraction(two_page_pdf, report).pages[1].quality[RASTER_RECOVERED_KEY][0]
        == record
    )

    data = json.loads(report.read_text())
    del data["pages"][1]["quality"][RASTER_RECOVERED_KEY][0]["reviewed_correction"]
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Corrected text changed"):
        load_reviewed_extraction(two_page_pdf, report)


_CORRECT_CLI_SCRIPT = """
import sys
import ingestion.main as cli
from ingestion.extract import Extraction, ExtractedPage

def fake_extract(path, pages=None):
    return Extraction("sample", "0" * 64, 2, [
        ExtractedPage(1, "PRIVATE cover Armor Class l3 text", "native_geometry", {"errors": []}),
        ExtractedPage(2, "PRIVATE Devil and Devil: the rules", "native_geometry", {"errors": []}),
    ])

cli.extract_document = fake_extract
sys.argv = ["ingest", sys.argv[1], "--extract-only", "--markdown-dir", sys.argv[2], *sys.argv[3:]]
cli.main()
"""


def _run_correct_cli(tmp_path, *args):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return out, subprocess.run(
        [sys.executable, "-c", _CORRECT_CLI_SCRIPT, str(pdf), str(out), *args],
        capture_output=True,
        text=True,
    )


def test_correct_text_cli_records_a_colon_and_space_correction(tmp_path):
    out, result = _run_correct_cli(tmp_path, "--correct-text", "1:Armor Class l3=>Armor Class 13")
    assert result.returncode == 0, result.stderr
    data = json.loads((out / "sample.extraction.json").read_text())
    assert data["text_corrections"] == [
        {"page": 1, "old": "Armor Class l3", "new": "Armor Class 13"}
    ]
    assert "Armor Class 13" in (out / "sample.md").read_text()
    assert "Text corrected by review (1 in 1 pages)" in (out / "sample.summary.txt").read_text()
    assert "PRIVATE" not in result.stderr


def test_corrections_file_records_many_corrections_with_notes(tmp_path):
    corrections = tmp_path / "corrections.json"
    corrections.write_text(
        json.dumps(
            [
                {"page": 1, "old": "Armor Class l3", "new": "Armor Class 13", "note": "p.1 check"},
                {"page": 2, "old": "Devil and", "new": "Spined Devil and"},
            ]
        )
    )
    out, result = _run_correct_cli(tmp_path, "--corrections-file", str(corrections))
    assert result.returncode == 0, result.stderr
    data = json.loads((out / "sample.extraction.json").read_text())
    assert [c["page"] for c in data["text_corrections"]] == [1, 2]
    assert data["text_corrections"][0]["note"] == "p.1 check"


@pytest.mark.parametrize(
    "args",
    [
        ["--correct-text", "1:Armor Class=>Armor Class 13", "--correct-text", "1:Armor Class=>x"],
        ["--correct-text", "1:Armor Class l3=>Armor Class 13", "--correct-text", "2:Missing=>Gone"],
        ["--correct-text", "1:Armor Class l3"],
        ["--correct-text", "x:Armor Class l3=>y"],
        ["--correct-text", "1:a=>b=>c"],
        ["--correct-text", "1:=>x"],
        ["--correct-text", "2:Devil=>Imp"],
        ["--correct-text", "1:Armor Class l3=>Armor Class 13", "--exclude-pages", "1:cover"],
    ],
)
def test_correct_text_cli_refuses_before_writing(tmp_path, args):
    out, result = _run_correct_cli(tmp_path, *args)
    assert result.returncode != 0
    assert not (out / "sample.md").exists()
    assert not (out / "sample.extraction.json").exists()
    assert "PRIVATE" not in result.stderr


def test_corrections_file_must_be_a_json_list(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"page": 1}')
    out, result = _run_correct_cli(tmp_path, "--corrections-file", str(bad))
    assert result.returncode != 0
    assert not (out / "sample.md").exists()


# Clearable versus non-clearable failures: a correction clears only text-content codes.


@pytest.mark.parametrize(
    "code",
    [
        "ambiguous_column_gutter",
        "ambiguous_spanning_column_text",
        "interleaved_stat_records",
        "duplicate_word_layer",
        "duplicate_text_layer",
        "insufficient_ocr_text_on_nonblank_page",
        "partial_native_numbers_lost_in_ocr",
        "some_unknown_future_code",
    ],
)
def test_non_clearable_code_keeps_a_corrected_page_failing(two_page_pdf, code):
    body = "The creature Armor Class l3 is recorded here."
    extraction = _text_book(two_page_pdf, body=body, errors=["missing_stat_type", code])
    apply_text_corrections(extraction, [{"page": 2, "old": "l3", "new": "13"}])
    # The clearable code stays too: a page that is not cleared keeps its earlier failures.
    assert extraction.pages[1].quality["errors"] == sorted({code, "missing_stat_type"})
    assert extraction.pages[1].quality["not_clearable_by_text_correction"] == [code]
    assert extraction.failing_pages() == {2: sorted({code, "missing_stat_type"})}
    assert f"page 2: correction cannot clear non-clearable codes {code}" in "\n".join(
        extraction.quality_summary()
    )
    with pytest.raises(ValueError, match="quality checks failed"):
        extraction.require_valid()


@pytest.mark.parametrize(
    "code",
    ["missing_stat_heading", "missing_stat_type", "incomplete_stat_fields", "garbage_stat_heading"],
)
def test_clearable_text_code_is_cleared_by_a_passing_correction(two_page_pdf, code):
    body = "The creature Armor Class l3 is recorded here."
    extraction = _text_book(two_page_pdf, body=body, errors=[code])
    apply_text_corrections(extraction, [{"page": 2, "old": "l3", "new": "13"}])
    assert extraction.failing_pages() == {}
    assert extraction.pages[1].quality["cleared_by_text_correction"] == [code]


def test_duplicate_word_layer_cannot_be_cleared_even_when_the_text_now_passes(two_page_pdf):
    body = "Alpha " * 9 + "delta rules."
    extraction = _text_book(two_page_pdf, body=body, errors=["duplicate_word_layer"])
    correction = {"page": 2, "old": ("Alpha " * 9).strip(), "new": "Alpha"}
    apply_text_corrections(extraction, [correction])
    assert extraction.pages[1].text == "Alpha delta rules."
    assert extraction.failing_pages() == {2: ["duplicate_word_layer"]}
    assert extraction.pages[1].quality["not_clearable_by_text_correction"] == [
        "duplicate_word_layer"
    ]


def test_corrected_raster_entry_is_flagged_in_the_review_sidecar(two_page_pdf, tmp_path):
    record = {"text": "Devil", "kind": "heading", "confidence": 41, "crop_index": 0}
    page2 = ExtractedPage(
        2,
        "# Devil\n\nA small fiend rests here.",
        "native_geometry+raster_headers",
        {"errors": [], RASTER_RECOVERED_KEY: [dict(record)]},
    )
    extraction = Extraction(
        "book",
        source_digest(two_page_pdf),
        2,
        [ExtractedPage(1, "BOOK COVER", "native_geometry", {"errors": []}), page2],
    )
    apply_text_corrections(extraction, [{"page": 2, "old": "Devil", "new": "Spined Devil"}])
    _markdown, report = save_extraction(extraction, tmp_path / "preview")

    sidecar = json.loads((tmp_path / "preview" / "book.raster-review.json").read_text())
    assert sidecar["markdown_sha256"] == json.loads(report.read_text())["markdown_sha256"]
    assert sidecar["entries"] == [
        {
            "page": 2,
            "text": "Devil",
            "kind": "heading",
            "confidence": 41,
            "crop_index": 0,
            "reviewed_correction": {"old": "Devil", "new": "Spined Devil"},
        }
    ]
    reviewed = load_reviewed_extraction(two_page_pdf, report)
    assert reviewed.raster_review() == sidecar["entries"]


def _plain_fails(lines, width=500):
    from ingestion.layout import _columns

    with pytest.raises(ValueError, match="ambiguous_spanning_column_text"):
        _columns(lines, width)


def test_drop_cap_is_merged_into_its_word_only_when_the_plain_reading_fails():
    # Mirrors DMG p.43/316: a tall initial beside the first lines bridges every vertical run.
    lines = [Line("CHAPTER TITLE", (30, 20, 300, 50), 20), Line("T", (30, 42, 60, 130), 70)]
    lines += [
        Line(f"he opening line {i}", (64, 60 + i * 12, 230, 70 + i * 12), 9) for i in range(3)
    ]
    lines += [Line(f"left body {i}", (30, 140 + i * 12, 230, 150 + i * 12), 9) for i in range(8)]
    lines += [Line(f"right prose {i}", (280, 60 + i * 12, 480, 70 + i * 12), 9) for i in range(14)]
    _plain_fails(lines)
    text, _ = render_native_page(lines, 500)
    assert "The opening line 0" in text and "\nT\n" not in text
    assert text.index("opening line 2") < text.index("left body 0") < text.index("right prose 0")


def test_illustration_captions_become_separate_blocks_not_a_gutter_failure():
    # Mirrors DMG p.167/193: small captions inside an image cross the gutter above the text.
    captions = [
        Line("RING OF", (200, 58, 260, 64), 5.5, illustration=True),
        Line("SPELL TURNING", (190, 66, 270, 72), 5.5, illustration=True),
        Line("RING OF WARMTH", (400, 59, 470, 65), 5.5, illustration=True),
        Line("RING OF SWIMMING", (150, 90, 340, 96), 5.5, illustration=True),
        Line("RING OF THE RAM", (170, 112, 330, 118), 5.5, illustration=True),
    ]
    lines = captions + _two_column_block(60, 6)
    _plain_fails(lines)
    text, _ = render_native_page(lines, 500)
    assert "RING OF\nSPELL TURNING" in text
    assert text.index("SPELL TURNING") < text.index("RING OF WARMTH")


def test_table_band_below_two_column_prose_reads_rows_with_blank_cells_as_blank():
    # Mirrors DMG p.136: prose columns above treasure tables whose empty cells are absent.
    lines = _two_column_block(30, 6)
    xs = [40, 120, 175, 218, 330, 420]  # the fourth column straddles the gutter
    for r in range(8):
        y = 114 + r * 12
        for c, x in enumerate(xs):
            if r == 0 or c == 0 or c == 1 + r % 5:
                lines.append(Line(f"r{r}c{c}", (x, y, x + (70 if c == 3 else 36), y + 10), 8))
    from ingestion.layout import _table_band

    assert _table_band(lines) is not None
    text, _ = render_native_page(lines, 500)
    assert text.index("left prose 5") < text.index("right prose 5") < text.index("r0c0")
    assert text.index("r1c0") < text.index("r7c0")
    assert "r1c1" not in text or text.index("r1c0") < text.index("r1c")  # blanks stay blank


def test_prose_beside_a_table_like_stat_box_is_not_read_as_table_rows():
    # Negative: two columns with a few aligned short cells must not be read across the page.
    lines = _two_column_block(40, 10)
    lines.append(Line("wide spanning title", (30, 100, 480, 110), 10))
    lines.append(Line("another wide line", (30, 112, 480, 122), 10))
    with pytest.raises(ValueError, match="ambiguous_spanning_column_text"):
        render_native_page(lines, 500)


def test_noise_spans_before_a_stat_line_are_cut_without_changing_words():
    from ingestion.layout import strip_leading_noise

    spans = (
        ("-----....,;", (257, 520, 290, 529), 10.7),
        ("Condition Immunities", (305, 519, 400, 530), 8.4),
    )
    line = Line("-----....,;Condition Immunities", (257, 519, 400, 530), 10.7, spans=spans)
    (cleaned,) = strip_leading_noise([line])
    assert cleaned.text == "Condition Immunities" and cleaned.bbox[0] == 305 and cleaned.size == 8.4
    plain = Line(
        "A homunculus",
        (50, 10, 100, 20),
        9,
        spans=(("A ", (50, 10, 56, 20), 9), ("homunculus", (57, 10, 100, 20), 9)),
    )
    assert strip_leading_noise([plain])[0].text == "A homunculus"


def test_unread_stat_heading_keeps_reading_order_and_only_text_codes():
    from ingestion.layout import StatHeaderError

    lines = _single_record(None, size_text=None)
    with pytest.raises(StatHeaderError) as error:
        render_native_page(lines, 500, header_ocr=lambda bbox: [])
    assert str(error.value) == "missing_stat_heading"
    assert error.value.text.index("Armor Class") < error.value.text.index("Hit Points")


def test_type_and_ability_row_without_armor_class_and_hit_points_is_incomplete():
    from ingestion.quality import check_text

    text = "## Sprout\n\nSmall plant, lawful neutral\n\nSTR DEX CON INT WIS CHA\n8 (-1) 10 (+0) 10 (+0) 8 (-1) 11 (+0) 5 (-3)"
    assert "incomplete_stat_fields" in check_text(text)["errors"]
    assert not check_text("## Prose\n\nSmall folk are lawful neutral in most towns.")["errors"]
