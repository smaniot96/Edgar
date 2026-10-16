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
finalization. Application startup calls `recover_private_preparations` for interrupted runs;
it reclaims the stable job and calls `prepare_adventure`, repeating publication idempotently
or finalizing an already-READY checkpoint without provider work.

Every chapter critic receives the complete finished manuscript, canonical cross-act
contracts, and source/hero/mechanical evidence. The provider enforces a conservative
128,000-token per-call input limit; oversized evidence blocks preparation explicitly rather
than silently truncating other chapters. Long multi-call stages renew the exact worker lease
under short lifecycle locks. Cancellation or replacement fences every subsequent semantic
and image subcall; no lock spans provider work.

Setup optionally accepts paired `max_image_calls` and `max_image_cost_microusd` values;
both default to zero. The image allowance is part of `max_cost_microusd`, and semantic
workers receive only the remainder. Commissioning requires a server-owned
`EDGAR_TRUSTED_IMAGE_PRICE_RECORD` matching the frozen illustration model, size and
quality, with a documented revision and per-call cost in micro-USD. No price is inferred.
The exact record and image profile are retained in the commission; a changed or missing
trusted record prevents new paid dispatch. Image reservations use one locked run-level
ledger across all repair and manifest versions, so repairs cannot reset spent allowance.
Required topology maps and clue handouts use deterministic rendering and retained text.

Public status comes from `safe_preparation_status` and contains only fixed progress labels,
safe status values and retry actions. It omits task names, private artifact data, findings,
provider diagnostics, creatures, scenes and manuscript text. `campaign_sources` and
`adventure_manifests` enforce the same readiness gate before exposing a source or manifest.
