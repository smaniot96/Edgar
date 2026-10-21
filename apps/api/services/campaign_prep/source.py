"""Full physical-page raster OCR. No native PDF text enters campaign authoring."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import subprocess
from pathlib import Path

import fitz
import tiktoken

from .store import Store, atomic_json, digest

OCR_CONFIG = {
    "version": "full-page-tesseract-2.0",
    "dpi": 200,
    "language": "eng",
    "psm": 3,
    "layout": "tsv-block-paragraph-line-word",
    "engine": "tesseract-5",
}
VISUAL_CANDIDATE_VERSION = "retained-layout-visual-2.1"


def file_hash(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _nontext_ink(page: dict, directory: Path) -> float:
    """Measure raster content left after masking retained OCR words, not PDF text."""
    from PIL import Image, ImageDraw

    path = directory / page.get("image", "")
    if not path.is_file():
        return 0.0
    with Image.open(path) as raster:
        gray = raster.convert("L")
        mask = ImageDraw.Draw(gray)
        for word in page.get("words", []):
            x, y, width, height = word["bbox"]
            mask.rectangle((x - 2, y - 2, x + width + 2, y + height + 2), fill=255)
        # Parchment/background shading is not nontext ink. Dark map geometry and
        # illustrations survive this threshold; image evidence alone is insufficient.
        histogram = gray.resize((256, 256)).histogram()
        return sum(histogram[:150]) / (256 * 256)


def visual_page_reasons(page: dict, directory: Path) -> list[str]:
    """Find targeted image questions even when ordinary OCR confidence is high.

    This can run against old retained receipts without altering or re-OCRing them.
    Decorative illustrations alone do not request a semantic image call.
    """
    quality = page.get("quality", {})
    if quality.get("blank"):
        return []
    reasons = []
    if quality.get("visual_interpretation_needed"):
        reasons.append("OCR confidence or sparse nontext page needs image interpretation")
    text = re.sub(r"\s+", " ", page.get("text", "")).lower()
    # Multiple stat blocks can be interleaved into high-confidence nonsense:
    # each word is correct while column ownership and table relationships are lost.
    if (text.count("armor class") >= 2 and text.count("hit points") >= 2) or len(
        re.findall(r"str\s+dex\s+con\s+int\s+wis\s+cha", text)
    ) >= 2:
        reasons.append("Multiple stat blocks require verification of column relationships")
    lines = {}
    for word in page.get("words", []):
        lines.setdefault((word.get("block"), word.get("paragraph"), word.get("line")), []).append(
            word
        )
    separated_rows = 0
    width = page.get("width", 0)
    for words in lines.values():
        boxes = sorted((w["bbox"] for w in words), key=lambda b: b[0])
        if width and len(boxes) >= 4:
            span = boxes[-1][0] + boxes[-1][2] - boxes[0][0]
            gap = max(b[0] - (a[0] + a[2]) for a, b in zip(boxes, boxes[1:], strict=False))
            if span > width * 0.65 and gap > width * 0.14:
                separated_rows += 1
    if separated_rows >= 3:
        reasons.append("Repeated aligned columns have ambiguous OCR reading order")
    # Map/plan prose plus significant raster material beyond recognized words is
    # evidence of an actual spatial question. Neither prose nor art alone suffices.
    spatial = set(
        re.findall(
            r"\b(?:maps?|plans?|platforms?|rooms?|doors?|windows?|stairs?|stairways?|"
            r"corridors?|passages?|kitchens?|libraries|bedrooms?|bridges?|paths?)\b",
            text,
        )
    )
    if len(spatial) >= 3 and _nontext_ink(page, directory) > 0.025:
        reasons.append("Spatial layout prose accompanies substantial nontext map or plan evidence")
    return reasons


def candidate_visual_pages(source: dict) -> list[int]:
    """Supplement saved spine selections from immutable local source evidence."""
    if source.get("kind") != "pdf":
        return []
    directory = Path(source["directory"])
    return [
        page["page"] for page in source.get("pages", []) if visual_page_reasons(page, directory)
    ]


def acquire_pdf(store: Store, path: Path):
    identity = file_hash(path)
    directory = store.directory / "source" / (identity[:16] + "-" + digest(OCR_CONFIG)[:12])
    directory.mkdir(parents=True, exist_ok=True)
    pages = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc, 1):
            store.check()
            receipt = directory / f"page-{index:04d}.json"
            if receipt.exists():
                value = json.loads(receipt.read_text())
                if all(
                    value.get(k)
                    and (directory / value[k]).is_file()
                    and file_hash(directory / value[k]) == value.get(k + "_sha256")
                    for k in ("image", "raw", "tsv", "hocr")
                ):
                    pages.append(value)
                    continue
            prefix = directory / f"page-{index:04d}"
            image = prefix.with_suffix(".png")
            pix = page.get_pixmap(dpi=OCR_CONFIG["dpi"], alpha=False)
            pix.save(image)
            subprocess.run(
                [
                    "tesseract",
                    str(image),
                    str(prefix),
                    "-l",
                    "eng",
                    "--psm",
                    "3",
                    "txt",
                    "tsv",
                    "hocr",
                ],
                capture_output=True,
                check=True,
            )
            raw, tsv = prefix.with_suffix(".txt"), prefix.with_suffix(".tsv")
            words = []
            for row in csv.DictReader(
                io.StringIO(tsv.read_text()), delimiter="\t", quoting=csv.QUOTE_NONE
            ):
                if row["level"] == "5" and row.get("text", "").strip():
                    words.append(
                        {
                            "text": row["text"],
                            "confidence": float(row["conf"]),
                            "bbox": [int(row[k]) for k in ("left", "top", "width", "height")],
                            "block": int(row["block_num"]),
                            "paragraph": int(row["par_num"]),
                            "line": int(row["line_num"]),
                            "order": len(words),
                        }
                    )
            regions = []
            for block in sorted({w["block"] for w in words}):
                items = [w for w in words if w["block"] == block]
                left = min(w["bbox"][0] for w in items)
                top = min(w["bbox"][1] for w in items)
                right = max(w["bbox"][0] + w["bbox"][2] for w in items)
                bottom = max(w["bbox"][1] + w["bbox"][3] for w in items)
                regions.append(
                    {
                        "id": f"p{index}-b{block}",
                        "bbox": [left, top, right - left, bottom - top],
                        "reading_order": len(regions),
                        "text": " ".join(w["text"] for w in items),
                        "relationships": "unverified; retain page image for tables, stat blocks and maps",
                    }
                )
            text = raw.read_text().replace("\x0c", "").strip()
            confidence = sum(w["confidence"] for w in words) / len(words) if words else 0
            # Raster density detects meaningful nontext pages without reading the PDF text layer.
            from PIL import Image, ImageStat

            with Image.open(image) as raster:
                gray = raster.convert("L").resize((256, 256))
                dark = sum(v for i, v in enumerate(gray.histogram()) if i < 210) / (256 * 256)
                variance = ImageStat.Stat(gray).var[0]
            visual = (len(words) < 60 and dark > 0.04) or (confidence < 65 and bool(words))
            value = {
                "page": index,
                "source_sha256": identity,
                "config": OCR_CONFIG,
                "image": image.name,
                "raw": raw.name,
                "tsv": tsv.name,
                "hocr": prefix.with_suffix(".hocr").name,
                "image_sha256": file_hash(image),
                "raw_sha256": file_hash(raw),
                "tsv_sha256": file_hash(tsv),
                "hocr_sha256": file_hash(prefix.with_suffix(".hocr")),
                "width": pix.width,
                "height": pix.height,
                "text": text,
                "words": words,
                "regions": regions,
                "quality": {
                    "mean_confidence": confidence,
                    "word_count": len(words),
                    "low_confidence_words": sum(w["confidence"] < 50 for w in words),
                    "ink_fraction": dark,
                    "variance": variance,
                    "blank": dark < 0.005,
                    "visual_interpretation_needed": visual,
                    "uncertainties": [
                        "Table relationships and map connectivity require image evidence, not word order."
                    ],
                },
            }
            atomic_json(receipt, value)
            pages.append(value)
            store.update(source_pages_done=index, source_pages_total=len(doc))
    source = {
        "kind": "pdf",
        "sha256": identity,
        "pdf_path": str(path),
        "directory": str(directory),
        "ocr_config": OCR_CONFIG,
        "ocr_engine": subprocess.check_output(["tesseract", "--version"], text=True).splitlines()[
            0
        ],
        "pages": pages,
    }
    atomic_json(directory / "source.json", source)
    return source


def token_count(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))


def packets(items, target=22000):
    """Measured evidence packing, not a spending/stopping cap. No evidence is truncated."""
    batch, size = [], 0
    for item in items:
        n = token_count(item)
        if batch and size + n > target:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += n
    if batch:
        yield batch


def evidence_pages(source, numbers=None):
    return [
        {
            "physical_page": p["page"],
            "text": p["text"],
            "regions": [{"id": r["id"], "bbox": r["bbox"]} for r in p["regions"]],
            "quality": p["quality"],
        }
        for p in source.get("pages", [])
        if numbers is None or p["page"] in numbers
    ]
