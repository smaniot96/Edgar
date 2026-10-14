# Extraction layout fixtures

These five minimal JSON fixtures retain physical row rectangles and font sizes captured from
read-only local PHB pages 73/258 and MM pages 167/188/99 with PyMuPDF 1.27.2.3 on 14 October 2026.
Each fixture records the source PDF SHA256 and physical page number. All names, prose and stat
values have been replaced with generated/anonymized text. No PDF, image, full page transcription
or original rule/creature prose is committed. Raster-header rectangles represent the inspected
missing headings and size lines; their text is also anonymized.

The geometry preserves the cases that produced the defect:

- PHB 73: low-size small-cap headings, a left-column feature continuing at the right-column top.
- PHB 258: a long left-column entry must finish before the neighboring spell entries.
- MM 167: two independent stat records, one missing native name, with actions/reactions.
- MM 188: one stat record starts in the left column and continues through right-column actions,
  leadership and reactions.
- MM 99: one left-column record and two right-column records, including missing raster-only names
  and size lines, six-column ability rows, and legendary actions.

Fixture tests deliberately reverse the PDF line stream to verify coordinate-owned order. They
use captured header geometry with an isolated OCR stub, preserve generated numeric/glyph strings,
and exercise the real renderer, validation and chunker together. Separate generated PDF tests
exercise selection, sparse native/OCR loss, duplicate layers, review checks and ingestion guards.
They do not rely on private PDFs, Tesseract, embeddings, Qdrant or a paid API in CI.

Real local extraction was additionally compared to rendered originals on all five physical pages.
The tracked QA record is in `apps/ingestion/EXTRACTION_QA.md`; full extracted previews and rendered
pages stay local. Captured-layout regression tests establish layout behavior, not perfect OCR or
complete transcription of every source page.
