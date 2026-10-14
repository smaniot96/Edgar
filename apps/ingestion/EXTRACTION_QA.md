# EDG-002 extraction QA — 14 October 2026

Read-only local extraction used PyMuPDF/pymupdf4llm 1.27.2.3 and Tesseract 5.5.1. No live ingestion,
embeddings, collection/DB writes, deployment or original PDF/Markdown changes were performed.
The source PDF SHA256s are recorded with the anonymized fixtures under `tests/fixtures/extraction`.
Full source pages/text/images are not committed.

The independent baseline reproduced the current parser's duplicate native/OCR layers and column
interleaving. Native extraction alone from that layout parser also drops almost all sampled MM
text. The repair reads native coordinates directly and supplements only absent raster stat
headings/type lines and action labels; it does not disable OCR for every page.

| Physical PDF page | Default parser characters | Parser without OCR characters | Selected characters | Selected method | Stat records |
| --- | ---: | ---: | ---: | --- | ---: |
| PHB 73 | 8,987 | 4,517 | 4,442 | native geometry | — |
| PHB 258 | 11,661 | 5,287 | 5,226 | native geometry | — |
| MM 167 | 2,859 | 79 | 1,696 | native geometry + raster headers | 2 |
| MM 188 | 2,131 | 10 | 1,797 | native geometry + raster headers | 1 |
| MM 99 | 4,269 | 317 | 4,126 | native geometry + raster headers/subheadings | 3 |

The selected pages pass structural checks. PHB 73 contains 18 feature headings and follows the
left column through Action Surge before its right-column continuation. PHB 258 ends Magic Jar
before Magic Missile and preserves the neighboring spell headings. MM 167 separates Goblin and
Goblin Boss, including the latter's Redirect Attack; the illustration caption becomes its own
explicit section. MM 188 preserves one Hobgoblin Warlord block across both columns, including
Leadership and Reactions/Parry. MM 99 yields three records: Adult Red Dragon, Young Red Dragon,
and Red Dragon Wyrmling. Each contains exactly one AC/HP pair; Adult retains legendary actions.
Default chunking keeps a complete stat record together even when it exceeds the usual character
budget. Physical page/source metadata remains stable.

The duplicate detector counts adjacent case-folded word tokens after punctuation removal. It
rejects at least eight repeated pairs exceeding 4% of words, identical long paragraphs within one
record, multiple AC/HP sets in one section, missing/generic stat headings, and incomplete stat
fields. It allows a shared trait in different correctly bounded creatures. Ambiguous gutters and
body text spanning inferred columns fail closed and remain available in the preview report.

This is validation of layer handling, headings, order and stat boundaries, not complete
linguistic/numeric transcription approval. The PDFs already contain OCR errors in their native
layers: examples include `ld10`, `Id4 + I`, `l3` and `GO ft.`. A Goblin Shortbow target phrase is
absent from its native text. The extractor preserves native strings rather than guessing or
silently replacing authoritative numbers with OCR. Reports surface raster-backed completeness
review and possible numeric glyph ambiguity; absent raster headings use explicit OCR review
warnings. A reviewer must compare mechanics, omitted phrases and headings with the rendered
original before acknowledging any complete rulebook bundle for indexing. Full-document review
and reindexing have not occurred. Unfamiliar layouts may be rejected for explicit review; scanned
pages need Tesseract with English language data. Broad readiness/coverage and atomic index
promotion are separate work.

The inspection commands are offline:

```bash
uv run ingest /path/to/player_handbook.pdf --extract-only --pages 73 258 --markdown-dir /isolated/phb-preview
uv run ingest /path/to/monster_manual.pdf --extract-only --pages 167 188 99 --markdown-dir /isolated/mm-preview
uv run pytest tests/test_extraction.py tests/test_chunk.py tests/test_collections.py
```

Sample-only bundles cannot be indexed. Rulebook indexing and any PDF replacement require a
complete extraction bundle, unchanged source/preview hashes, passing recomputed structural
checks, and the explicit `--reviewed-extraction` acknowledgment. The exact reviewed text is used
without a second extraction. Existing stale-point removal semantics and unrelated source data
are preserved; atomic promotion is not introduced here.
