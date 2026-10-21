"""Target image review from retained OCR/layout, without rebuilding paid artifacts."""

from copy import deepcopy

from PIL import Image, ImageDraw

from apps.api.services.campaign_prep.source import candidate_visual_pages, visual_page_reasons


def page(number, text, *, words=None, image=None, blank=False):
    return {
        "page": number,
        "text": text,
        "words": words or [],
        "image": image or f"page-{number:04d}.png",
        "width": 400,
        "height": 600,
        "quality": {
            "mean_confidence": 98,
            "visual_interpretation_needed": False,
            "blank": blank,
        },
    }


def test_embedded_map_and_interleaved_stat_blocks_are_targeted_at_high_confidence(tmp_path):
    raster = Image.new("RGB", (400, 600), "#f2ead7")
    draw = ImageDraw.Draw(raster)
    draw.rectangle((30, 310, 370, 570), fill="#52683e", outline="black", width=3)
    draw.rectangle((60, 340, 170, 440), fill="#8b6748", outline="black", width=3)
    draw.rectangle((220, 400, 340, 540), fill="#8b6748", outline="black", width=3)
    draw.line((170, 400, 220, 450), fill="black", width=6)
    raster.save(tmp_path / "page-0004.png")
    map_page = page(
        4, "Three platforms contain rooms, a kitchen and windows. A door links the rooms."
    )
    stat_page = page(
        5,
        "Bed dragon Noke\nArmor Class 16 Armor Class 12\nHit Points 75 Hit Points 55\n"
        "STR DEX CON INT WIS CHA STR DEX CON INT WIS CHA",
    )
    source = {"kind": "pdf", "directory": str(tmp_path), "pages": [map_page, stat_page]}
    before = deepcopy(source)
    image_before = (tmp_path / "page-0004.png").read_bytes()
    assert candidate_visual_pages(source) == [4, 5]
    assert "Spatial layout" in visual_page_reasons(map_page, tmp_path)[0]
    assert "stat blocks" in visual_page_reasons(stat_page, tmp_path)[0]
    assert source == before
    assert (tmp_path / "page-0004.png").read_bytes() == image_before


def test_decorative_art_and_spatial_prose_alone_do_not_request_image_calls(tmp_path):
    art = Image.new("RGB", (400, 600), "white")
    ImageDraw.Draw(art).ellipse((0, 300, 400, 600), fill="#303030")
    art.save(tmp_path / "page-0001.png")
    art.save(tmp_path / "page-0003.png")
    prose = Image.new("RGB", (400, 600), "white")
    prose.save(tmp_path / "page-0002.png")
    source = {
        "kind": "pdf",
        "directory": str(tmp_path),
        "pages": [
            page(1, "A wizard celebrates beneath the stars."),
            page(2, "A room has a door, stairs, a kitchen and windows."),
            page(3, "A room has a door, stairs, a kitchen and windows.", blank=True),
        ],
    }
    assert candidate_visual_pages(source) == []
    assert candidate_visual_pages({"kind": "ai"}) == []


def test_repeated_ocr_columns_and_original_uncertainty_remain_candidates(tmp_path):
    words = []
    for line in range(1, 4):
        for left in [20, 60, 260, 300]:
            words.append(
                {
                    "text": "cell",
                    "bbox": [left, line * 30, 25, 15],
                    "block": 1,
                    "paragraph": 1,
                    "line": line,
                }
            )
    columns = page(1, "A table with several records", words=words)
    uncertain = page(2, "Unreadable note")
    uncertain["quality"]["visual_interpretation_needed"] = True
    source = {"kind": "pdf", "directory": str(tmp_path), "pages": [columns, uncertain]}
    assert candidate_visual_pages(source) == [1, 2]
    assert "aligned columns" in visual_page_reasons(columns, tmp_path)[0]


def test_recognized_words_are_masked_before_map_ink_measurement(tmp_path):
    raster = Image.new("RGB", (400, 600), "white")
    ImageDraw.Draw(raster).rectangle((20, 20, 380, 250), fill="black")
    raster.save(tmp_path / "page-0001.png")
    text = page(
        1,
        "A room has a door, stairs, a kitchen and windows.",
        words=[
            {
                "text": "recognized",
                "bbox": [20, 20, 360, 230],
                "block": 1,
                "paragraph": 1,
                "line": 1,
            }
        ],
    )
    assert visual_page_reasons(text, tmp_path) == []
