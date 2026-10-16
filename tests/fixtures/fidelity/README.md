# EDG-054 frozen-source fidelity corpus

`corpus.json` pins small, human-curated excerpts from the local 2014 PHB, MM and Lost Mine of
Phandelver extraction artifacts. It stores source and extraction SHA-256 values, edition basis,
physical page where the original PDF is present, stable section identity, gold facts and the current
production prompt-file hash. The excerpts are purpose-built evaluation data; they are not a complete
replacement extraction or approved reindex bundle.

The PHB samples are physical PDF pages 73 (Second Wind) and 258 (Magic Missile). They were extracted
with `ingestion.extract.extract_document`; source PDF hashes and the combined selected-page
extraction digest are pinned in the fixture. Numeric OCR glyphs were normalized only where the
printed page makes the value clear (`ld10` to `1d10`, `Id4 + I` to `1d4 + 1`). The MM Goblin and
Adult Red Dragon samples come from physical pages 167 and 99, which pass the current extractor's
structural checks. The Troll stat excerpt is manually transcribed and visually checked against the
printed stat panel on physical page 292. The current extractor reports
`incomplete_stat_fields` and `interleaved_stat_records` on that page; the fixture deliberately
records the warning and does not claim that page is ingestion-ready.

The Lost Mine source is the existing local Markdown artifact. It has no physical-page markers and
contains mixed-column text around the Mage spellcasting line. The fixture preserves the exact
`DC 13` statement but labels its locator and encounter context ambiguous. Its result measures
source-line retrieval and engine use of a supplied explicit DC; it does not certify printed page
attribution or a reviewed adventure encounter manifest. One additional short Wave Echo Cave overview
passage supplies the unconditional player text required by production's published-manifest contract;
it is not counted in retrieval or ruling metrics.

Run the deterministic harness with:

```bash
uv run python tools/eval_fidelity.py
uv run pytest tests/test_fidelity_eval.py -q
```

The harness builds disposable local in-memory Qdrant collections and calls production
`search_rules_context`, `retrieve_monster`, `retrieve_active_scene`, source-version replacement,
`parse_monster_stats`, `attack_plan`, `regenerate`, `spend_second_wind`, `cast_request`,
`resolve_spell`, and `resolve_check`. Exact-heading and exact-identity retrieval use Qdrant's local
scroll/filter behavior; query embeddings are deliberately disabled. The output reports lexical/exact
fixture relevance separately from applied ruling accuracy. These are deterministic regression
measurements over the pinned fixtures, not a claim about semantic-vector recall on a deployed
collection. Second Wind's deterministic healing helper is measured here; persistence of its use
count remains the calling combat graph's responsibility and is outside this harness.

No model or paid provider is called. The prompt SHA is checked and reported only as provenance. A
future real-model evaluation must be a separately approved, budgeted run against real retrieval
and prompt versions; it is not implied by this offline corpus.
