# Preparation supervisor integration

`apps/api/services/preparation.py` owns durable task leases, provider budget reservations,
receipt acceptance, retry recovery and the readiness gate. Keep generation and upload entrypoints
thin: create a frozen `PreparationRunBinding`, call `PreparationSupervisor.create_run`, then
schedule `step` until the returned run is ready or blocked. The private checkpoint is stored in
the source's existing `.jobs/<run-id>/preparation.json` directory; public status must come from
`supervisor.status` so private artifact payloads, validator findings and error codes do not leak.

Register one `StageAdapter` for each stage. Its planner receives the frozen binding and all
accepted artifacts; its executor receives `(binding, task, dependency_artifacts, attempt,
idempotency_key)` and returns `StageExecutionResult`. The executor must use the idempotency key,
return exact dependency-bound artifacts, and include `ProviderUsage` for success or failure.
Raise `PreparationTaskFailure(code, usage)` when the provider fails. If usage is unknown, the
supervisor records an unknown-usage attempt and charges the task's full call, token and cost
reservation. Provider SDK retries must be disabled; each supervisor attempt is checkpointed.

Register code validators for each stage as callables in `validators[stage]`; assign each callable
a stable `validator_id` string. A successful validator returns a `ValidationResult` with exactly
one passing receipt for that ID. Each receipt must bind the exact proposed artifact and dependency
digests. P04 requires `p04_prepared_campaign`; P09 requires separate
`p09_deterministic_review` and `p09_independent_critic` validators; P12 requires
`p12_publication_inventory`. Missing adapters or validators block before dispatch. P06 and P08
plans must cover exactly the `act_ids` in P03's `scope_contract`.

Call `step(slug, run_id, worker_id, source_guard, publisher)` repeatedly. `source_guard(binding)`
must confirm that the source version and extraction/PDF digest are still current. It is checked
around worker execution and before publication. Supply `publisher(publication)` only for the final
P12 step; it must commit the exact inventory atomically and idempotently, and return the supplied
`publication.bundle_sha256` only after that commit succeeds. The supervisor marks the run ready
only after that digest is confirmed and all twelve stage plans, tasks, artifacts and receipts
still match the checkpoint.

For explicit recovery use `retry` on a blocked run after correcting its stage contract, or when a
blocked task has attempts remaining. `cancel` fences in-flight workers and charges outstanding
reservations conservatively. `invalidate_dependents` removes an artifact's transitive dependent
closure so amended private work cannot reuse stale downstream approvals.
