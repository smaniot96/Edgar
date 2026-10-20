"""Runtime lifecycle glue for private, fully reviewed adventure preparation.

The supervisor owns durable stage execution. This module freezes the source, commission,
hero and profile identities, connects the supervisor to retained source/index publication,
and exposes only safe status and readiness projections to the rest of the API.
"""

from __future__ import annotations

import asyncio
import copy
import fcntl
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from edgar_core.preparation import (
    PreparationBudget,
    PreparationJobStatus,
    PreparationPublication,
    PreparationRunBinding,
    PreparationRunState,
    PreparationTaskStatus,
    PublicPreparationStatus,
    SafePreparationFailure,
    canonical_sha256,
    safe_public_stage,
)

from . import adventures_meta as meta_store
from .preparation import PreparationError, PreparationSupervisor
from .preparation_store import PreparationStore, PreparationStoreError, preparation_path
from .safe_errors import classify_error, new_support_ref, safe_failure_metadata

log = structlog.get_logger()

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

_RECOVERY_TASKS: set[asyncio.Task] = set()
_RESTART_PREPARATION_CODES = {
    "required_book_phb_unindexed",
    "required_book_dmg_unindexed",
    "required_book_mm_unindexed",
    "required_books_unavailable",
    "required_book_unavailable",
    "required_books_missing",
    "book_version_unavailable",
    "unsupported_party",
    "unsupported_edition",
    "unsupported_progression",
    "unsupported_hero_capability",
    "budget_exhausted",
    "run_budget_exhausted",
    "attempt_budget_exhausted",
    "provider_budget_exhausted",
    "providerbudget_exhausted",
    "repair_budget_exhausted",
    "required_assets_unavailable",
}
_PREPARATION_FAILURE_MESSAGES = {
    "required_book_phb_unindexed": "The Player's Handbook has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.",
    "required_book_dmg_unindexed": "The DM's Guide has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.",
    "required_book_mm_unindexed": "The Monster Manual has no exact indexed version for this rules edition. Re-ingest that rulebook for this edition, then start a new campaign.",
    "required_books_unavailable": "Required sourcebooks are unavailable for this edition. Choose an available edition or sourcebook and start a new preparation.",
    "required_book_unavailable": "A required sourcebook is unavailable for this edition. Choose an available edition or sourcebook and start a new preparation.",
    "required_books_missing": "Required sourcebooks are missing. Choose an available edition or sourcebook and start a new preparation.",
    "book_version_unavailable": "A required sourcebook version is unavailable. Choose an available edition or sourcebook and start a new preparation.",
    "unsupported_party": "The selected hero party is not supported by this preparation. Choose a supported party and start a new adventure.",
    "unsupported_edition": "This rules edition is not supported for preparation. Choose a supported edition and start a new adventure.",
    "unsupported_progression": "The requested character progression is not supported. Change the target level or party and start a new adventure.",
    "unsupported_hero_capability": "A selected hero capability is not supported by preparation. Choose a different party and start a new adventure.",
    "budget_exhausted": "The preparation exceeded its commissioned budget. Start a new preparation with a larger budget.",
    "run_budget_exhausted": "The preparation exceeded its commissioned budget. Start a new preparation with a larger budget.",
    "attempt_budget_exhausted": "The preparation exceeded its commissioned budget. Start a new preparation with a larger budget.",
    "provider_budget_exhausted": "The preparation exceeded its commissioned provider budget. Start a new preparation with a larger budget.",
    "providerbudget_exhausted": "The preparation exceeded its commissioned provider budget. Start a new preparation with a larger budget.",
    "repair_budget_exhausted": "The adventure needs more repair work than this commission allows. Start a new preparation with a larger repair budget.",
    "required_assets_unavailable": "Required maps or handouts could not be prepared. Change asset requirements and start a new preparation.",
}


def _needs_new_commission(code: str | None) -> bool:
    return code in _RESTART_PREPARATION_CODES or (
        isinstance(code, str) and "budget" in code.lower()
    )


def _preparation_failure_message(code: str | None) -> str:
    if isinstance(code, str):
        return _PREPARATION_FAILURE_MESSAGES.get(
            code,
            "The preparation needs a new commission. Review the party, rules edition, sourcebooks, assets, and budget before starting again."
            if _needs_new_commission(code)
            else "Preparation needs attention. Retry the saved work or start a new adventure.",
        )
    return "Preparation needs attention. Retry the saved work or start a new adventure."


def _setup_failure_code(error: BaseException) -> str:
    """Project an explicit fixed setup code without classifying exception text."""
    code = getattr(error, "code", None)
    if code in {"preparation_profile_invalid", "preparation_source_invalid"}:
        return code
    if isinstance(error, ValueError):
        return "preparation_setup_invalid"
    return classify_error(error).code


def make_preparation_supervisor(
    *, store: PreparationStore | None = None, provider: Any = None, book_client: Any = None
) -> PreparationSupervisor:
    """Build the production stage supervisor, preserving injectable offline seams."""

    from .preparation_stages import make_preparation_supervisor as make_stages_supervisor

    return make_stages_supervisor(store=store, provider=provider, book_client=book_client)


def _has_retryable_task(state: PreparationRunState) -> bool:
    """A FAILED run whose failed task still has attempts left continues in this job."""
    if state.status != PreparationJobStatus.FAILED or state.cancel_requested:
        return False
    limit = state.binding.budget.max_attempts_per_task
    return any(
        task.status == PreparationTaskStatus.FAILED and task.attempt_count < limit
        for task in state.tasks.values()
    )


def _profiles_snapshot(profiles: Any = None) -> tuple[Any, dict[str, Any]]:
    from edgar_core.model_profiles import (
        PREPARATION_STAGES,
        ModelProfileSnapshot,
        effective_profile,
        load_model_profiles,
    )

    snapshot = load_model_profiles() if profiles is None else profiles
    if isinstance(snapshot, Mapping):
        snapshot = ModelProfileSnapshot.from_dict(snapshot)
    if not isinstance(snapshot, ModelProfileSnapshot):
        raise ValueError("Preparation profile snapshot is invalid.")
    # Resolve every role up front. This enforces Sol/high/Responses API without a provider call.
    for stage_name in PREPARATION_STAGES:
        effective_profile(stage_name, snapshot)
    return snapshot, snapshot.to_dict()


def _setup_snapshot(meta: dict) -> dict | None:
    """Return only a server-captured setup; user IDs are never treated as hero snapshots."""

    from ..schemas.preparation_setup import PreparationSetup

    setup = meta.get("preparation_setup")
    if not isinstance(setup, dict) or not isinstance(setup.get("request"), dict):
        return None
    request = PreparationSetup.model_validate(setup["request"])
    if len(set(request.character_ids)) != len(request.character_ids):
        raise ValueError("Captured hero selection contains duplicates.")
    heroes = setup.get("heroes")
    if not isinstance(heroes, list) or not heroes:
        return None
    from edgar_core.grounding import HeroPartySnapshot

    try:
        party = HeroPartySnapshot.model_validate({"heroes": heroes})
    except (TypeError, ValueError) as exc:
        raise ValueError("Captured hero snapshot is invalid.") from exc
    canonical_party = party.model_dump(mode="json")
    ids = [hero.character_id for hero in party.heroes]
    if ids != request.character_ids:
        raise ValueError("Captured hero selection does not match the setup request.")
    normalized_request = request.model_dump()
    for name in ("max_image_calls", "max_image_cost_microusd"):
        if name not in setup["request"]:
            normalized_request.pop(name, None)
    return {"request": normalized_request, "heroes": canonical_party}


def _generation_commission(meta: dict, setup: dict) -> dict[str, Any]:
    generation = meta.get("generation")
    if not isinstance(generation, dict) or generation.get("kind") != "ai":
        raise ValueError("AI preparation commission is missing.")
    allowed = {"kind", "size", "theme", "title_hint", "duration", "starting_level"}
    request = {key: copy.deepcopy(value) for key, value in generation.items() if key in allowed}
    act_counts = {"small": 3, "medium": 6, "large": 12, "gigantic": 24}
    size = request.get("size")
    if size not in act_counts:
        raise ValueError("AI preparation size is invalid.")
    request["act_count"] = act_counts[size]
    request["target_level"] = setup["request"].get("target_level")
    request["setup"] = copy.deepcopy(setup["request"])
    return request


def _binding_for(
    slug: str,
    meta: dict,
    setup: dict,
    profile_data: dict[str, Any],
    run_id: str,
    pdf_path: str | Path | None,
) -> PreparationRunBinding:
    from edgar_core.artifacts import source_digest
    from edgar_core.preparation import canonical_sha256 as digest

    request = setup["request"]
    heroes = setup["heroes"]
    edition = meta.get("rules_edition", "unspecified")
    if edition not in ("2014", "2024", "unspecified"):
        raise ValueError("Rules edition is unsupported for private preparation.")
    mode = "pdf" if pdf_path is not None or meta.get("source_filename") else "ai"
    if mode == "pdf":
        source_dir = _run_source_dir(slug, run_id)
        source_dir.mkdir(parents=True, exist_ok=True)
        retained_pdf = source_dir / f"{slug}.pdf"
        extraction_path = source_dir / f"{slug}.extraction.json"
        if retained_pdf.is_file() and extraction_path.is_file():
            from .preparation_sources import load_retained

            extraction = load_retained(retained_pdf, extraction_path)
        else:
            candidate = (
                Path(pdf_path) if pdf_path is not None else meta_store.source_pdf_path(slug, meta)
            )
            if not candidate.is_file():
                raise ValueError("The retained PDF source is unavailable.")
            from .preparation_sources import retain_pdf

            extraction = retain_pdf(slug, run_id, candidate, edition)
        source_sha = source_digest(retained_pdf)
        extraction_sha = extraction.text_sha256()
        commission = copy.deepcopy(request)
        commission.update({"source_kind": "pdf", "source_sha256": source_sha})
        version = f"pdf-{source_sha[:24]}-{extraction_sha[:16]}"
    else:
        commission = _generation_commission(meta, setup)
        commission.update({"source_kind": "ai", "rules_edition": edition})
        source_sha = None
        extraction_sha = None

    from .adventure_assets import commissioned_image_budget

    image_budget = commissioned_image_budget(
        profile_snapshot=profile_data,
        max_image_calls=request.get("max_image_calls", 0),
        max_image_cost_microusd=request.get("max_image_cost_microusd", 0),
    )
    commission["image_budget"] = image_budget
    if mode == "ai":
        commission_digest = digest(commission)
        version = f"ai-{commission_digest[:40]}"

    budget = PreparationBudget(
        max_provider_calls=request["max_provider_calls"],
        max_total_tokens=request["max_total_tokens"],
        max_cost_microusd=request["max_cost_microusd"] - image_budget["max_image_cost_microusd"],
        max_attempts_per_task=3,
        max_repair_rounds=2,
        max_parallel_tasks=3,
    )
    book_bindings = meta.get("verified_book_bindings") or ()
    if not isinstance(book_bindings, (list, tuple)):
        raise ValueError("Verified book bindings are invalid.")
    commission_sha = digest(commission)
    hero_snapshot = copy.deepcopy(heroes)
    hero_sha = digest(hero_snapshot)
    profile_sha = digest(profile_data)
    return PreparationRunBinding(
        run_id=run_id,
        source_mode=mode,
        source_slug=slug,
        source_version=version,
        source_sha256=source_sha,
        extraction_sha256=extraction_sha,
        manuscript_sha256=None,
        rules_edition=edition,
        commission=commission,
        commission_sha256=commission_sha,
        hero_party_snapshot=hero_snapshot,
        hero_party_sha256=hero_sha,
        profile_snapshot=profile_data,
        profile_snapshot_sha256=profile_sha,
        verified_book_bindings=tuple(copy.deepcopy(book_bindings)),
        budget=budget,
    )


def _asset_image_budget_for_binding(binding: PreparationRunBinding):
    """Validate and expose the image allowance frozen in a run commission."""
    from edgar_core.model_profiles import ModelProfileSnapshot, effective_image_profile

    from .adventure_assets import AssetImageBudget

    request = binding.commission.get("setup", binding.commission)
    frozen = binding.commission.get("image_budget")
    if frozen is None and request.get("max_image_calls", 0) == 0:
        # Existing persisted runs predate optional image commissioning and remain disabled.
        if binding.budget.max_cost_microusd != request.get("max_cost_microusd", 0):
            raise PreparationError("The legacy semantic budget does not match its commission.")
        return AssetImageBudget(max_image_calls=0)
    if not isinstance(frozen, dict):
        raise PreparationError("The run commission has no frozen image budget.")
    enabled = frozen.get("max_image_calls", 0) > 0
    if enabled:
        profile_snapshot = ModelProfileSnapshot.from_dict(binding.profile_snapshot)
        effective = effective_image_profile("image.illustration", profile_snapshot)
        if (
            frozen.get("effective_profile") != effective.to_dict()
            or frozen.get("profile_digest") != profile_snapshot.digest
        ):
            raise PreparationError("The frozen image profile does not match this preparation run.")
    if not enabled and (frozen.get("effective_profile") is not None or frozen.get("profile_digest") is not None):
        raise PreparationError("Disabled image generation cannot carry a paid image profile.")
    if (
        frozen.get("max_image_calls") != request.get("max_image_calls", 0)
        or frozen.get("max_image_cost_microusd") != request.get("max_image_cost_microusd", 0)
        or frozen.get("max_image_cost_microusd", 0) > request.get("max_cost_microusd", 0)
        or binding.budget.max_cost_microusd
        + frozen.get("max_image_cost_microusd", 0)
        != request.get("max_cost_microusd", 0)
    ):
        raise PreparationError("The frozen semantic and image budgets do not match the total commission.")
    return AssetImageBudget.model_validate(
        {key: value for key, value in frozen.items() if key in AssetImageBudget.model_fields}
    )


def _run_source_dir(slug: str, run_id: str) -> Path:
    from .preparation_sources import source_directory

    return source_directory(slug, run_id)


def _job_identity(meta: dict) -> tuple[str, str]:
    job = meta.get("job")
    if not isinstance(job, dict):
        raise ValueError("Preparation job is unavailable.")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not _HEX32.fullmatch(job_id):
        raise ValueError("Preparation job identity is invalid.")
    attempt = meta.get("attempt") or {}
    token = attempt.get("id")
    if not isinstance(token, str) or not _HEX32.fullmatch(token):
        raise ValueError("Preparation attempt is no longer active.")
    return "prep-" + job_id, token


def _write_safe_job(slug: str, token: str, *, stage: str, state: str = "running") -> None:
    with meta_store.attempt_guard(slug, token) as meta:
        job = meta.get("job") or {}
        job.update(state=state, stage=stage, attempt_id=token, updated_at=_now())
        meta["job"] = job
        meta_store.save_meta(slug, meta)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _current_source_matches(slug: str, token: str, binding: PreparationRunBinding) -> bool:
    """Cheap identity guard used immediately before/after every provider and publication call."""

    try:
        meta = meta_store.load_meta(slug)
        if meta.get("private_preparation") is not True:
            return False
        if (meta.get("attempt") or {}).get("id") != token:
            return False
        job = meta.get("job") or {}
        if job.get("attempt_id") != token or job.get("state") == "cancel_requested":
            return False
        if meta.get("rules_edition", "unspecified") != binding.rules_edition:
            return False
        setup = _setup_snapshot(meta)
        if setup is None or canonical_sha256(setup["heroes"]) != binding.hero_party_sha256:
            return False
        if binding.source_mode == "pdf":
            from edgar_core.artifacts import source_digest

            from .preparation_sources import load_retained

            source_dir = _run_source_dir(slug, binding.run_id)
            pdf = source_dir / f"{slug}.pdf"
            report = source_dir / f"{slug}.extraction.json"
            extraction = load_retained(pdf, report)
            if source_digest(pdf) != binding.source_sha256:
                return False
            if extraction.text_sha256() != binding.extraction_sha256:
                return False
            expected_commission = copy.deepcopy(setup["request"])
            expected_commission.update(
                {"source_kind": "pdf", "source_sha256": binding.source_sha256}
            )
            if "image_budget" in binding.commission:
                expected_commission["image_budget"] = copy.deepcopy(binding.commission["image_budget"])
            if canonical_sha256(expected_commission) != binding.commission_sha256:
                return False
        else:
            expected_commission = _generation_commission(meta, setup)
            expected_commission.update(
                {"source_kind": "ai", "rules_edition": binding.rules_edition}
            )
            if "image_budget" in binding.commission:
                expected_commission["image_budget"] = copy.deepcopy(binding.commission["image_budget"])
            if (
                canonical_sha256(expected_commission) != binding.commission_sha256
            ):
                return False
        frozen = meta.get("preparation_binding")
        return not frozen or (
            isinstance(frozen, dict)
            and frozen.get("binding_sha256") == binding.binding_sha256
            and frozen.get("hero_party_sha256") == binding.hero_party_sha256
            and frozen.get("profile_snapshot_sha256") == binding.profile_snapshot_sha256
        )
    except Exception:
        return False


def _find_artifact(publication: PreparationPublication, kind: str) -> dict[str, Any]:
    matches = [
        artifact.payload for artifact in publication.artifacts if artifact.artifact_kind == kind
    ]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise PreparationError(f"Publication requires exactly one {kind} artifact.")
    return matches[0]


def _validated_asset_coverage(value: Any, *, manifest_sha256: str, prepared_sha256: str):
    """Require an exact final-manifest receipt before any source can be indexed."""

    from edgar_core.assets import AssetCoverageReceipt

    try:
        receipt = AssetCoverageReceipt.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise PreparationError("The final asset coverage receipt is invalid.") from exc
    if (
        not receipt.required_ready
        or receipt.manifest_sha256 != manifest_sha256
        or receipt.prepared_sha256 != prepared_sha256
    ):
        raise PreparationError("Required assets are incomplete or bound to another manifest.")
    return receipt


def _make_extraction(slug: str, binding: PreparationRunBinding, payload: dict[str, Any]):
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.quality import check_text

    if payload.get("source") != slug or payload.get("rules_edition") != binding.rules_edition:
        raise PreparationError("Canonical extraction source or edition does not match the run.")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        raise PreparationError("Canonical extraction contains no pages.")
    extracted = []
    for index, page in enumerate(pages, 1):
        if (
            not isinstance(page, dict)
            or page.get("page") != index
            or not isinstance(page.get("text"), str)
            or not isinstance(page.get("method"), str)
        ):
            raise PreparationError("Canonical extraction page inventory is invalid.")
        text = page["text"]
        extracted.append(ExtractedPage(index, text, page["method"], check_text(text)))
    extraction = Extraction(
        slug,
        payload.get("source_sha256", ""),
        len(extracted),
        extracted,
        rules_edition=binding.rules_edition,
    )
    if not _HEX64.fullmatch(extraction.source_sha256):
        raise PreparationError("Canonical extraction source digest is invalid.")
    expected_text_digest = payload.get("extraction_sha256")
    if expected_text_digest is not None and expected_text_digest != extraction.text_sha256():
        raise PreparationError("Canonical extraction text digest is stale.")
    if binding.source_mode == "pdf" and (
        extraction.source_sha256 != binding.source_sha256
        or extraction.text_sha256() != binding.extraction_sha256
    ):
        raise PreparationError("PDF source or extraction changed after run binding.")
    if binding.source_mode == "ai" and extraction.source_sha256 != extraction.text_sha256():
        raise PreparationError(
            "Generated source identity does not match its exact manuscript text."
        )
    # For generated sources, the final exact digest is supplied by this accepted P08/P09
    # extraction and cross-checked against the manifest and retained manuscript artifact.
    return extraction


def _render_private_manuscript(value: Any) -> tuple[str, str]:
    """Render retained chapter packets without dropping their exact prose."""

    from edgar_core.coherence import RetainedManuscript

    try:
        manuscript = RetainedManuscript.model_validate_json(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise PreparationError("The retained manuscript chapter inventory is invalid.") from exc
    guide = "\n\n".join(chapter.text for chapter in manuscript.chapters)
    serialized = json.dumps(
        manuscript.model_dump(mode="json"),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return guide, serialized


@contextmanager
def _publication_lock(slug: str, run_id: str):
    """Serialize publication across workers and API processes for one immutable run."""

    lock_path = preparation_path(slug, run_id).with_name("publication.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publication_candidate_complete(slug: str, publication: PreparationPublication) -> bool:
    candidate = meta_store.load_meta(slug).get("preparation_publication_candidate")
    return (
        isinstance(candidate, dict)
        and candidate.get("run_id") == publication.run_id
        and candidate.get("bundle_sha256") == publication.bundle_sha256
        and candidate.get("artifact_inventory") == publication.artifact_digests
        and candidate.get("publication_complete") is True
        and isinstance(candidate.get("chunk_count"), int)
        and _HEX64.fullmatch(str(candidate.get("index_version", ""))) is not None
        and isinstance(candidate.get("ingestion"), dict)
    )


def _publish_candidate(slug: str, token: str, publication: PreparationPublication) -> str:
    """Serialize exact publication and return idempotently after a completed activation."""

    with _publication_lock(slug, publication.run_id):
        state = PreparationStore().load_run(slug, publication.run_id)
        if state.status not in (PreparationJobStatus.PUBLISHING, PreparationJobStatus.READY):
            raise PreparationError("The preparation run is not in its publication phase.")
        if (
            publication.binding_sha256 != state.binding.binding_sha256
            or publication.artifact_digests
            != {
                artifact.artifact_id: artifact.content_sha256
                for artifact in state.artifacts.values()
            }
        ):
            raise PreparationError("Publication does not match the accepted checkpoint inventory.")
        if not _current_source_matches(slug, token, state.binding):
            raise PreparationError(
                "Source changed or preparation was cancelled before publication."
            )
        if _publication_candidate_complete(slug, publication):
            return publication.bundle_sha256
        return _publish_candidate_locked(slug, token, publication)


def _publish_candidate_locked(slug: str, token: str, publication: PreparationPublication) -> str:
    """Validate and atomically index the exact private bundle, leaving metadata processing."""

    from edgar_core.manifests import AdventureManifest, manifest_digest
    from ingestion.embed import _embed_and_upsert, _prepare_extraction
    from ingestion.manifest import manifest_chunks, validate_spans
    from ingestion.readiness import prepare_report, report_summary, require_ready

    state = PreparationStore().load_run(slug, publication.run_id)
    binding = state.binding
    if state.status != PreparationJobStatus.PUBLISHING:
        raise PreparationError("The preparation run is not in its publication phase.")
    if (
        not publication.run_id.startswith("prep-")
        or publication.binding_sha256 != binding.binding_sha256
    ):
        raise PreparationError("Publication does not match the frozen run binding.")
    if set(publication.artifact_digests) != {
        artifact.artifact_id for artifact in publication.artifacts
    }:
        raise PreparationError("Publication inventory changed before indexing.")
    if not _current_source_matches(slug, token, binding):
        raise PreparationError("Source changed or preparation was cancelled before publication.")

    extraction_payload = _find_artifact(publication, "canonical_extraction")
    manifest_payload = _find_artifact(publication, "canonical_manifest")
    prepared_payload = _find_artifact(publication, "prepared_campaign")
    manuscript_payload = _find_artifact(publication, "retained_manuscript")
    coverage_payload = _find_artifact(publication, "asset_coverage")
    if set(manuscript_payload) < {"manuscript"} or not manuscript_payload.get("manuscript"):
        raise PreparationError("The final retained manuscript is missing.")
    extraction = _make_extraction(slug, binding, extraction_payload)
    manifest_value = manifest_payload.get("manifest")
    if not isinstance(manifest_value, dict):
        raise PreparationError("The final canonical manifest is missing.")
    manifest = AdventureManifest.model_validate(manifest_value)
    validate_spans(extraction, manifest)
    if manifest.rules_edition != binding.rules_edition:
        raise PreparationError("Final manifest edition does not match the frozen run.")
    if manifest.reviewed_sha256 not in (None, manifest_digest(manifest)):
        raise PreparationError("Final manifest contains a stale internal receipt.")
    # A fixed code-owned receipt records deterministic validation plus the independent
    # P09 critic. No artifact/model-supplied reviewer value grants publication authority.
    manifest.reviewed_by = "private-preparation-supervisor"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    from ingestion.manifest import review_manifest

    manifest = review_manifest(
        extraction,
        manifest,
        reviewed_digest=manifest_digest(manifest),
        reviewer="private-preparation-supervisor",
    )
    prepared_value = prepared_payload.get("prepared")
    if not isinstance(prepared_value, dict):
        raise PreparationError("The final prepared campaign contract is missing.")
    from edgar_core.prepared import PreparedCampaign, prepared_digest, validate_prepared_campaign

    prepared_report = validate_prepared_campaign(manifest, prepared_value)
    if not prepared_report.valid:
        raise PreparationError(
            "The final prepared campaign contract failed deterministic validation."
        )
    try:
        prepared_campaign = PreparedCampaign.model_validate_json(json.dumps(prepared_value))
    except (TypeError, ValueError) as exc:
        raise PreparationError("The final prepared campaign contract is invalid.") from exc
    if manifest.prepared is None or prepared_campaign.model_dump(
        mode="json"
    ) != manifest.prepared.model_dump(mode="json"):
        raise PreparationError("The prepared campaign differs from the canonical manifest.")
    _validated_asset_coverage(
        coverage_payload.get("receipt"),
        manifest_sha256=manifest.reviewed_sha256,
        prepared_sha256=prepared_digest(manifest.prepared),
    )
    from .preparation_stages import _validate_assets

    _validate_assets(
        binding,
        publication.artifacts,
        manifest,
        next(a for a in publication.artifacts if a.artifact_kind == "asset_coverage"),
    )
    source_dir = _run_source_dir(slug, binding.run_id)
    # Save the canonical extraction and exact rendered retained manuscript under this
    # immutable run. The source-mode-specific helper retains the original PDF separately.
    from ingestion.extract import save_extraction

    report_chunks = manifest_chunks(extraction, manifest)
    from .preparation_sources import page_budget

    report = prepare_report(
        extraction,
        source_kind="pdf" if binding.source_mode == "pdf" else "generated",
        chunks=report_chunks,
        degraded_page_budget=page_budget(binding.source_mode),
    )
    require_ready(report)
    markdown, _ = save_extraction(extraction, source_dir, readiness=report)
    if binding.source_mode == "ai":
        from .preparation_sources import retain_manuscript

        retain_manuscript(binding, extraction)
    # Ensure the final retained source matches the already validated extraction.
    if not markdown.is_file() or markdown.read_text(encoding="utf-8") != extraction.markdown():
        raise PreparationError("Final manuscript retention did not preserve exact extraction text.")

    manuscript_value = manuscript_payload["manuscript"]
    from edgar_core.coherence import RetainedManuscript

    manuscript = RetainedManuscript.model_validate_json(json.dumps(manuscript_value))
    if manuscript.source_mode != binding.source_mode:
        raise PreparationError("The retained manuscript source mode does not match the run.")
    manuscript_guide, manuscript_json = _render_private_manuscript(manuscript_value)
    if binding.source_mode == "ai" and (
        len(manuscript.chapters) != len(extraction.pages)
        or any(
            not page.text.endswith("\n\n" + chapter.text)
            for page, chapter in zip(extraction.pages, manuscript.chapters, strict=True)
        )
    ):
        raise PreparationError(
            "AI manuscript chapters do not match the final canonical extraction."
        )
    manuscript_path = source_dir / "retained-manuscript.json"
    manuscript_path.write_text(manuscript_json, encoding="utf-8")
    guide_path = source_dir / "private-dm-guide.md"
    guide_path.write_text(manuscript_guide, encoding="utf-8")

    report_holder: dict[str, Any] = {}

    def prepared(result: dict) -> None:
        report_holder.update(result)

    chunks = _prepare_extraction(
        extraction,
        "pdf" if binding.source_mode == "pdf" else "generated",
        1000,
        100,
        None,
        prepared,
        prepared_chunks=report_chunks,
        degraded_page_budget=page_budget(binding.source_mode),
    )
    require_ready(report_holder)
    # Keep candidate metadata private and processing while the exact index is activated.
    candidate = {
        "run_id": publication.run_id,
        "bundle_sha256": publication.bundle_sha256,
        "artifact_inventory": publication.artifact_digests,
        "source_sha256": extraction.source_sha256,
        "extraction_sha256": extraction.text_sha256(),
        "manifest_sha256": manifest.reviewed_sha256,
        "index_version": report_holder.get("index_version"),
        "checkpoint_revision": state.revision,
    }
    with meta_store.attempt_guard(slug, token) as meta:
        if meta.get("private_preparation") is not True:
            raise PreparationError("The source is no longer private.")
        prior = meta.get("preparation_publication_candidate")
        if prior and any(
            prior.get(key) != candidate.get(key)
            for key in (
                "run_id",
                "bundle_sha256",
                "artifact_inventory",
                "source_sha256",
                "extraction_sha256",
            )
        ):
            raise PreparationError("A different candidate bundle already owns publication.")
        meta["preparation_publication_candidate"] = candidate
        meta_store.save_meta(slug, meta)

    def vector_guard():
        return meta_store.publication_guard(slug, token, stage="publishing")

    chunks_count = _embed_and_upsert(
        chunks,
        slug,
        replace=True,
        publication_guard=vector_guard,
    )
    if not _current_source_matches(slug, token, binding):
        raise PreparationError("Source changed or preparation was cancelled during publication.")
    candidate["chunk_count"] = chunks_count
    candidate["ingestion"] = report_summary(report_holder)
    candidate["publication_complete"] = True
    with meta_store.attempt_guard(slug, token) as meta:
        existing = meta.get("preparation_publication_candidate") or {}
        if existing.get("bundle_sha256") != publication.bundle_sha256:
            raise PreparationError("Publication candidate changed during index activation.")
        existing.update(candidate)
        meta["preparation_publication_candidate"] = existing
        meta_store.save_meta(slug, meta)
    return publication.bundle_sha256


def _finalize_ready(slug: str, token: str, state: PreparationRunState) -> None:
    """Promote the exact candidate to metadata READY only after checkpoint READY is durable."""

    if state.status != PreparationJobStatus.READY or not state.published_bundle_sha256:
        raise PreparationError("A durable READY checkpoint is required before finalization.")
    # Read immutable artifacts before taking the lifecycle lock; checkpoint and metadata
    # stores share that lock and nested acquisition could deadlock another API process.
    saved_state = PreparationStore().load_run(slug, state.binding.run_id)
    canonical = _find_artifact_from_state(saved_state, "canonical_manifest")
    prepared = _find_artifact_from_state(saved_state, "prepared_campaign")
    manifest = canonical.get("manifest")
    if not isinstance(manifest, dict):
        raise PreparationError("Final manifest artifact disappeared after publication.")
    from edgar_core.manifests import AdventureManifest, manifest_digest

    reviewed_manifest = AdventureManifest.model_validate(manifest)
    reviewed_digest = manifest_digest(reviewed_manifest)
    if reviewed_manifest.reviewed_sha256 not in (None, reviewed_digest):
        raise PreparationError("Final manifest contains a stale internal receipt.")
    reviewed_manifest.reviewed_by = "private-preparation-supervisor"
    reviewed_manifest.reviewed_sha256 = reviewed_digest
    manifest = reviewed_manifest.model_dump(mode="json")
    with meta_store.attempt_guard(slug, token) as meta:
        candidate = meta.get("preparation_publication_candidate") or {}
        if (
            candidate.get("run_id") != state.binding.run_id
            or candidate.get("bundle_sha256") != state.published_bundle_sha256
            or candidate.get("artifact_inventory") != state.publication_inventory
            or not _HEX64.fullmatch(str(candidate.get("index_version", "")))
        ):
            raise PreparationError("Final metadata receipt does not match the READY checkpoint.")
        receipt = {
            "run_id": state.binding.run_id,
            "bundle_sha256": state.published_bundle_sha256,
            "artifact_inventory": state.publication_inventory,
            "checkpoint_revision": state.revision,
            "binding_sha256": state.binding.binding_sha256,
            "source_binding_sha256": state.binding.source_binding_sha256,
            "hero_party_sha256": state.binding.hero_party_sha256,
            "profile_snapshot_sha256": state.binding.profile_snapshot_sha256,
            "index_version": candidate["index_version"],
        }
        meta.update(
            status="ready",
            chunks=candidate.get("chunk_count"),
            ingestion=candidate.get("ingestion"),
            manifest=manifest,
            prepared_campaign=prepared.get("prepared"),
            prepared_bundle={
                "run_id": state.binding.run_id,
                "bundle_sha256": state.published_bundle_sha256,
                "artifact_inventory": state.publication_inventory,
                "index_version": candidate["index_version"],
            },
            preparation_publication_receipt=receipt,
            error=None,
            failure_code=None,
        )
        job = meta.get("job") or {}
        job.update(
            state="completed",
            stage="completed",
            retained_work="published",
            retryable=False,
            updated_at=_now(),
        )
        job.pop("attempt_id", None)
        meta["job"] = job
        meta.pop("preparation_publication_candidate", None)
        meta_store.save_meta(slug, meta)


def _find_artifact_from_state(state: PreparationRunState, kind: str) -> dict[str, Any]:
    values = [a.payload for a in state.artifacts.values() if a.artifact_kind == kind]
    if len(values) != 1:
        raise PreparationError(f"Accepted run must contain exactly one {kind} artifact.")
    return values[0]


def _private_binding(meta: dict, slug: str) -> tuple[str, PreparationRunBinding | None] | None:
    run_id = meta.get("preparation_run_id")
    if not isinstance(run_id, str):
        job_id = (meta.get("job") or {}).get("job_id")
        if (
            isinstance(job_id, str)
            and _HEX32.fullmatch(job_id)
            and meta.get("failure_code")
            in {
                "preparation_setup_required",
                "preparation_setup_invalid",
                "preparation_profile_invalid",
                "preparation_source_invalid",
            }
        ):
            return "prep-" + job_id, None
        return None
    try:
        state = PreparationStore().load_run(slug, run_id)
    except (PreparationStoreError, ValueError):
        return None
    return run_id, state.binding


def private_preparation_ready(slug: str, meta: dict | None = None) -> bool:
    """Require an exact metadata publication receipt and its durable READY checkpoint."""

    current = meta_store.load_meta(slug) if meta is None else meta
    if current.get("private_preparation") is not True:
        return True
    binding_pair = _private_binding(current, slug)
    if binding_pair is None:
        return False
    run_id, binding = binding_pair
    if binding is None:
        return False
    try:
        state = PreparationStore().load_run(slug, run_id)
    except (PreparationStoreError, ValueError):
        return False
    receipt = current.get("preparation_publication_receipt")
    prepared = current.get("prepared_bundle")
    if not isinstance(receipt, dict) or not isinstance(prepared, dict):
        return False
    inventory = state.publication_inventory
    try:
        extraction = _find_artifact_from_state(state, "canonical_extraction")
        manifest_payload = _find_artifact_from_state(state, "canonical_manifest")
        prepared_payload = _find_artifact_from_state(state, "prepared_campaign")
        manuscript_payload = _find_artifact_from_state(state, "retained_manuscript")
        coverage_payload = _find_artifact_from_state(state, "asset_coverage")
        manifest = manifest_payload.get("manifest")
        prepared_campaign = prepared_payload.get("prepared")
        ingestion = current.get("ingestion") or {}
        if not isinstance(manifest, dict) or not isinstance(prepared_campaign, dict):
            return False
        canonical_extraction = _make_extraction(slug, binding, extraction)
        if (
            ingestion.get("source_sha256") != canonical_extraction.source_sha256
            or ingestion.get("extraction_sha256") != canonical_extraction.text_sha256()
        ):
            return False
        from edgar_core.manifests import AdventureManifest, manifest_digest
        from edgar_core.prepared import (
            PreparedCampaign,
            prepared_digest,
            validate_prepared_campaign,
        )

        final_manifest = AdventureManifest.model_validate(current.get("manifest"))
        accepted_manifest = AdventureManifest.model_validate(manifest)
        if (
            final_manifest.reviewed_sha256 != manifest_digest(final_manifest)
            or final_manifest.reviewed_sha256 != manifest_digest(accepted_manifest)
            or final_manifest.source_sha256 != canonical_extraction.source_sha256
            or final_manifest.extraction_sha256 != canonical_extraction.text_sha256()
            or current.get("prepared_campaign") != prepared_campaign
        ):
            return False
        prepared_report = validate_prepared_campaign(accepted_manifest, prepared_campaign)
        try:
            prepared_value = PreparedCampaign.model_validate_json(json.dumps(prepared_campaign))
        except (TypeError, ValueError):
            return False
        if (
            not prepared_report.valid
            or accepted_manifest.prepared is None
            or prepared_value.model_dump(mode="json")
            != accepted_manifest.prepared.model_dump(mode="json")
        ):
            return False
        _validated_asset_coverage(
            coverage_payload.get("receipt"),
            manifest_sha256=final_manifest.reviewed_sha256,
            prepared_sha256=prepared_digest(accepted_manifest.prepared),
        )
        from .preparation_stages import _validate_assets

        _validate_assets(
            binding,
            tuple(state.artifacts.values()),
            final_manifest,
            next(a for a in state.artifacts.values() if a.artifact_kind == "asset_coverage"),
        )
        manuscript_value = manuscript_payload.get("manuscript")
        manuscript_guide, manuscript_json = _render_private_manuscript(manuscript_value)
        from edgar_core.coherence import RetainedManuscript

        manuscript = RetainedManuscript.model_validate_json(json.dumps(manuscript_value))
        if manuscript.source_mode != binding.source_mode:
            return False
        if binding.source_mode == "ai" and (
            len(manuscript.chapters) != len(canonical_extraction.pages)
            or any(
                not page.text.endswith("\n\n" + chapter.text)
                for page, chapter in zip(
                    canonical_extraction.pages, manuscript.chapters, strict=True
                )
            )
        ):
            return False
        from .preparation_sources import load_retained

        source_dir = _run_source_dir(slug, run_id)
        pdf = source_dir / f"{slug}.pdf" if binding.source_mode == "pdf" else None
        retained = load_retained(pdf, source_dir / f"{slug}.extraction.json")
        if (
            retained.source_sha256 != canonical_extraction.source_sha256
            or retained.text_sha256() != canonical_extraction.text_sha256()
            or retained.rules_edition != binding.rules_edition
            or (source_dir / "retained-manuscript.json").read_text(encoding="utf-8")
            != manuscript_json
            or (source_dir / "private-dm-guide.md").read_text(encoding="utf-8") != manuscript_guide
        ):
            return False
        from db.vector.versions import active_index_version

        active_version = active_index_version(slug)
        if (
            active_version != receipt.get("index_version")
            or ingestion.get("index_version") != active_version
        ):
            return False
    except Exception:
        return False
    exact = (
        state.status == PreparationJobStatus.READY
        and state.published_bundle_sha256 is not None
        and receipt.get("run_id") == run_id == prepared.get("run_id")
        and receipt.get("bundle_sha256")
        == state.published_bundle_sha256
        == prepared.get("bundle_sha256")
        and receipt.get("artifact_inventory") == inventory == prepared.get("artifact_inventory")
        and receipt.get("binding_sha256") == binding.binding_sha256
        and receipt.get("source_binding_sha256") == binding.source_binding_sha256
        and receipt.get("hero_party_sha256") == binding.hero_party_sha256
        and receipt.get("profile_snapshot_sha256") == binding.profile_snapshot_sha256
        and receipt.get("checkpoint_revision") == state.revision
        and receipt.get("index_version") == prepared.get("index_version")
        and current.get("status") == "ready"
    )
    return bool(exact)


def safe_preparation_status(meta: dict, slug: str) -> PublicPreparationStatus | None:
    """Public safe projection; it never includes private labels, counts or findings."""

    if meta.get("private_preparation") is not True:
        return None
    pair = _private_binding(meta, slug)
    run_id = pair[0] if pair else meta.get("preparation_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"prep-[0-9a-f]{32}", run_id):
        return None
    if pair is not None and pair[1] is None:
        failure = _failure_projection(
            meta.get("failure_code") or "preparation_setup_invalid",
            meta.get("support_ref"),
            run_id,
        )
        return PublicPreparationStatus(
            run_id=run_id,
            status=PreparationJobStatus.BLOCKED,
            stage="Preparation needs attention",
            retryable=False,
            action="restart",
            failure=failure,
        )
    try:
        state = PreparationStore().load_run(slug, run_id)
    except (PreparationStoreError, ValueError):
        failure = _failure_projection(
            meta.get("failure_code") or "preparation_failed", meta.get("support_ref"), run_id
        )
        return PublicPreparationStatus(
            run_id=run_id,
            status=PreparationJobStatus.BLOCKED,
            stage="Preparation needs attention",
            retryable=False,
            action="restart",
            failure=failure,
        )
    if _needs_new_commission(state.last_safe_error_code):
        failure = _failure_projection(
            state.last_safe_error_code, state.last_safe_error_ref, run_id
        )
        return PublicPreparationStatus(
            run_id=run_id,
            status=state.status,
            stage=(
                safe_public_stage(state.current_stage)
                if state.current_stage is not None
                else "Preparation needs attention"
            ),
            retryable=False,
            action="restart",
            failure=failure,
        )
    if private_preparation_ready(slug, meta):
        return PublicPreparationStatus(
            run_id=run_id,
            status=PreparationJobStatus.READY,
            stage="Ready to play",
            retryable=False,
            action="none",
        )
    if state.status == PreparationJobStatus.READY:
        return PublicPreparationStatus(
            run_id=run_id,
            status=PreparationJobStatus.PUBLISHING,
            stage="Finalizing your adventure",
            retryable=False,
            action="wait",
        )
    return PreparationSupervisor(adapters={}, validators={}, store=PreparationStore()).status(
        slug, run_id
    )


def _failure_projection(
    code: object, support_ref: object, identity: str
) -> SafePreparationFailure | None:
    if not isinstance(code, str):
        return None
    if not isinstance(support_ref, str) or not re.fullmatch(r"err-[0-9a-f]{16}", support_ref):
        # Legacy records receive a stable reference without modifying them during a read.
        support_ref = "err-" + hashlib.sha256(f"{identity}:{code}".encode()).hexdigest()[:16]
    failure = classify_error(code, support_ref=support_ref)
    return SafePreparationFailure(
        code=failure.code,
        category=failure.category.value,
        message=failure.message,
        recovery_action=failure.recovery_action.value,
        retryable=failure.retryable,
        support_ref=failure.support_ref,
        outcome=failure.outcome.value,
    )


def _mark_setup_blocked(slug: str, token: str, code: str) -> None:
    """A missing setup never reaches supervisor planning or a provider adapter."""

    failure = safe_failure_metadata(code, support_ref=new_support_ref())
    with meta_store.attempt_guard(slug, token) as meta:
        meta.update(
            status="failed",
            **failure,
        )
        job = meta.get("job") or {}
        job.update(state="failed", stage="preparation", retryable=False, updated_at=_now())
        meta["job"] = job
        meta_store.save_meta(slug, meta)


def _sync_terminal_meta(slug: str, token: str, state: PreparationRunState) -> None:
    """Mirror terminal supervisor state using only fixed player-safe lifecycle facts."""

    if state.status == PreparationJobStatus.CANCELLED:
        try:
            meta_store.cancel_attempt(slug, token)
        except Exception:
            pass
        return
    public_stage = (
        safe_public_stage(state.current_stage)
        if state.current_stage is not None
        else "Preparation needs attention"
    )
    retained = "none"
    if any(a.artifact_kind == "retained_manuscript" for a in state.artifacts.values()):
        retained = "complete_manuscript"
    elif state.binding.source_mode == "pdf":
        retained = "source_pdf"
    with meta_store.attempt_guard(slug, token) as meta:
        if meta.get("private_preparation") is not True:
            return
        error_code = state.last_safe_error_code
        safe_failure = safe_failure_metadata(
            error_code or "preparation_failed",
            support_ref=state.last_safe_error_ref or new_support_ref(),
        )
        supervisor_status = PreparationSupervisor(
            adapters={}, validators={}, store=PreparationStore()
        ).status(slug, state.binding.run_id)
        restart_required = _needs_new_commission(error_code)
        if error_code:
            # Safe, correlated stage failure: the code and support reference only, no provider text.
            log.error(
                "preparation_stage_failed",
                stage=public_stage,
                failure_code=safe_failure["failure_code"],
                support_ref=safe_failure["support_ref"],
                run_id=state.binding.run_id,
                slug=slug,
            )
        meta.update(
            status="failed",
            **safe_failure,
        )
        job = meta.get("job") or {}
        job.update(
            state="failed",
            stage=public_stage,
            retryable=not restart_required and supervisor_status.action == "retry",
            retained_work=retained,
            updated_at=_now(),
        )
        meta["job"] = job
        meta_store.save_meta(slug, meta)


def _active_lease_wait_seconds(state: PreparationRunState) -> float | None:
    """Return time to the next current-stage lease expiry when no task is claimable."""

    if state.current_stage is None:
        return None
    plan = state.stage_plans.get(state.current_stage.value)
    if plan is None:
        return None
    active = []
    for task in plan.tasks:
        task_state = state.tasks.get(f"{task.stage.value}:{task.task_id}")
        if task_state is None:
            continue
        if task_state.status.value == "pending":
            return None
        if task_state.status.value in ("leased", "running") and task_state.lease_expires_at:
            try:
                expires = datetime.fromisoformat(task_state.lease_expires_at)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
            except ValueError:
                return None
            active.append(max(0.0, (expires - datetime.now(UTC)).total_seconds()))
    return min(active) if active else None


def _recovery_task_finished(task: asyncio.Task) -> None:
    _RECOVERY_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


async def recover_private_preparations() -> list[str]:
    """Schedule interrupted private runs with valid durable checkpoints for safe resume.

    Call after ``fail_stale_processing`` has cleared the dead attempt and marked its job
    interrupted. Active task leases are left untouched; the supervisor waits for their
    original expiry, then charges reserved usage and advances their fences as usual.
    """

    scheduled: list[str] = []
    store = PreparationStore()
    recoverable = {
        PreparationJobStatus.QUEUED,
        PreparationJobStatus.PREPARING,
        PreparationJobStatus.REVIEWING,
        PreparationJobStatus.REPAIRING,
        PreparationJobStatus.PUBLISHING,
        PreparationJobStatus.READY,
    }
    for slug in sorted(meta_store.metadata_slugs()):
        meta = meta_store.load_meta(slug)
        job = meta.get("job") or {}
        run_id = meta.get("preparation_run_id")
        job_id = job.get("job_id")
        if not isinstance(run_id, str) and isinstance(job_id, str) and _HEX32.fullmatch(job_id):
            # The process can stop after the run checkpoint is durable but before its ID
            # is mirrored into metadata. The job ID is the deterministic run identity.
            candidate_run_id = "prep-" + job_id
            try:
                store.load_run(slug, candidate_run_id)
            except (PreparationStoreError, ValueError):
                pass
            else:
                run_id = candidate_run_id
        if (
            meta.get("private_preparation") is not True
            or job.get("state") != "interrupted"
            or not isinstance(run_id, str)
            or not re.fullmatch(r"prep-[0-9a-f]{32}", run_id)
            or job_id != run_id.removeprefix("prep-")
        ):
            continue
        try:
            state = store.load_run(slug, run_id)
        except (PreparationStoreError, ValueError):
            continue
        if state.status not in recoverable or state.binding.run_id != run_id:
            continue

        # Claim the interrupted metadata record under the same lock used by attempts so
        # concurrent lifespan callers cannot dispatch two new workers for this run.
        with meta_store.lifecycle_lock(slug):
            current = meta_store.load_meta(slug)
            current_job = current.get("job") or {}
            if (
                current.get("private_preparation") is not True
                or current.get("preparation_run_id") not in (None, run_id)
                or current_job.get("state") != "interrupted"
                or (current.get("attempt") or {}).get("id")
            ):
                continue
            token = uuid.uuid4().hex
            current.update(
                status="processing",
                error=None,
                failure_code=None,
                preparation_run_id=run_id,
                attempt={"id": token},
            )
            current_job.update(
                state="queued",
                attempt_id=token,
                retryable=False,
                updated_at=_now(),
            )
            current_job.pop("interruption", None)
            current["job"] = current_job
            meta_store.save_meta(slug, current)

        task = asyncio.create_task(
            prepare_adventure(slug, attempt_token=token),
            name=f"private-preparation-recovery-{slug}",
        )
        _RECOVERY_TASKS.add(task)
        task.add_done_callback(_recovery_task_finished)
        scheduled.append(slug)
    return scheduled


async def prepare_adventure(
    slug: str,
    pdf_path: str | Path | None = None,
    *,
    attempt_token: str | None = None,
    model_profiles: Any = None,
) -> PreparationRunState | None:
    """Run/resume one private preparation job to a durable terminal checkpoint."""

    meta = meta_store.load_meta(slug)
    if meta.get("private_preparation") is not True:
        raise PreparationError("Private preparation is not enabled for this source.")
    try:
        run_id, current_token = _job_identity(meta)
        if attempt_token is not None and attempt_token != current_token:
            return None
        setup = _setup_snapshot(meta)
        if setup is None:
            _mark_setup_blocked(slug, current_token, "preparation_setup_required")
            return None
        _, profile_data = _profiles_snapshot(model_profiles)
        stored = PreparationStore()
        run_path = stored  # keep one store instance for the full operation
        try:
            state = run_path.load_run(slug, run_id)
        except PreparationStoreError:
            binding = _binding_for(slug, meta, setup, profile_data, run_id, pdf_path)
            state = run_path.create_run(binding)
            with meta_store.attempt_guard(slug, current_token) as current:
                current["preparation_run_id"] = run_id
                current["preparation_binding"] = {
                    "binding_sha256": binding.binding_sha256,
                    "source_binding_sha256": binding.source_binding_sha256,
                    "hero_party_sha256": binding.hero_party_sha256,
                    "profile_snapshot_sha256": binding.profile_snapshot_sha256,
                }
                current["profile_snapshot"] = profile_data
                meta_store.save_meta(slug, current)
        else:
            if state.binding.source_slug != slug or state.binding.run_id != run_id:
                raise PreparationError("Saved preparation run binding does not match this job.")
            # A resumed run is frozen; changed live setup/source/profile produces a stale guard,
            # never a newly rebound run under old accepted artifacts.
            with meta_store.attempt_guard(slug, current_token) as current:
                current["preparation_run_id"] = run_id
                current["preparation_binding"] = {
                    "binding_sha256": state.binding.binding_sha256,
                    "source_binding_sha256": state.binding.source_binding_sha256,
                    "hero_party_sha256": state.binding.hero_party_sha256,
                    "profile_snapshot_sha256": state.binding.profile_snapshot_sha256,
                }
                current["profile_snapshot"] = state.binding.profile_snapshot
                meta_store.save_meta(slug, current)
        if not _current_source_matches(slug, current_token, state.binding):
            raise PreparationError("Preparation source or frozen identity changed.")
        _write_safe_job(
            slug,
            current_token,
            stage=safe_public_stage(state.current_stage)
            if state.current_stage is not None
            else "Starting preparation",
        )
        supervisor = make_preparation_supervisor(store=run_path)

        async def publisher(publication: PreparationPublication) -> str:
            return await asyncio.to_thread(_publish_candidate, slug, current_token, publication)

        worker_id = f"prep-{current_token}"
        # Keep each loop iteration bounded and cancellation/restart responsive.
        while True:
            state = run_path.load_run(slug, run_id)
            if state.status == PreparationJobStatus.READY:
                _finalize_ready(slug, current_token, state)
                return state
            if state.status in (
                PreparationJobStatus.BLOCKED,
                PreparationJobStatus.FAILED,
                PreparationJobStatus.CANCELLED,
            ) and not _has_retryable_task(state):
                _sync_terminal_meta(slug, current_token, state)
                return state
            # A failed task with attempts left (a known, retryable failure such as an invalid model
            # answer) is retried by the next supervisor step rather than ending the job.
            result = await supervisor.step(
                slug,
                run_id,
                worker_id,
                lambda binding: _current_source_matches(slug, current_token, binding),
                publisher,
            )
            state = result.state
            if state.status == PreparationJobStatus.READY:
                _finalize_ready(slug, current_token, state)
                return state
            if state.current_stage is not None:
                safe_stage = safe_public_stage(state.current_stage)
                try:
                    _write_safe_job(slug, current_token, stage=safe_stage)
                except Exception:
                    # A newer attempt/cancel fences metadata progress; supervisor source_guard
                    # will stop the stale worker on the next boundary.
                    return state
            lease_wait = _active_lease_wait_seconds(state)
            if lease_wait is not None:
                # Never spin through the bounded iteration budget while a valid lease
                # belongs to a worker that may still be finishing elsewhere.
                await asyncio.sleep(max(0.05, min(lease_wait, 15.0)))
    except PreparationError:
        raise
    except Exception as exc:
        code = _setup_failure_code(exc)
        try:
            token = attempt_token or _job_identity(meta_store.load_meta(slug))[1]
            _mark_setup_blocked(slug, token, code)
        except Exception:
            pass
        return None


def retry_preparation(slug: str) -> tuple[str, str]:
    """Requeue retained private work and return `(run_id, new_attempt_token)` for the router."""

    meta = meta_store.load_meta(slug)
    if meta.get("private_preparation") is not True:
        raise PreparationError("Private preparation is not enabled for this source.")
    run_id = meta.get("preparation_run_id")
    if not isinstance(run_id, str):
        raise PreparationError("No retained preparation run is available to retry.")
    state = PreparationStore().load_run(slug, run_id)
    if _needs_new_commission(state.last_safe_error_code):
        raise PreparationError("This preparation needs a new commission and cannot be retried.")
    supervisor = make_preparation_supervisor()
    supervisor.retry(slug, run_id)
    token = meta_store.begin_attempt(slug, job_attempt=True, job_kind="generation")
    with meta_store.attempt_guard(slug, token) as current:
        current["preparation_run_id"] = run_id
        current["status"] = "processing"
        current["error"] = None
        current["failure_code"] = None
        meta_store.save_meta(slug, current)
    return run_id, token


def cancel_preparation(slug: str) -> PreparationRunState:
    """Fence the durable supervisor run before the metadata cancellation receipt."""

    meta = meta_store.load_meta(slug)
    if meta.get("private_preparation") is not True:
        raise PreparationError("Private preparation is not enabled for this source.")
    run_id = meta.get("preparation_run_id")
    if not isinstance(run_id, str):
        raise PreparationError("No active preparation run exists.")
    store = PreparationStore()
    supervisor = make_preparation_supervisor(store=store)
    cancelled = supervisor.cancel(slug, run_id)
    token = (meta_store.load_meta(slug).get("attempt") or {}).get("id")
    if isinstance(token, str):
        try:
            meta_store.cancel_attempt(slug, token)
        except Exception:
            pass
    return cancelled
