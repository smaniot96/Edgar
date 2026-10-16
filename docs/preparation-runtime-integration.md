# Private preparation runtime integration

`apps/api/services/preparation_runtime.py` is the production lifecycle seam for private
adventure preparation. The upload and AI generation entrypoints call
`await prepare_adventure(slug, pdf_path, attempt_token=token, model_profiles=snapshot)`.
The metadata row must already contain `private_preparation: true`, the non-spoiler
`preparation_setup.request`, and server-captured `preparation_setup.heroes`. If setup is
missing or invalid, the worker records a safe blocked state and never constructs or calls a
provider stage.

The run ID is derived from the stable adventure job ID (`prep-<32 lowercase hex>`). Its
binding freezes the exact PDF and extraction digests or AI commission digest, edition,
hero snapshot, request budget and complete non-secret runtime profile snapshot. Retries reuse
the binding and accepted artifacts; changes to its source or setup fail the source guard.
Profile settings are validated for all preparation stages before the supervisor is created.

The stage factory is `make_preparation_supervisor(*, store=None, provider=None,
book_client=None)`. Offline tests inject a fake provider/store through those arguments. The
P12 publisher consumes exact accepted `canonical_extraction`, `canonical_manifest`,
`prepared_campaign`, `retained_manuscript` and `asset_coverage` artifacts. It validates the
source-bound manifest and prepared contract, persists the run's final extraction/manuscript,
then invokes existing extraction/indexing primitives with an attempt and cancellation fence.
Its metadata candidate stays private and processing while the index activates. The callback
returns only the exact `PreparationPublication.bundle_sha256` after index activation succeeds.

The supervisor writes durable READY only after that callback returns the exact digest. The
runtime then atomically finalizes metadata with the run ID, bundle digest, artifact inventory,
checkpoint revision, index version and frozen binding digests. `private_preparation_ready`
is the read gate for player/session consumers; it requires that metadata receipt and
`prepared_bundle` match each other and the immutable READY checkpoint, the retained
extraction/manifest still match, and the active index version match the receipt. A staged
index is therefore not playable during the crash window before READY or metadata
finalization. Startup recovery should call `prepare_adventure` for interrupted runs; it
repeats publication idempotently or finalizes an already-READY checkpoint without provider
work.

Public status comes from `safe_preparation_status` and contains only fixed progress labels,
safe status values and retry actions. It omits task names, private artifact data, findings,
provider diagnostics, creatures, scenes and manuscript text. `campaign_sources` and
`adventure_manifests` enforce the same readiness gate before exposing a source or manifest.
