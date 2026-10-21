# Preparation supervisor integration

`apps/api/services/preparation.py` owns durable leases, usage accounting, accepted
receipts, recovery and publication. There is one scheduler: PDF and AI inputs share
whole-campaign design and compact blueprint authoring. Later compilation, grounding,
scene/guide construction, repair directions and scaffold checks are local operations.
See `preparation-runtime-integration.md` for the current narrative contract.

Entrypoints create a frozen `PreparationRunBinding`, call
`PreparationSupervisor.create_run`, and schedule `step` until ready or blocked. Private
checkpoints live in `.jobs/<run-id>/preparation.json`. Public status comes from
`supervisor.status` and excludes private content, findings and provider diagnostics.

The commission prepares a reusable adventure without selecting a character. Legacy
hero snapshots/digests remain part of historical checkpoint integrity only; they do
not require the old hero when starting or assigning a playthrough. A new commission
uses an empty hero snapshot and grounds original encounters independently of a party.
New provider submissions always resolve the current campaign policy (`gpt-6-luna`,
`medium`, Responses), including saved runs whose retained profile says Sol/high.
Recovering an existing paid response preserves the actual original model and usage.

Stage executors receive `(binding, task, dependency_artifacts, attempt,
idempotency_key)` and return `StageExecutionResult`. Provider work uses durable
response identity and retains usage on both success and failure. Unique completed
response receipts establish known usage; unresolved estimates do not erase it.
Token/call/cost and retry estimates do not stop otherwise valid preparation.

Validators issue receipts bound to exact proposed and dependency digests. P04 checks
the compiled scaffold; P09 combines deterministic source/structure review with an
independent critic; P10 recomputes source-scaffold coverage; P12 validates publication
inventory. Stable task/artifact identifiers remain compatible with the retained run.
An accepted model response alone cannot mark a campaign ready.

`source_guard(binding)` fences changed sources, cancellation and replacement before
and after execution and before publication. No lifecycle lock spans provider work.
The publisher must return the exact `publication.bundle_sha256` after index activation;
READY follows only when accepted receipts and publication agree.

`retry` resumes the same saved blocked run after its defect is corrected. Review
repairs archive prior downstream artifacts and retain P01–P03 plus the accepted compact
blueprint. They restart local compilation at P04 and require fresh dependent review
and publication receipts. They do not repeat paid studies or design. Historical
per-act authoring checkpoints keep their data and report a conversion diagnostic;
they never dispatch the removed heavy authoring workflow.
