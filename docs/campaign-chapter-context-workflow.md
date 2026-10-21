# Source-preserving chapter preparation

The sole campaign preparation graph lives in `apps/api/services/campaign_prep/graph.py`.
The ordinary path is local full-page OCR, one planning call, chapter authoring,
local continuity/assembly, and one independent final review. Small chapters use one call;
dense chapters use several page groups assembled locally into the same chapter. For N small
chapters this is N+2 semantic calls if no concrete repair or identity collision is found. Every semantic call
uses `gpt-6-luna` with medium reasoning. Large evidence can require measured context packets;
there is no overall money, duration, token or repair-round stopping cap. Planning and
authoring compare the full prompt, strict schema, evidence and conservative image estimate
with Luna’s published context window; a 60k/180k text threshold no longer forces summaries.
Original pages stay in authoring requests; chapter packets never replace them with summaries.
Context estimates are stored separately
from actual provider token usage and never presented as billed usage.

The planner assigns every physical source page to at least one chapter. Appendix/map/item
pages may be assigned to several chapters. Each chapter receives its complete original OCR,
selected original page images, shared entities/truths (including future reveals), chapter
briefs and compact commitments from actual dependency chapters. Images are attached to those
same planning, chapter and review requests; there is no separate visual semantic stage.
The local context join also follows the page references of identities and secrets used by
that chapter. This supplies a shared appendix even when the planner allocated it elsewhere.
Uncertain shared source references attach their original page images in the same calls.
The chapter's base page inventory remains distinct from this supporting evidence inventory.
Local assembly projects the author's inventory back to assigned base pages and records
the model's original inventory and omitted inventory rows in the context receipt. Page
ownership is a local planner fact; repeating its list is not proof that content was authored.
Final review checks actual coverage against all original assigned pages. Citations outside
supplied evidence still hold preparation. The paid output and authored detail stay unchanged.
Local visual diagnostics and saved earlier interpretation receipts remain readable evidence.
AI campaigns create their spine directly from the request and use the same chapter path.

`chapter_parts.py` separates the chapter artifact from a provider response. A local source
token sizing target groups original core pages into useful work units; it is not a request,
output, money or time stopping cap. Shared appendix pages, neighboring page boundaries,
shared identities/future truths and existing record commitments accompany each group.
Within one chapter groups run sequentially to reuse IDs; independent chapters remain parallel.
`chapter-part-v2.md` requests additions rather than repeated chapters, and permits empty
goal/event lists for reference-only work. Exact duplicate records deduplicate; conflicting
records hold for explicit repair. Every owned page needs an explicit completed part receipt;
a citation or a recovered source_pages field is not proof of complete coverage.

If a provider output exhausts its real output window, its original task, raw response
(including whitespace), usage and incomplete status remain unchanged. Only lexically closed,
fully serialized typed records are recovered into a separate provisional artifact. Missing
origins or open strings/records are never filled by defaults. A distinct completion request
receives the original evidence, retained records and unfinished fields, and authors only
missing material. Closed fields of an unfinished identified record remain constraints.
A fully closed valid chapter/part surviving missing outer syntax can be derived locally.
The assembled chapter still passes structural checks and the independent final review.
Continuation with no new content holds rather than repeating paid work. Restart replays
original and completion receipts; explicitly matching saved original chapter inputs can
reuse their earlier prompt receipt without submitting the whole chapter again.

This follows the useful long-form pattern documented by
[Re3](https://arxiv.org/abs/2210.06774), [DOC](https://arxiv.org/abs/2212.10077), and
[LangGraph's section-writing workflow](https://docs.langchain.com/oss/python/langgraph/workflows-agents#orchestrator-worker):
maintain the plan and relevant context, write smaller durable units, then assemble and
check consistency. These sources describe workflows and several-thousand-word stories;
they do not establish that an entire commercial book is reliably generated in one response.

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
Publication indexes these as reviewed semantic records, whose integrity comes from exact
manifest spans, source/passage hashes, audience/reference checks and complete chunk coverage.
They are not raw PDF extraction pages: a private note may legitimately quote several stat
profiles or an incomplete original OCR line. Original page quality diagnostics stay attached
to the source artifacts; independent rulebook/PDF extraction keeps its own layout checks.

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
Before assembly, a local join check detects concrete ID collisions, dangling event links and
missing encounter/reveal references. Valid bundles add no call. New structural repair requests
return sparse, typed field edits identified by chapter, collection, original row index and
expected stable ID. They return full records only for specifically permitted missing NPCs.
Encounter actor edits refer to existing encounter indexes; reveal edits refer to existing
reveal indexes, so accepted descriptions, conditions, quantities and mechanics are copied
locally. A nine-link correction does not require returning four complete chapters.
The original and paid repair receipts are kept. Previously dispatched graph-v2 chapter-shaped
repair responses remain recoverable: uniquely identified permitted rows are projected onto
original records, omitted rows remain unchanged, and only the recomputed residual defects
can trigger a distinct sparse request. Ambiguous identities are never matched by position.
The full assembled candidate still passes scope and semantic checks before acceptance.
The residual audit then checks every typed entity/event/goal/procedure reference together,
including item identities. `structural-links-v2.md` returns only those remaining joins and
explicitly permitted missing entities. An item gets its own canonical clue/object identity;
its carrier or corpse remains an NPC. Original procedures and encounter mechanics stay
unchanged. Human-readable area/section labels mistakenly supplied as OCR region IDs move
losslessly into `origin.note`; only actual region IDs remain in `origin.regions`. The exact
original provider responses and prior sparse repair contract remain available for replay.
The compiler does not depend on a model copying unchanged prose exactly. Previously retained proposals
can be reprocessed after a representation fix without another provider request; original
discovery wording and conditions stay intact even when an invalid reveal becomes guidance.
Ordinary scoped patches likewise preserve existing identities outside the repair closure
when a model incidentally re-describes them. Their ignored IDs are recorded, while their
accepted records remain unchanged. A parenthetical profile heading annotation is checked
as presentation without rewriting the retained stat text or paying to regenerate it.
When a chapter-content correction omits unchanged actors or secrets, the local merge retains
the original records. Omission is never deletion authority; unrelated supplied modifications
still fail validation, and the full paid proposal remains available for inspection.

During play, verified current-scene guidance reaches the existing input parser before it
classifies the action. Existing evidence is annotated inline with stable source-span IDs,
bound to the reviewed source reference, current scene and exact original offsets. The
existing consequence call selects a span ID and supplies a natural player-safe answer.
Local validation resolves that binding without asking the model to retype source words or
join a quotation to a separate reference. Unknown, stale, future and foreign spans fail.
No additional semantic call is needed. Archived responses retain exact quotation matching
and unambiguous relocation within already eligible same-campaign current-scene evidence.
The consequence call also resolves present NPCs' immediate replies to offered plans from
their evidenced motives and knowledge. Narration performs those replies and leaves the
hero's next decision to the player. Dialogue alone does not execute the plan or complete
a goal. Inspecting or discussing a spell and negated conditions do not trigger unsupported
mechanics warnings; an actual declaration later in the same turn still does.
The narrator receives only the selected answer; the public adjudication DTO excludes raw
source evidence, span IDs and private reasoning. Rejections retain private operator replay
receipts with restricted permissions, without failing play if receipt capture is unavailable.
Whether a natural-language action earns a secret remains the live DM's semantic judgment,
as for creative goal completion. Source provenance alone does not prove that judgment correct.
New atomic turn receipts retain that public adjudication alongside the saved response, so
replay preserves it without a model call. Historical receipts remain untouched.
Linked event triggers permit meaningful progress between events at the same location
without requiring artificial physical travel.

Preserving exact source instructions does not implement all their mechanics. Canonical item
custody and ordinary fixed-DC checks compile to existing engine definitions. Attunement,
persistent charge reset/escalation, arbitrary transformations, area damage and unsupported
traits/spells remain explicit capability findings. These execution limits are separate from
source omissions and must not trigger an endless paid prose-repair loop.
Source check policies currently execute the fixed-DC roll only; their private success and
failure text does not itself commit discoveries or other persistent outcomes.

Versioned prompts are in `apps/api/services/campaign_prep/prompts/`: `principles-v2.md`,
`spine-v2.md`, `chapters-v2.md`, `chapter-part-v2.md`, `review-v2.md`, `repair-v2.md`, `continuity-v2.md` and
`structural-joins-v2.md`, `structural-links-v2.md`, plus `read-source-v2.md` (oversized planning evidence only).
`structural-repair-v2.md` is used only to read already dispatched graph-v2 repair receipts;
new structural work uses the sparse joins contract. The obsolete standalone `visual-v2.md`
prompt and `VisualEvidence` dispatch schema were removed. The earlier replacement commit
`2b7fb4d` contains the removal of the superseded blueprint executor and its callers; this
change does not reintroduce it.

Acceptance for the two fresh PDF demonstrations uses independent fixed source-feature
inventories spanning the original adventures. At least 80% must be accurately retained,
with logical motives, continuity and playable choices. Private OCR storage alone does not
count as authored retention. Material contradictions and secret disclosure errors require
scoped correction; small harmless omissions above the threshold do not justify endless
paid refinement. Engine execution support is assessed separately.

The obsolete-structure audit found no additional PostgreSQL tables to remove: fourteen
current application model tables and `alembic_version` account for all fifteen public tables.
Historical campaigns, source files, paid task receipts and checkpoints are protected records,
not unused preparation infrastructure. The remaining archive reader is inert and cannot
dispatch a model task. PDF creation remains part of the shared graph.
Source removal checks retained work before any vector or filesystem deletion. Held,
cancelled, failed and unpublished preparations retain their receipts even without a prepared
bundle or complete metadata. Deleting a campaign can release only its own unused plain
source; unrelated sources and nonempty artifact directories are preserved. Explicit source
deletion also refuses to recursively erase retained work.

Focused regression evidence is in `tests/test_campaign_chapter_calls_v2.py`,
`tests/test_chapter_source_retention.py`, `tests/test_campaign_source_mechanics_v2.py` and the
adapted graph/workflow/mechanics repair tests. `tools/dev_rollout.py` builds all four owned
Docker targets from committed dev via `git archive`, then preserves the existing stack,
absolute campaign bind, PostgreSQL/Qdrant volumes and Redis container. It explicitly ignores
the separately owned `.claude/worktrees/ux-campaign-cards/` checkout, which is neither included
in the build archive nor modified. Rollout and paid-run receipts belong outside Git.
