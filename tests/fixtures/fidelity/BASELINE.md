# EDG-054 baseline — 16 October 2026

`uv run --locked python tools/eval_fidelity.py --compact` completed with `all_passed: true`.
The baseline is pinned to corpus SHA-256
`d1f0d687c4e7df3530fd5f2b3cb9b6a398e691683e891d90e7c49945d23a2ab3` and production prompt-source
SHA-256 `e9de592383d3fc3c44728dccc3fee066bbc48c179733d0b4ab600dd46ce84ac8`.

| Measure | Result |
| --- | ---: |
| Exact/lexical positive fixture retrieval | 6 / 6 (1.00) |
| Retrieved source, edition, excerpt and section identities | 6 / 6 |
| Negative identity, source and missing-module cases | 5 / 5 |
| Deterministic applied rulings | 6 / 6 (1.00) |
| Successful retry replacement | old HP 84 → active sentinel HP 85; index version changed |
| Model/provider calls | 0 |

The six positive retrieval cases are PHB Second Wind and Magic Missile; exact MM Goblin, Troll and
Adult Red Dragon; and the Lost Mine Mage DC 13 line. Retrieval exercised production `search_rules_context`,
`retrieve_monster` and `retrieve_active_scene` against disposable local in-memory Qdrant indexes. PHB
retrieval used the production exact-heading lexical fallback because embeddings were disabled.

The six ruling cases applied Second Wind (seed 11: 13 healing on `1d10+5`), Magic Missile (three
darts, 15 force damage), Goblin AC/HP and scimitar stats, Troll AC/HP/Regeneration and bite-claw-claw
multiattack, the Adult Red Dragon's AC/HP plus conservative declaration of its shortened Fire Breath excerpt, and a check
using the source's DC 13 (seed 11: total 15, success). These call deterministic engine functions,
without LLM adjudication or narration.

## Frozen source provenance

| Document | Source SHA-256 | Extraction SHA-256 | Edition / locator |
| --- | --- | --- | --- |
| Player's Handbook | `3c707344a803f264dd08e1ba0b2c3f8f6b577b167f6758c33d2799f02ec81fcf` | `5fc1910ab761cd060408a548a7341509c2af394a7cce20375fd01867eca91bfe` | 2014; PDF pages 73 and 258 |
| Monster Manual | `51cde09adade4186028088798c9d5bc4bf2b63e1b274080d9a975943b79069f5` | `bc8f9679e21f637d54e56aa9a6cec89941a96a2ac6bad36497a5058a135bea9a` | 2014; PDF pages 99, 167 and 292 |
| Lost Mine of Phandelver Markdown | `a923884ff70861898990aa4531109c71a9ad7aa94198034f4d7c308f176248ed` | `a923884ff70861898990aa4531109c71a9ad7aa94198034f4d7c308f176248ed` | 2014; local text has no physical-page markers |

The PHB/MM source SHA values are the original local PDF hashes. Their extraction digests are the
production extractor's selected-page Markdown digests, computed from PHB pages 73/258 and MM pages
99/167/292 respectively. Source metadata says `unspecified`; 2014 is the reviewed edition from the
printed titles. The prompt source hash is a change detector; this run did not invoke any prompt.

## Limits

The retrieval rate is exact/lexical relevance over the frozen fixture index, not semantic-vector
recall or live Qdrant performance. The DC 13 line comes from a mixed-column Lost Mine Markdown span
and has no reliable printed page locator; this baseline proves exact preservation of that line and
the engine's use of an explicit DC, not a reviewed encounter-specific policy. MM page 292's current
extractor output fails `incomplete_stat_fields` and `interleaved_stat_records`; the compact Troll
excerpt is manually transcribed from and visually checked against the local printed stat panel. It
is not an extractor pass or ingestion approval. A future real-model evaluation remains necessary to
measure query interpretation, semantic retrieval and prompt-conditioned rulings.

The focused pytest run emitted seven PyMuPDF binding deprecation warnings and one local-Qdrant
warning that payload indexes are unavailable in local mode. All assertions passed.

## Integration review — 16 October 2026

Re-ran on main `2bbe5df` plus the reviewed EDG-125/054/062 integration. The prompt hash was
refreshed after reviewing the merged retrieved-excerpt trust-boundary and tag-neutralisation
changes; no prompt was invoked. The Dragon excerpt remains unchanged source text. Its abbreviated
breath wording does not match the exact parser, so it stays declared. This does not imply that
full correctly worded breath actions are unsupported; their execution is covered by EDG-043.
