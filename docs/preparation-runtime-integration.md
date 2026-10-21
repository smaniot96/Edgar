# Campaign preparation runtime

Edgar has one preparation pipeline for both PDF and generated campaigns. Source type
changes the input retention, not the authoring workflow. `preparation_runtime.py` owns
the lifecycle; `preparation_stages.py` owns the shared stage adapters.

The prepared adventure is character independent. A player may create or choose the
protagonist after preparation, and reuse the adventure in another playthrough. A
specific character ID, class, race, sheet, inventory or current assignment is not a
preparation prerequisite. Prompts describe the player character as the protagonist
without presupposing those traits. Source level recommendations and original
encounter statistics remain campaign information. Character-specific decisions
belong to the playthrough and live rules engine.

Preparation retains the source and studies, accepts a whole-campaign design, then asks
for one compact blueprint. The blueprint contains useful scene notes, source-backed
goals, important identities, geography, encounter requests and optional exact engine
checks. Ordinary conversations, rescues, reactions and creative player solutions are
resolved by the live DM. A goal describes the outcome to earn, rather than prescribing
an exhaustive sequence of actions.

Code compiles canonical records and grounds encounters against their source and books,
assembles scene packets and the private guide, prepares relevant assets, and validates
source/structure. PDF guides retain every original physical source page. Generated
guides use accepted design seeds and compact scene notes. There is no separate per-act
authoring, prose expansion or branch-amendment model workflow.

An independent critic receives the actual retained text, source context, blueprint,
canonical records and mechanical evidence. Review is partitioned when provider context
requires it. P10 independently recomputes a source-scaffold report covering exact
bindings, goal evidence, packets, route topology and actor grounding. It does not
simulate completion of an open story. The historical artifact and validator identifiers
`path_exploration_report` and `p10_path_exploration` remain to resume saved checkpoints;
no exhaustive path search or alternative scripted pipeline is dispatched.

The live consequence planner judges earned goal status from source criteria, actual
player action, established history and engine outcomes. Each accepted goal change has
a private achievement basis and code-owned turn receipt. Combat, rolls, inventory,
travel, terminal progress and secret disclosure retain their engine validation. An
optional authored policy cannot award a milestone merely because it was selected.
The player projection excludes private criteria and planner passages.

Every paid response and proposal is retained before downstream validation. Local
compiler/source failures preserve the latest proposal. Review repairs retain the
accepted source studies, design and blueprint, then rebuild dependent records from
P04. Archived old checkpoints remain readable; old per-act authoring tasks require a
one-time conversion rather than silently dispatching the removed workflow.

New commissions have no selected heroes. Historical hero fields and digests remain
readable to preserve the identity of paid work, but do not restrict character choice
at play. Existing hero-specific authored notes or encounter adjustments are retained
historical content; removing the character gate does not silently rewrite them.

Every new semantic campaign request uses `gpt-6-luna` with `medium` reasoning on the
Responses API, for both PDF and AI campaigns at every complexity. This includes
studies, design, blueprint corrections, and independent text/visual review. Current
model policy also applies when resuming a saved run; its historical profile snapshot
and paid response receipts keep their original identities. Image generation and
embeddings retain their separate model configuration. The OCR/LangGraph alternative
is assessed in `campaign-creation-ocr-langgraph-assessment-2026-10-21.md`; it is not
an additional implemented pipeline.

The earlier AI outline/chapter generator is also removed. Fresh AI requests schedule
this runtime directly; its old module retains archive schemas and a guarded delegate
for saved callers. Legacy non-private generation jobs remain readable and preserve
their files, but require conversion before continuing. Their UI status does not offer
a retry of the retired generator. Existing public PDF source reindexing remains a
low-level source operation.

The P12 publisher verifies the exact accepted extraction, manifest, prepared scaffold,
manuscript and asset inventory. It indexes with attempt/cancellation fencing, then
returns the accepted publication digest. Only that successful publication can produce
READY. `private_preparation_ready` checks the final checkpoint, metadata receipt,
source identities and active index before play. Startup recovery uses the same saved
run and durable provider receipts.

Token/call/cost estimates and repair counts are accounting and log information. The
provider's actual request/context constraints still apply; evidence is measured in
tokens and review input is partitioned instead of repeating source studies. Long model
requests retain durable response identity and renewable worker leases. The local
frontend uses port 8000.

Independent review has one measured semantic batching implementation. It scopes
blueprint/grounding evidence and avoids repeating compiler-expanded joins, ready
asset rows and rulebook search duplicates. Exact chapter text, original artifact
pointers, disclosure predicates and relevant creature statistics remain available.
Preferred request size partitions input and never stops a run or limits spend.

Each paid critic proposal records its exact review-inventory digest and reviewed
text ranges/hashes. Restart loads matching observations, revalidates citations and
assigns only uncovered intervals. Rejected observations retain paid usage without
earning coverage. Usage deduplicates by provider response ID, independently of
coverage; repeated findings are merged and distinct findings keep distinct IDs.
Review uses the normal stage execution/accounting path rather than a P09 wrapper.
