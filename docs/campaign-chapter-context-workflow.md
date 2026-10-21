# Source-preserving chapter preparation

The sole campaign preparation graph lives in `apps/api/services/campaign_prep/graph.py`.
The ordinary path is local full-page OCR, one planning call, one authoring call per chapter,
local continuity/assembly, and one independent final review. For N chapters this is N+2
semantic calls if no concrete repair or identity collision is found. Every semantic call
uses `gpt-6-luna` with medium reasoning. Large evidence can require measured context packets;
there is no overall money, duration, token or repair-round stopping cap.

The planner assigns every physical source page to at least one chapter. Appendix/map/item
pages may be assigned to several chapters. Each chapter receives its complete original OCR,
selected original page images, shared entities/truths (including future reveals), chapter
briefs and compact commitments from actual dependency chapters. Images are attached to those
same planning, chapter and review requests; there is no separate visual semantic stage.
Local visual diagnostics and saved earlier interpretation receipts remain readable evidence.
AI campaigns create their spine directly from the request and use the same chapter path.

Original source files live under each campaign's `preparation-v2/source/<pdf-hash>-<ocr-config-hash>/`:
200-DPI PNG pages, raw TXT OCR, word/coordinate/confidence TSV, layout HOCR, JSON page receipts
and `source.json`. Durable graph artifacts and full paid response receipts are in
`preparation-v2/graph.sqlite3`. The original uploaded PDF is also retained. No native PDF text
is used to author campaign content.

`campaign-semantic-2.1` adds detailed source notes, page inventories, complete source stat
profiles, canonical source item procedures and ordinary source checks. The local compiler
publishes original OCR once per physical page as private planner passages, linked to each
relevant chapter's scenes/goals. Full notes are chapter-scoped. The shared runtime dossier
stays compact; the administrative DM guide remains comprehensive. Source pages and private
truths are not player discoveries. Complete custom profiles use existing module-stat bindings;
verified catalogue bases can be combined only with evidenced source modifications.

The evidence hierarchy is explicit in author/reviewer and live DM instructions. Original
adventure facts take precedence over accidental prepared contradictions. A deliberate
adaptation identifies its original fact, replacement and reason. Committed gameplay and
engine state take precedence over both: a source passage cannot undo a player choice or
reset an outcome. The final reviewer compares source to output, including omissions, rather
than checking only records that already exist.

Final review findings reference stable records. Chapter content records allow targeted
repairs to omitted running notes/items/checks. Repairs preserve unaffected contained records,
event/goal identities and page inventory, then recheck affected records and actual consumers.
No routine per-chapter critic is added. Saved accepted responses replay without another paid
submission. Old completed bundle shapes retain their original registry/fingerprint inventory;
archived Phandelver has no executable recovery path.

Preserving exact source instructions does not implement all their mechanics. Canonical item
custody and ordinary fixed-DC checks compile to existing engine definitions. Attunement,
persistent charge reset/escalation, arbitrary transformations, area damage and unsupported
traits/spells remain explicit capability findings. These execution limits are separate from
source omissions and must not trigger an endless paid prose-repair loop.

Versioned prompts are in `apps/api/services/campaign_prep/prompts/`: `principles-v2.md`,
`spine-v2.md`, `chapters-v2.md`, `review-v2.md`, `repair-v2.md`, `continuity-v2.md` and
`read-source-v2.md` (oversized source fallback). The obsolete standalone `visual-v2.md`
prompt and `VisualEvidence` dispatch schema were removed. The earlier replacement commit
`2b7fb4d` contains the removal of the superseded blueprint executor and its callers; this
change does not reintroduce it.

Focused regression evidence is in `tests/test_campaign_chapter_calls_v2.py`,
`tests/test_chapter_source_retention.py`, `tests/test_campaign_source_mechanics_v2.py` and the
adapted graph/workflow/mechanics repair tests. `tools/dev_rollout.py` builds all four owned
Docker targets from committed dev via `git archive`, then preserves the existing stack,
absolute campaign bind, PostgreSQL/Qdrant volumes and Redis container. It explicitly ignores
the separately owned `.claude/worktrees/ux-campaign-cards/` checkout, which is neither included
in the build archive nor modified. Rollout and paid-run receipts belong outside Git.
