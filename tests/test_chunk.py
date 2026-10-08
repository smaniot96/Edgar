"""ingestion.chunk_pages edge cases."""

from ingestion.chunk import chunk_pages


def test_chunk_empty_page_skipped() -> None:
    chunks = chunk_pages([(1, "   "), (2, "Hello")], source="doc", chunk_size=100, overlap=10)
    texts = [c["text"] for c in chunks]
    assert "Hello" in "".join(texts)


def test_chunk_long_paragraph_splits_fixed_size() -> None:
    long_para = "x" * 250
    chunks = chunk_pages([(1, long_para)], source="doc", chunk_size=80, overlap=10)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c["text"]) <= 80


def test_chunk_mid_page_boundary_two_pages() -> None:
    p1 = "First page only text.\n\nSecond paragraph same page."
    p2 = "Second page starts here."
    chunks = chunk_pages([(1, p1), (2, p2)], source="book", chunk_size=50, overlap=5)
    pages = {c["page"] for c in chunks}
    assert 1 in pages
    assert 2 in pages


def test_chunk_multi_page_preserves_source() -> None:
    chunks = chunk_pages([(1, "A"), (2, "B")], source="mod", chunk_size=10, overlap=0)
    assert all(c["source"] == "mod" for c in chunks)
    indices = [c["index"] for c in chunks]
    assert indices == sorted(indices)


# --- heading-aware chunking -----------------------------------------------------------------

_MODULE = """# The Lost Temple

Intro text before any chapter.

## Chapter 2: The Temple

The temple looms over the town.

### Area 3. Shrine

A cracked altar stands here.
"""

_STAT_BLOCK_PAGE = """## Goblin

_Small humanoid (goblinoid), neutral evil_

**Armor Class** 15 (leather armor, shield) **Hit Points** 7 (2d6) **Speed** 30 ft.

|STR|DEX|CON|INT|WIS|CHA|
|---|---|---|---|---|---|
|8 (-1)|14 (+2)|10 (+0)|10 (+0)|8 (-1)|8 (-1)|

**Skills** Stealth +6 **Senses** darkvision 60 ft., passive Perception 9 **Challenge** 1/4 (50 XP)

_Nimble Escape._ The goblin can take the Disengage or Hide action as a bonus action on each of its turns.

## ACTIONS

_Scimitar. Melee Weapon Attack:_ +4 to hit, reach 5 ft., one target. _Hit:_ 5 (1d6 + 2) slashing damage.

_Shortbow. Ranged Weapon Attack:_ +4 to hit, range 80/320 ft., one target. _Hit:_ 5 (1d6 + 2) piercing damage.

## Golem

Golems are made from humble materials.
"""


def test_heading_path_prefix_and_payload_fields() -> None:
    chunks = chunk_pages([(1, _MODULE)], source="mod", chunk_size=500, overlap=50)
    by_section = {c["section"]: c for c in chunks}
    area = by_section["Area 3. Shrine"]
    assert area["heading_path"] == "Chapter 2: The Temple > Area 3. Shrine"
    assert area["chapter"] == "Chapter 2: The Temple"
    assert area["text"].startswith("Chapter 2: The Temple > Area 3. Shrine\n")
    assert "cracked altar" in area["text"]
    intro = by_section["The Lost Temple"]
    assert intro["chapter"] is None
    assert intro["page"] == 1
    # Each heading starts a new chunk even though everything fits in one chunk_size.
    assert not any("cracked altar" in c["text"] and "looms" in c["text"] for c in chunks)


def test_text_before_first_heading_has_no_heading_fields() -> None:
    chunks = chunk_pages([(1, "Just prose.\n\n## Later\n\nMore.")], source="d", chunk_size=200)
    assert chunks[0]["section"] is None and chunks[0]["heading_path"] is None
    assert chunks[0]["text"] == "Just prose."


def test_stat_block_kept_whole_including_actions() -> None:
    # chunk_size far smaller than the stat block: it must still come out as a single chunk.
    chunks = chunk_pages([(12, _STAT_BLOCK_PAGE)], source="mm", chunk_size=300, overlap=30)
    goblin = [c for c in chunks if c["section"] == "Goblin"]
    assert len(goblin) == 1
    block = goblin[0]
    assert block["stat_block"] is True
    for needle in ("Armor Class", "Hit Points", "ACTIONS", "Scimitar", "Shortbow", "Nimble Escape"):
        assert needle in block["text"]
    assert block["page"] == 12
    # ACTIONS did not open its own section; the next real heading did.
    assert {c["section"] for c in chunks} == {"Goblin", "Golem"}
    assert next(c for c in chunks if c["section"] == "Golem")["stat_block"] is False


def test_stat_block_spanning_pages_stays_together() -> None:
    first, second = _STAT_BLOCK_PAGE.split("## ACTIONS")
    chunks = chunk_pages([(40, first), (41, "## ACTIONS" + second)], source="mm", chunk_size=300)
    goblin = next(c for c in chunks if c["section"] == "Goblin")
    assert "Shortbow" in goblin["text"] and "Armor Class" in goblin["text"]
    assert goblin["page"] == 40 and goblin["page_end"] == 41


def test_long_section_split_with_cap_and_overlap() -> None:
    paras = [f"Paragraph {i} " + "lorem ipsum " * 12 for i in range(12)]
    text = "## Chapter 1: Long\n\n" + "\n\n".join(paras)
    chunks = chunk_pages([(3, text)], source="d", chunk_size=400, overlap=60)
    assert len(chunks) > 3
    for c in chunks:
        assert len(c["text"]) <= 400
        assert c["text"].startswith("Chapter 1: Long\n")
        assert c["chapter"] == "Chapter 1: Long"
    # Consecutive chunks share carried-over text.
    body0 = chunks[0]["text"].split("\n", 1)[1]
    body1 = chunks[1]["text"].split("\n", 1)[1]
    assert body1.split()[0] in body0


def test_ocr_noise_stat_subheading_is_merged() -> None:
    page = _STAT_BLOCK_PAGE.replace("## ACTIONS", "## **AC T IONS**")
    chunks = chunk_pages([(1, page)], source="mm", chunk_size=300)
    assert {c["section"] for c in chunks} == {"Goblin", "Golem"}
