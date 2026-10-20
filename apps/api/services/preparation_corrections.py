"""Repair retained provider work with edits, preserving its provenance and usage."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Literal

from pydantic import Field

from edgar_core.preparation import ProviderUsage, assert_artifact_integrity, canonical_sha256

from .preparation_content import ContentRecord
from .preparation_proposals import proposal_directory
from .preparation_store import _atomic_write


class ProposalEdit(ContentRecord):
    op: Literal["add", "replace", "remove"]
    path: str = Field(min_length=1)
    # JSON encoded as text keeps the edit schema finite and supports any schema field.
    value_json: str


class ProposalCorrection(ContentRecord):
    base_proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    edits: list[ProposalEdit] = Field(min_length=1)


def _reference_feedback(proposal, dependencies):
    """Report independent canonical defects together, before another repair call."""
    canonical = proposal.get("canonical", {})
    earlier = [a.payload["fragment"] for a in dependencies if a.artifact_kind == "canonical_act"]
    sources = [*earlier, canonical]
    passages = {p["passage_id"] for f in sources for p in f.get("passages", [])}
    entities = {e["entity_id"]: e for f in sources for e in f.get("entities", [])}
    statuses = {"discovered", "encountered", "visited", "active", "announced", "completed", "failed",
                "fulfilled", "broken", "advanced", "friendly", "neutral", "hostile", "acquired",
                "consumed", "moved", "used", "changed"}
    messages = []

    def evidence(record, path):
        missing = set(record.get("passage_ids", [])) - passages
        if missing:
            messages.append(f"{path}/passage_ids references non-source evidence {sorted(missing)}. "
                            "Canonical policies/effects/definitions require exact physical source passage IDs. "
                            "Typed DM extensions are retained for scene packets/DM notes; they are not physical citations.")

    for category in ("objects", "objectives", "milestones", "promises", "clocks"):
        for index, definition in enumerate(canonical.get(category, [])):
            path = f"/canonical/{category}/{index}"
            invalid = set(definition.get("statuses", [])) - statuses
            if invalid:
                messages.append(f"{path}/statuses contains unsupported values {sorted(invalid)}. "
                                f"Supported statuses are {sorted(statuses)}; preserve the actual intended result.")
            evidence(definition, path)
    for index, policy in enumerate(canonical.get("resolution_policies", [])):
        path = f"/canonical/resolution_policies/{index}"
        evidence(policy, path)
        for outcome in ("on_automatic", "on_success", "on_failure"):
            for effect_index, effect in enumerate(policy.get(outcome, [])):
                effect_path = f"{path}/{outcome}/{effect_index}"
                evidence(effect, effect_path)
                entity = entities.get(effect.get("target_id"))
                if effect.get("kind") in {"encounter", "relationship"} and entity and entity["kind"] != "actor":
                    messages.append(f"{effect_path} targets {entity['kind']} {entity['entity_id']}; "
                                    "encounter and relationship effects require actors. Model trap costs using "
                                    "a declared environment object and operation supported by the same source.")
    return [{"type": "CanonicalReferenceError", "message": m} for m in messages]


def apply_correction(proposal, correction, diagnostics):
    if canonical_sha256(proposal) != correction.base_proposal_sha256:
        raise ValueError("Correction is not bound to the retained proposal.")
    result = deepcopy(proposal)
    messages = " ".join(d.get("message", "") for d in diagnostics).lower()
    protected = {"act_id", "scene_id", "entity_id", "canonical_id", "policy_id", "quest_id",
                 "branch_id", "outcome_id", "extension_id", "route_id", "passage_id"}
    protected |= {name for name in ("check", "dc", "quote", "page", "occurrence")
                  if name not in messages}
    original_fields = _protected_fields(proposal, protected)
    for edit in correction.edits:
        if not edit.path.startswith("/"):
            raise ValueError("Corrections must address a field, never replace the entire proposal.")
        parts = edit.path[1:].split("/")
        if len(parts) < 3:
            raise ValueError("Corrections must edit individual records or fields, not whole sections.")
        if any("~" in part.replace("~1", "").replace("~0", "") for part in parts):
            raise ValueError("Invalid JSON pointer escape.")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in parts]
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, list):
                parent = parent[_index(part, len(parent), append=False)]
            elif isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                raise ValueError("Correction parent does not exist.")
        name = parts[-1]
        value = json.loads(edit.value_json) if edit.op != "remove" else None
        if isinstance(parent, list):
            index = _index(name, len(parent), append=edit.op == "add")
            if edit.op == "add":
                parent.insert(index, value)
            elif edit.op == "replace":
                parent[index] = value
            else:
                parent.pop(index)
        elif isinstance(parent, dict):
            if edit.op != "add" and name not in parent:
                raise ValueError("Correction target does not exist.")
            if edit.op == "remove":
                del parent[name]
            else:
                parent[name] = value
        else:
            raise ValueError("Correction target must have an object or array parent.")
    corrected_fields = _protected_fields(result, protected)
    if any(corrected_fields.get(path) != value for path, value in original_fields.items()):
        raise ValueError("Correction changed an existing identity or an unreported source/check field.")
    return result


def _protected_fields(value, protected, path=()):
    fields = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key in protected:
                fields[path + (key,)] = child
            fields.update(_protected_fields(child, protected, path + (key,)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            fields.update(_protected_fields(child, protected, path + (index,)))
    return fields


def _index(value, length, *, append):
    if value == "-" and append:
        return length
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("Correction array index is invalid.")
    index = int(value)
    if index >= length + int(append):
        raise ValueError("Correction array index is out of range.")
    return index


async def correct_retained(provider, binding, task, schema, feedback, *, instruction,
                           evidence, idempotency_key, dependencies, reuse_valid=False):
    from .preparation import PreparationTaskFailure
    from .preparation_proposals import latest_rejection, reject_proposal, save_proposal
    from .preparation_stages import _usage_sum

    proposal = feedback["proposal"]
    if set(task.dependency_artifact_ids) != {a.artifact_id for a in dependencies}:
        raise PreparationTaskFailure("retained_dependencies_invalid")
    for artifact in dependencies:
        assert_artifact_integrity(binding, artifact)
    diagnostics = [*feedback["diagnostics"], *_reference_feedback(proposal, dependencies)]
    original_usage = ProviderUsage.model_validate_json(json.dumps(feedback["usage"]))
    # Only exact, accounted provider receipts can satisfy a provider-backed task.
    if not (original_usage.provider_called and original_usage.usage_known
            and original_usage.responses and original_usage.requested_model == "gpt-6.1-sol"
            and original_usage.reasoning_effort == "high"):
        raise PreparationTaskFailure("retained_usage_invalid", usage=original_usage)
    if reuse_valid:
        try:
            return schema.model_validate(proposal), original_usage.model_copy(update={"failure_code": None})
        except ValueError:
            pass
    base_digest = canonical_sha256(proposal)
    diagnostics_digest = canonical_sha256(diagnostics)
    logical, separator, attempt = idempotency_key.partition(":attempt:")
    patch_key = logical + ":edit:" + base_digest + ":" + diagnostics_digest
    if separator:
        patch_key += separator + attempt
    patch_feedback = latest_rejection(binding, task, ProposalCorrection, patch_key)
    # Supply the saved proposal, schema and relevant task context once. Do not resend
    # the full rulebook dossier or require a second copy of the entire authored act.
    context = {name: evidence[name] for name in (
        "commission", "party", "whole_design", "act", "accepted_registry", "supported_mechanics"
    ) if name in evidence}
    if any("quote" in d.get("message", "").lower() for d in diagnostics):
        context["exact_pages"] = evidence.get("exact_pages", [])
    correction, patch_usage = await provider(
        binding, task, ProposalCorrection,
        instruction=(
            "Repair only the reported defects in this saved campaign proposal. Return JSON pointer edits, "
            "not the complete proposal. Preserve all unaffected content, IDs, source quotes, checks, DCs "
            "and intended consequences. Add narrowly required supporting records when necessary. "
            "Paths address the current proposal; edits apply in order. value_json is the exact JSON "
            "encoding of the new value (use null for a remove). Copy base_proposal_sha256 exactly. "
            "Do not weaken validation, remove required content, or fabricate citations/stats. "
            "Encounter/relationship effects target ACTOR entities, never hazards. Trap costs can use "
            "source-backed environment effects on a declared object with a supported operation; "
            "trap entities remain hazards and their separate saving throws remain in the DM notes. "
            "Cost failures need clock/consume/environment effects. Partial failures mean actual partial "
            "progress, not an arbitrary way to bypass cost validation. Keep reveal gates safe."
        ),
        evidence={"base_proposal_sha256": base_digest, "previous_proposal": proposal,
                  "code_validation_feedback": diagnostics,
                  "previous_invalid_patch": patch_feedback["proposal"] if patch_feedback else None,
                  "patch_validation_feedback": patch_feedback["diagnostics"] if patch_feedback else [],
                  "required_proposal_schema": schema.model_json_schema(), "task_context": context},
        idempotency_key=patch_key,
    )
    usage = _usage_sum([original_usage, patch_usage])
    try:
        patched = apply_correction(proposal, correction, diagnostics)
    except ValueError as exc:
        retained = save_proposal(binding, task, schema, proposal, usage, idempotency_key)
        reject_proposal(*retained, exc)
        raise PreparationTaskFailure("provider_output_invalid", usage=usage.model_copy(
            update={"failure_code": "provider_output_invalid"})) from exc
    path, record = save_proposal(binding, task, schema, patched, usage, idempotency_key)
    _atomic_write(proposal_directory(binding, task) / "repairs" / (canonical_sha256(patch_key) + ".json"), {
        "binding_sha256": binding.binding_sha256, "task": task.model_dump(mode="json"),
        "dependency_digests": {a.artifact_id: a.content_sha256 for a in dependencies},
        "base_proposal_sha256": base_digest, "result_proposal_sha256": canonical_sha256(patched),
        "diagnostics_sha256": diagnostics_digest, "version": "retained-json-edits-v1",
        "correction": correction.model_dump(mode="json"), "usage": usage.model_dump(mode="json"),
    })
    try:
        return schema.model_validate(patched), usage
    except ValueError as exc:
        reject_proposal(path, record, exc)
        raise PreparationTaskFailure("provider_output_invalid", usage=usage.model_copy(
            update={"failure_code": "provider_output_invalid"})) from exc
