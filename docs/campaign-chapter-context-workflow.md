# Source-preserving chapter preparation

The current acceptance rule is a coherent, enjoyable adventure based on the PDF or AI
request. Source differences in rooms, treasure, dialogue, optional events, encounter choices
and timing are allowed. The retained original pages are evidence, not a requirement to clone
every detail or meet an exact-fidelity percentage. Review blocks consequential internal
contradictions, broken plot/clue logic, substantial premature secrets and invalid mechanical
bindings. Ordinary DM interpretation and harmless differences are advice. Material changes
to continuity/geography need a concise note so prepared facts form one playable version.
During play the prepared adaptation governs its narrative; source pages fill compatible
gaps, and committed play state takes precedence over both. Earlier source-retention scores
remain useful descriptive measurements and are not publication gates.

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
Final review uses all original assigned pages to assess the prepared adventure's coherence
and meaningful source relationships, while allowing adaptation. Citations outside
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

The evidence hierarchy is explicit in author/reviewer and live DM instructions. A coherent
prepared adaptation governs its narrative; original pages fill compatible gaps. Material
continuity or geography changes need a concise clarification, while harmless inventions
need no exhaustive change log. Committed gameplay and engine state take precedence over
both: a source passage cannot undo a player choice or reset an outcome. Final review uses
source evidence to understand the adventure and identify consequential contradictions or
missing causal links, rather than enforcing exhaustive reproduction.

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
When a chapter-content correction omits unchanged events, goals, actors or secrets, the local
merge retains the original records, procedures and blank unchanged guidance. A partial
correction note layers over the complete earlier reference with explicit correction precedence.
Omission is never deletion authority; unrelated supplied modifications and invented identities
still fail validation. Supplemental page citations preserve planner ownership and must trace to
physical pages supplied in this repair or a retained critic packet whose exact finding is supplied.
A separate normalization receipt records omitted IDs, proposed pages and critic ancestry; the
full original paid proposal remains unchanged and available for inspection.
Event corrections likewise retain omitted encounter actors and earned reveals. An explicit
actor move removes participation at the old event and supplies the binding at the destination;
the merge preserves that move rather than duplicating the actor. Matching actor corrections
and explicit reveal replacements retain the paid wording. Original private running notes
remain beneath an explicit correction layer. A chapter-level correction can add an entry
opportunity without silently removing an existing route; directly targeted event findings
can replace their trigger and timing. Normalization receipts record each retained or moved
binding and private guidance layer before independent recheck.

Small prose contradictions can instead use exact `field_edits` on a chapter's
`source_notes` or event's `dm_notes`. Each replacement must identify one unique accepted
substring within the affected dependency closure. The graph validates the complete batch
before applying it after inventory retention, so a removed false instruction does not
return beneath a correction preface. Unrelated text stays byte-for-byte unchanged; original
text and the exact edit remain in durable task and normalization receipts. Ambiguous,
overlapping, missing and mixed replacement/edit authority require attention rather than
guessing. This avoids returning a large complete chapter to correct one directional clause.

When a paid repair incorrectly encodes an existing chapter-content identity as an event
inside a chapter proposal, the entire malformed chapter proposal is quarantined. Its full
contents, identity conflict and hashes remain in a reconciliation receipt; the accepted
chapter is retained. Independently supplied valid record patches still pass the ordinary
scope and semantic checks. Quarantine alone is not progress and cannot mark the run Ready.
The independent review must resolve any findings that the ignored proposal did not repair.

Repeated appearances of the same individual retain one actor identity and the same exact
authoritative creature profile. A disguise with unchanged statistics does not create a
second creature or a different profile. Authoring and repair prompts distinguish this
reference-consistency problem from genuinely different creatures or mechanical forms.
`profiles.py` folds mechanical declarations into one canonical registry. Each new encounter
contains `actor_id`, `profile_id`, quantity and per-appearance adaptation; the provider's
strict output schema excludes inline creature names and statistics. Chapter declarations
precede their events so output recovery cannot accept references whose only definition
was truncated after the chapter. Known definitions are reused by ID across chapters and
parts. Identical complete profiles deduplicate, and source/catalogue aliases merge only
when the complete verified text agrees. The compiler parses each shared source profile
once and publishes one statistics passage for its actual actors and scenes. Profile
records participate in dependency fingerprints, scoped repair and independent review.
Legacy inline fields remain inert compatibility inputs for retained paid output; the
derived projection never rewrites original task bytes. Existing Ready publications keep
their accepted representation and hashes on read/restart.

Selected original visual pages from the relevant chapter remain available as `map_context`
through whole-chapter authoring, every multipart continuation, scoped repair and final
review. References identify physical pages and immutable source/image hashes; requests
include the actual page images. A later page group therefore does not lose an earlier
chapter map. Additional map evidence does not expand a repair's allowed record IDs.
When using source geography, spatial directions and connections are checked against the
actual map and keyed prose, not inferred from OCR order or room numbers. Coherent map
adaptations are allowed when all prepared guidance agrees. Unclear source relationships
remain explicitly uncertain. This uses existing author/review calls without a separate
universal map agent or an exhaustive generated room-adjacency guide.

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
Preparation translates existing engine check aliases locally, preserving the original source
identifier, DC and procedure. A stat profile that begins directly with size/type receives its
already-declared encounter name as a derived runtime heading; its original bytes and hash
remain available. An existing different creature heading is not silently renamed.

If a paid sparse reference response leaves unresolved links, retain and apply its valid work
before requesting only the residual rows, permitted missing identities and relevant original
pages. Each residual request has a durable signature and receipt. Replaying a rejected older
candidate does not repay the completed full request, and unchanged residuals hold for inspection.

Versioned prompts are in `apps/api/services/campaign_prep/prompts/`: `principles-v2.md`,
`spine-v2.md`, `chapters-v2.md`, `chapter-part-v2.md`, `review-v2.md`, `repair-v2.md`, `continuity-v2.md` and
`structural-joins-v2.md`, `structural-links-v2.md`, plus `read-source-v2.md` (oversized planning evidence only).
`structural-repair-v2.md` is used only to read already dispatched graph-v2 repair receipts;
new structural work uses the sparse joins contract. The obsolete standalone `visual-v2.md`
prompt and `VisualEvidence` dispatch schema were removed. The earlier replacement commit
`2b7fb4d` contains the removal of the superseded blueprint executor and its callers; this
change does not reintroduce it.

Independent fixed source-feature inventories describe retention in the two fresh PDF
demonstrations. The user's later acceptance supersedes the earlier 80% threshold: a
coherent, logical adventure based on the source is acceptable even when details differ.
Private OCR storage alone is not authored retention. Consequential internal contradictions,
broken clue paths and substantial premature secrets require scoped correction; harmless
differences and optional omissions do not block publication. Engine execution support is
assessed separately.

Every final-review packet includes the complete campaign record and chapter encounter
inventory. A chapter-content audit also receives that chapter's complete events, goals,
compiled scenes and encounter bindings. An event outside a packet is not missing from the
campaign. Overview review checks global continuity; it cannot infer local omissions from
partial scene context. Publication-blocking findings require a consequential story,
encounter, procedure or secrecy error. Ordinary low-stakes lead sharing, implicit inspection
of a discovered container, and consulting a preserved usable source map are advice unless
the critic identifies a concrete harmful consequence. Hidden enemies and substantial
unearned truths remain protected.

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

The library presents prepared stories as **Adventures**. Each adventure is reusable and
independent of characters. A **Campaign** is one player's playthrough of an adventure;
sessions, hero assignments, discoveries and outcomes belong to that campaign. Starting
another campaign creates fresh play state without repeating preparation or copying the
previous campaign's outcomes. Continue opens an existing save, and the adventure card lists
the player's campaigns separately. Existing `/campaigns` library links remain supported;
the library's navigation address is `/adventures`.

`POST /api/adventures/{slug}/campaign` accepts `{new_campaign: true, request_id: UUID}`
for explicit creation. The owner-scoped request identity binds the adventure and request
body: a retry returns the same campaign; reuse for a different request is rejected. The
additive migration `23_campaign_creation_requests` retains existing rows. Omitting the
body retains the older continue/get-or-create API behavior for existing clients.

Library title and archive preferences are a separately locked, atomically written
`/data/campaign-library.json` index, changed through the administrator library endpoint.
Renaming or archiving a template never rewrites accepted source material, checkpoints or
saved play. The default library hides archived templates after reconstructing any fallback
cards, while Show archived adventures makes restoration available. Active preparation
cannot be archived. This supports keeping one current visible copy of each adventure
without deleting historical or failed work.

Focused regression evidence is in `tests/test_campaign_chapter_calls_v2.py`,
`tests/test_chapter_source_retention.py`, `tests/test_campaign_source_mechanics_v2.py` and the
adapted graph/workflow/mechanics repair tests. `tools/dev_rollout.py` builds all four owned
Docker targets from committed dev via `git archive`, then preserves the existing stack,
absolute campaign bind, PostgreSQL/Qdrant volumes and Redis container. It explicitly ignores
the separately owned `.claude/worktrees/ux-campaign-cards/` checkout, which is neither included
in the build archive nor modified. Rollout and paid-run receipts belong outside Git.
