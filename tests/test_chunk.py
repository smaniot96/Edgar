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
