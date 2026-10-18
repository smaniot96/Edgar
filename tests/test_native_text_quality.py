"""EDG-166: degraded native OCR text layers are detected and compared with fresh OCR.

Synthetic PDFs only. Tesseract/pymupdf4llm is stubbed so these tests stay deterministic and
never need a local PDF, a language pack or network access.
"""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pymupdf
import pytest

from ingestion import extract
from ingestion.artifacts import text_digest
from ingestion.extract import extract_document, load_reviewed_extraction, save_extraction
from ingestion.text_quality import is_degraded, score_text

NO_OCR = pytest.fail  # any unexpected Tesseract call fails the test

CLEAN_LINES = [
    "Your character level increases when you gain experience points.",
    "Equipment includes armor, weapons and tools for your class.",
    "Hit Points equal your level plus your constitution modifier.",
    "Your level determines how many hit points you have at each level.",
    "Roll 1d12 for damage and add the modifier you chose at level one.",
    "Priests may cast spells from the list while they wear their armor.",
]
DEGRADED_LINES = [
    "Your character levei increases when you gain experience points.",
    "Equlpment includes armor, weapons and tools for your class.",
    "Hit Polnts equal your levei plus your constitution modifier.",
    "Your levei determines how many hit points you have at each levei.",
    "Roll ld12 for damage and add the modifier you chose at levei one.",
    "Priests may cast spells from the list while they wear their armor.",
]


def _native_pdf(tmp_path, lines, name="native.pdf"):
    path = tmp_path / name
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((40, 60), "PREPARED RULES", fontsize=16)
        page.insert_text((40, 90), "\n".join(lines))
        doc.save(path)
    return path


def _ocr_stub(text, calls=None):
    def ocr(_page):
        if calls is not None:
            calls.append(_page)
        return text

    return ocr


def test_signature_score_flags_recurring_misreadings_but_not_clean_rules_text():
    clean = score_text(" ".join(CLEAN_LINES))
    degraded = score_text(" ".join(DEGRADED_LINES))
    assert clean["signature_hits"] == 0 and not is_degraded(clean)
    assert degraded["signature_hits"] >= 2 and is_degraded(degraded)
    assert degraded["ocr_signature_score"] < clean["ocr_signature_score"]
    assert score_text("Roll 1d12 and 1d4 + 1.")["signature_hits"] == 0
    assert score_text("Roll ld12 for damage.")["signature_hits"] == 1
    assert score_text("Equlpment and levei")["signature_hits"] == 2


def test_degraded_native_layer_switches_the_whole_page_to_fresh_ocr(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(extract, "_tesseract_page_text", _ocr_stub("\n".join(CLEAN_LINES), calls))
    result = extract_document(_native_pdf(tmp_path, DEGRADED_LINES))
    page = result.pages[0]
    assert page.method == "layout_ocr"
    assert len(calls) == 1
    assert "levei" not in page.text and "level" in page.text
    scores = page.quality["source_scores"]
    assert scores["native_geometry"]["signature_hits"] >= 2
    assert scores["layout_ocr"]["signature_hits"] == 0
    assert "degraded_native_text_layer_replaced_with_ocr" in page.warnings
    assert "ocr_signature_review_required" not in page.warnings
    result.require_valid()


def test_clean_native_layer_keeps_native_geometry_without_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_tesseract_page_text", NO_OCR)
    monkeypatch.setattr(extract.pymupdf4llm, "to_markdown", NO_OCR)
    result = extract_document(_native_pdf(tmp_path, CLEAN_LINES))
    page = result.pages[0]
    assert page.method == "native_geometry"
    assert page.quality["source_scores"]["native_geometry"]["signature_hits"] == 0
    assert "layout_ocr" not in page.quality["source_scores"]
    assert not [w for w in page.warnings if w.startswith("degraded_native")]
    assert "level" in page.text and "Equipment" in page.text


@pytest.mark.parametrize(
    "ocr_text",
    [
        "\n".join(DEGRADED_LINES),
        # Same words, but the native numbers 30 are absent; OCR must not replace them.
        "\n".join(CLEAN_LINES).replace("1d12", "d12").replace("30", ""),
    ],
    ids=["ocr_not_better", "ocr_dropped_native_number"],
)
def test_degraded_native_is_kept_when_ocr_is_not_better(tmp_path, monkeypatch, ocr_text):
    lines = [*DEGRADED_LINES, "Carry 30 feet of rope."]
    monkeypatch.setattr(extract, "_tesseract_page_text", _ocr_stub(ocr_text))
    page = extract_document(_native_pdf(tmp_path, lines)).pages[0]
    assert page.method == "native_geometry"
    assert "levei" in page.text
    assert "degraded_native_text_layer_kept" in page.warnings
    assert "ocr_signature_review_required" in page.warnings
    rejected = [c for c in page.candidates if c.get("method") == "layout_ocr"]
    assert rejected and rejected[0]["rejected"]


def test_degraded_native_is_kept_with_review_warning_when_ocr_is_unavailable(tmp_path, monkeypatch):
    def unavailable(_page):
        raise RuntimeError("no language data")

    monkeypatch.setattr(extract, "_tesseract_page_text", unavailable)
    result = extract_document(_native_pdf(tmp_path, DEGRADED_LINES))
    page = result.pages[0]
    assert page.method == "native_geometry"
    assert "degraded_native_text_layer_ocr_unavailable" in page.warnings
    assert "ocr_signature_review_required" in page.warnings
    assert result.failing_pages() == {}


def test_single_remaining_misspelling_is_warned_without_running_ocr(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_tesseract_page_text", NO_OCR)
    page = extract_document(
        _native_pdf(tmp_path, [*CLEAN_LINES, "Priests may ean fight in combat."])
    ).pages[0]
    assert page.method == "native_geometry"
    assert "ocr_signature_review_required" in page.warnings
    assert page.quality["source_scores"]["native_geometry"]["signature_hits"] == 1


def test_selected_method_and_both_scores_round_trip_through_the_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_tesseract_page_text", _ocr_stub("\n".join(CLEAN_LINES)))
    pdf = _native_pdf(tmp_path, DEGRADED_LINES)
    result = extract_document(pdf)
    markdown, report = save_extraction(result, tmp_path / "preview")
    saved = json.loads(report.read_text(encoding="utf-8"))
    page = saved["pages"][0]
    assert page["method"] == "layout_ocr"
    assert page["quality"]["source_scores"]["native_geometry"]["signature_hits"] >= 2
    assert page["quality"]["source_scores"]["layout_ocr"]["signature_hits"] == 0
    reviewed = load_reviewed_extraction(pdf, report)
    assert asdict(reviewed) == asdict(result)
    assert text_digest(markdown.read_text(encoding="utf-8")) == saved["markdown_sha256"]


def test_bundle_saved_before_score_fields_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "_tesseract_page_text", NO_OCR)
    pdf = _native_pdf(tmp_path, CLEAN_LINES)
    result = extract_document(pdf)
    markdown, report = save_extraction(result, tmp_path / "preview")
    data = json.loads(report.read_text(encoding="utf-8"))
    for page in data["pages"]:
        page["quality"].pop("source_scores", None)
    report.write_text(json.dumps(data), encoding="utf-8")
    reviewed = load_reviewed_extraction(pdf, report)
    assert [p.text for p in reviewed.pages] == [p.text for p in result.pages]
    assert text_digest(markdown.read_text(encoding="utf-8")) == data["markdown_sha256"]


def test_tesseract_page_read_keeps_word_geometry_and_falls_back_to_block_order(monkeypatch):
    tsv = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t40\t60\t60\t12\t95\tLevel\n"
        "5\t1\t1\t1\t1\t2\t104\t60\t40\t12\t95\tUp\n"
        "5\t1\t1\t1\t2\t1\t40\t400\t90\t12\t95\tEquipment\n"
        "5\t1\t1\t1\t2\t2\t140\t400\t20\t12\t10\tnoise\n"
    )
    monkeypatch.setattr(extract.shutil, "which", lambda name: "/isolated/tesseract")
    monkeypatch.setattr(
        extract.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout=tsv.encode()),
    )
    page = SimpleNamespace(
        get_pixmap=lambda **_kw: SimpleNamespace(tobytes=lambda fmt: b"png"),
        rect=SimpleNamespace(width=500),
    )
    assert extract._tesseract_page_text(page) == "Level Up\n\nEquipment"
    # Ambiguous geometry (a spanning line over two columns) falls back to Tesseract order.
    monkeypatch.setattr(
        extract, "render_native_page", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("x"))
    )
    assert extract._tesseract_page_text(page) == "Level Up\nEquipment"
