"""Private, version-bound campaign backbone and deterministic validation helpers.

Prepared records add structure around an existing :class:`AdventureManifest`; they do not
own source text, session progress, or a second narrative ledger. All canonical references
point back to scene/entity/passage/objective/milestone identities in that manifest.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from edgar_core.artifacts import text_digest
from edgar_core.identifiers import Digest, Identifier

if TYPE_CHECKING:
    from edgar_core.manifests import AdventureManifest


class PreparedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PreparedProvenance(PreparedRecord):
    """Internal provenance; source citations remain exact manifest passages."""

    origin: Literal["source", "dm_extension"]
    passage_ids: list[Identifier] = Field(default_factory=list, max_length=32)
    rationale: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def valid_origin(self):
        if self.origin == "source" and not self.passage_ids:
            raise ValueError("Source-authored preparation needs exact source passage IDs.")
        if self.origin == "dm_extension" and not self.rationale.strip():
            raise ValueError("DM extensions need an internal rationale.")
        if len(set(self.passage_ids)) != len(self.passage_ids):
            raise ValueError("Provenance cannot repeat a passage ID.")
        return self


class PreparedAct(PreparedRecord):
    act_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    order: int = Field(ge=1, le=100)
    scene_ids: list[Identifier] = Field(min_length=1, max_length=200)
    provenance: PreparedProvenance


class PreparedQuest(PreparedRecord):
    quest_id: Identifier
    objective_id: Identifier
    milestone_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    scene_ids: list[Identifier] = Field(min_length=1, max_length=200)
    prerequisites: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    # The author states which materially different approaches this quest supports.
    required_approaches: list[
        Literal["negotiation", "combat", "avoidance", "failure", "partial_success", "unexpected"]
    ] = Field(min_length=2, max_length=6)
    provenance: PreparedProvenance


class PreparedScenePacket(PreparedRecord):
    scene_id: Identifier
    entry_requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    passage_ids: list[Identifier] = Field(min_length=1, max_length=100)
    entity_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    objective_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    milestone_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    policy_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    dm_notes: str = Field(min_length=1, max_length=12000)
    provenance: PreparedProvenance


class PreparedRoute(PreparedRecord):
    from_scene_id: Identifier
    to_scene_id: Identifier
    visibility: Literal["known", "private"]
    # Both maps use exact WorldFlag key/value predicates read by the runtime.
    traversal_requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    reveal_requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    provenance: PreparedProvenance

    @model_validator(mode="after")
    def private_routes_have_a_gate(self):
        if self.visibility == "private" and not self.reveal_requires:
            raise ValueError("Private routes need a concrete reveal predicate.")
        return self


class PreparedExtension(PreparedRecord):
    """DM-written exact text kept separate from physical source spans/pages."""

    extension_id: Identifier
    passage_id: Identifier
    scene_ids: list[Identifier] = Field(min_length=1, max_length=20)
    entity_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    audience: Literal["player", "planner"]
    requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    text: str = Field(min_length=1, max_length=12000)
    provenance: PreparedProvenance

    @model_validator(mode="after")
    def extension_is_provenanced_as_extension(self):
        if self.provenance.origin != "dm_extension":
            raise ValueError("Prepared extension text must be labelled as a DM extension.")
        return self


class PreparedBranch(PreparedRecord):
    branch_id: Identifier
    quest_id: Identifier
    scene_id: Identifier
    approach: Literal["negotiation", "combat", "avoidance", "failure", "partial_success", "unexpected"]
    action: str = Field(min_length=1, max_length=500)
    requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    policy_ids: list[Identifier] = Field(min_length=1, max_length=50)
    outcome_ids: list[Identifier] = Field(min_length=2, max_length=50)
    provenance: PreparedProvenance


class PreparedPolicySelection(PreparedRecord):
    policy_id: Identifier
    result: Literal["automatic", "success", "failure"]


class PreparedOutcome(PreparedRecord):
    outcome_id: Identifier
    branch_id: Identifier
    result: Literal["success", "partial", "failure"]
    policy_selections: list[PreparedPolicySelection] = Field(min_length=1, max_length=50)
    ending_entity_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    requires: dict[Identifier, str] = Field(default_factory=dict, max_length=32)
    # Compatibility is an explicit, checked claim about coexisting durable outcomes.
    compatible_with: list[Identifier] = Field(default_factory=list, max_length=100)
    provenance: PreparedProvenance


class PreparedCampaign(PreparedRecord):
    """Canonical private campaign backbone. No readiness/reviewer field is accepted."""

    schema_version: Literal[1] = 1
    source_mode: Literal["pdf", "ai"]
    acts: list[PreparedAct] = Field(min_length=1, max_length=100)
    quests: list[PreparedQuest] = Field(min_length=1, max_length=500)
    scene_packets: list[PreparedScenePacket] = Field(min_length=1, max_length=1000)
    routes: list[PreparedRoute] = Field(default_factory=list, max_length=4000)
    extensions: list[PreparedExtension] = Field(default_factory=list, max_length=1000)
    branches: list[PreparedBranch] = Field(min_length=1, max_length=4000)
    outcomes: list[PreparedOutcome] = Field(min_length=1, max_length=8000)
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    amendment_policy: Literal["bounded_additive_preserve_reveals"] = "bounded_additive_preserve_reveals"


class PreparedFinding(PreparedRecord):
    code: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=1000)


class PreparedValidationReport(PreparedRecord):
    prepared_sha256: Digest | None = None
    findings: list[PreparedFinding] = Field(default_factory=list, max_length=1000)

    @property
    def valid(self) -> bool:
        """Acceptance is computed from code-owned findings, never supplied by a producer."""
        return not self.findings


class PreparedAmendment(PreparedRecord):
    amendment_id: Identifier
    base_prepared_sha256: Digest
    branches: list[PreparedBranch] = Field(default_factory=list, max_length=8)
    outcomes: list[PreparedOutcome] = Field(default_factory=list, max_length=16)
    extensions: list[PreparedExtension] = Field(default_factory=list, max_length=8)


class CommittedPreparedFact(PreparedRecord):
    """Small caller-supplied snapshot of facts that an amendment must preserve."""

    kind: Literal["world_flag", "passage", "entity", "scene", "objective", "milestone", "clock", "object"]
    target_id: Identifier
    status: str = Field(min_length=1, max_length=80)
    value: str | None = Field(default=None, max_length=500)
    text_sha256: Digest | None = None
    passage_ids: list[Identifier] = Field(default_factory=list, max_length=32)
    irreversible: bool = False


class PreparedAmendmentResult(PreparedRecord):
    report: PreparedValidationReport
    amended: PreparedCampaign | None = None

    @property
    def valid(self) -> bool:
        return self.report.valid and self.amended is not None


_APPROACHES = {"negotiation", "combat", "avoidance", "failure", "partial_success", "unexpected"}
_TERMINAL_CONFLICTS = {
    frozenset({"completed", "failed"}),
    frozenset({"friendly", "hostile"}),
    frozenset({"peaceful", "ruined"}),
    frozenset({"rescued", "lost"}),
}


def prepared_digest(prepared: PreparedCampaign | dict) -> str:
    """Canonical SHA256 for exact private prepared content and stable references."""
    value = prepared if isinstance(prepared, PreparedCampaign) else PreparedCampaign.model_validate(prepared)
    encoded = json.dumps(value.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _finding(code: str, path: str, message: str) -> PreparedFinding:
    return PreparedFinding(code=code, path=path, message=message)


def _policy_effects(policy, result: str):
    if result == "automatic":
        return policy.on_automatic
    return policy.on_success if result == "success" else policy.on_failure


def _predicate_conflicts(left: dict[str, str], right: dict[str, str]) -> bool:
    return any(key in right and right[key] != value for key, value in left.items())


def _effect_conflict(left: list, right: list) -> bool:
    for first in left:
        for second in right:
            if first.kind == second.kind and first.target_id == second.target_id:
                if first.status != second.status and frozenset({first.status, second.status}) in _TERMINAL_CONFLICTS:
                    return True
                if first.kind == "discover" and first.passage_ids != second.passage_ids:
                    return True
    return False


def _selected_effects(outcome: PreparedOutcome, policies: dict[str, object]) -> list:
    return [
        effect
        for selection in outcome.policy_selections
        for effect in _policy_effects(policies[selection.policy_id], selection.result)
    ]


def validate_prepared_campaign(
    manifest: AdventureManifest | dict,
    prepared: PreparedCampaign | dict | None = None,
) -> PreparedValidationReport:
    """Validate a typed PDF/AI candidate against the existing canonical manifest.

    This is deterministic structural acceptance, not semantic review. Callers must retain
    the separate campaign critic and source/hero/artifact validation receipts.
    """
    from edgar_core.manifests import AdventureManifest

    findings: list[PreparedFinding] = []
    try:
        canonical = manifest if isinstance(manifest, AdventureManifest) else AdventureManifest.model_validate(manifest)
        candidate_value = prepared if prepared is not None else canonical.prepared
        candidate = candidate_value if isinstance(candidate_value, PreparedCampaign) else PreparedCampaign.model_validate(candidate_value)
    except (TypeError, ValueError) as exc:
        return PreparedValidationReport(findings=[_finding("invalid_schema", "$", str(exc)[:1000])])

    try:
        digest = prepared_digest(candidate)
    except (TypeError, ValueError):
        digest = None
    scene_ids = {scene.scene_id for scene in canonical.scenes}
    entity_by_id = {entity.entity_id: entity for entity in canonical.entities}
    passage_by_id = {passage.passage_id: passage for passage in canonical.passages}
    objective_by_id = {item.canonical_id: item for item in canonical.objectives}
    milestone_by_id = {item.canonical_id: item for item in canonical.milestones}
    policy_by_id = {item.policy_id: item for item in canonical.resolution_policies}
    expected_source = "pdf" if canonical.source.lower().endswith(".pdf") else None
    if expected_source and candidate.source_mode != expected_source:
        findings.append(_finding("source_mode_mismatch", "source_mode", "Prepared source mode conflicts with the retained PDF source."))

    act_ids = [item.act_id for item in candidate.acts]
    quest_ids = [item.quest_id for item in candidate.quests]
    packet_ids = [item.scene_id for item in candidate.scene_packets]
    route_keys = [(item.from_scene_id, item.to_scene_id) for item in candidate.routes]
    extension_ids = [item.extension_id for item in candidate.extensions]
    extension_passages = [item.passage_id for item in candidate.extensions]
    branch_ids = [item.branch_id for item in candidate.branches]
    outcome_ids = [item.outcome_id for item in candidate.outcomes]
    for label, values in (
        ("act", act_ids), ("quest", quest_ids), ("scene packet", packet_ids),
        ("route", route_keys), ("extension", extension_ids),
        ("extension passage", extension_passages), ("branch", branch_ids), ("outcome", outcome_ids),
    ):
        if len(values) != len(set(values)):
            findings.append(_finding("duplicate_identity", label, f"Prepared {label} identities must be unique."))

    if sorted(act.order for act in candidate.acts) != list(range(1, len(candidate.acts) + 1)):
        findings.append(_finding("act_order", "acts", "Acts must have one unique contiguous order independent of character level."))
    assigned_scenes = [scene_id for act in candidate.acts for scene_id in act.scene_ids]
    if set(assigned_scenes) != scene_ids or len(assigned_scenes) != len(set(assigned_scenes)):
        findings.append(_finding("act_coverage", "acts", "Every canonical scene must belong to exactly one act."))
    if set(packet_ids) != scene_ids:
        findings.append(_finding("scene_packet_coverage", "scene_packets", "Every canonical scene needs exactly one runnable scene packet."))

    objective_refs = [quest.objective_id for quest in candidate.quests]
    milestone_refs = [mid for quest in candidate.quests for mid in quest.milestone_ids]
    if set(objective_refs) != set(objective_by_id) or len(objective_refs) != len(set(objective_refs)):
        findings.append(_finding("quest_objective_coverage", "quests", "Every canonical objective must belong to exactly one quest."))
    if set(milestone_refs) != set(milestone_by_id) or len(milestone_refs) != len(set(milestone_refs)):
        findings.append(_finding("quest_milestone_coverage", "quests", "Every canonical milestone must belong to exactly one quest."))

    manifest_routes = {
        (scene.scene_id, link.scene_id): link.requires
        for scene in canonical.scenes
        for link in scene.links
    }
    prepared_routes = {(route.from_scene_id, route.to_scene_id): route for route in candidate.routes}
    if set(manifest_routes) != set(prepared_routes):
        findings.append(_finding("topology_coverage", "routes", "Prepared topology must classify every and only reviewed manifest route."))
    for key, route in prepared_routes.items():
        if key not in manifest_routes:
            continue
        if route.traversal_requires != manifest_routes[key]:
            findings.append(_finding("route_predicate_mismatch", f"routes.{key[0]}.{key[1]}", "Traversal predicates must equal the route conditions read by the runtime WorldFlag evaluator."))
        if route.from_scene_id not in scene_ids or route.to_scene_id not in scene_ids:
            findings.append(_finding("unknown_route_scene", f"routes.{key}", "Topology names an unknown canonical scene."))

    # A predicate is meaningful only when a production runtime WorldFlag contract reads it.
    runtime_flag_keys = {
        key
        for scene in canonical.scenes for link in scene.links for key in link.requires
    } | {
        key
        for policy in canonical.resolution_policies for key in policy.requires
    } | {
        key
        for group in (canonical.objects, canonical.objectives, canonical.milestones, canonical.promises, canonical.clocks)
        for item in group for key in item.requires
    }
    for index, route in enumerate(candidate.routes):
        for group_name, predicates in (("traversal_requires", route.traversal_requires), ("reveal_requires", route.reveal_requires)):
            if set(predicates) - runtime_flag_keys:
                findings.append(_finding("unbacked_world_flag", f"routes[{index}].{group_name}", "Route predicates must name WorldFlag keys already consumed by a reviewed runtime route or policy."))

    for index, packet in enumerate(candidate.scene_packets):
        if packet.scene_id not in scene_ids:
            findings.append(_finding("unknown_packet_scene", f"scene_packets[{index}].scene_id", "Scene packet names an unknown scene."))
        if not packet.passage_ids or any(pid not in passage_by_id and pid not in set(extension_passages) for pid in packet.passage_ids):
            findings.append(_finding("unknown_packet_passage", f"scene_packets[{index}].passage_ids", "Scene packet must reference retained source passages or typed DM extensions."))
        for pid in packet.passage_ids:
            source_passage = passage_by_id.get(pid)
            extension = next((row for row in candidate.extensions if row.passage_id == pid), None)
            if source_passage and packet.scene_id not in source_passage.scene_ids:
                findings.append(_finding("packet_passage_scene_mismatch", f"scene_packets[{index}].passage_ids", "Source passage is not scoped to this scene."))
            if extension and packet.scene_id not in extension.scene_ids:
                findings.append(_finding("packet_extension_scene_mismatch", f"scene_packets[{index}].passage_ids", "DM extension is not scoped to this scene."))
        if set(packet.entity_ids) - set(entity_by_id):
            findings.append(_finding("unknown_packet_entity", f"scene_packets[{index}].entity_ids", "Scene packet names an unknown entity."))
        for policy_id in packet.policy_ids:
            if policy_id not in policy_by_id or packet.scene_id not in policy_by_id[policy_id].scene_ids:
                findings.append(_finding("unknown_packet_policy", f"scene_packets[{index}].policy_ids", "Scene packet policy must be reviewed for the same scene."))
        if set(packet.objective_ids) - set(objective_by_id) or set(packet.milestone_ids) - set(milestone_by_id):
            findings.append(_finding("unknown_packet_goal", f"scene_packets[{index}]", "Scene packet names an unknown objective or milestone."))

    for index, extension in enumerate(candidate.extensions):
        if extension.passage_id in passage_by_id:
            findings.append(_finding("extension_reuses_source_passage", f"extensions[{index}].passage_id", "DM extension passage IDs must remain distinct from physical source spans."))
        if set(extension.scene_ids) - scene_ids or set(extension.entity_ids) - set(entity_by_id):
            findings.append(_finding("unknown_extension_reference", f"extensions[{index}]", "DM extension must reference existing canonical scenes and entities."))
        if set(extension.requires) - runtime_flag_keys:
            findings.append(_finding("unbacked_world_flag", f"extensions[{index}].requires", "Extension reveal predicates must use runtime WorldFlag keys."))

    quest_by_id = {quest.quest_id: quest for quest in candidate.quests}
    branches_by_quest: dict[str, list[PreparedBranch]] = {qid: [] for qid in quest_by_id}
    branch_by_id = {branch.branch_id: branch for branch in candidate.branches}
    for index, quest in enumerate(candidate.quests):
        if quest.objective_id not in objective_by_id or set(quest.milestone_ids) - set(milestone_by_id):
            findings.append(_finding("unknown_quest_definition", f"quests[{index}]", "Quest must reuse existing objective and milestone IDs."))
        if set(quest.scene_ids) - scene_ids:
            findings.append(_finding("unknown_quest_scene", f"quests[{index}].scene_ids", "Quest names an unknown scene."))
        if set(quest.prerequisites) - runtime_flag_keys:
            findings.append(_finding("unbacked_world_flag", f"quests[{index}].prerequisites", "Quest prerequisites must use runtime WorldFlag keys."))
    for index, branch in enumerate(candidate.branches):
        if branch.quest_id not in quest_by_id:
            findings.append(_finding("unknown_branch_quest", f"branches[{index}].quest_id", "Branch names an unknown quest."))
        else:
            branches_by_quest[branch.quest_id].append(branch)
            quest = quest_by_id[branch.quest_id]
            if branch.scene_id not in quest.scene_ids:
                findings.append(_finding("branch_outside_quest", f"branches[{index}].scene_id", "Branch scene must belong to its quest."))
            if branch.approach not in quest.required_approaches and branch.approach != "unexpected":
                findings.append(_finding("undeclared_approach", f"branches[{index}].approach", "Branch approach was not declared for this quest."))
        if branch.scene_id not in scene_ids:
            findings.append(_finding("unknown_branch_scene", f"branches[{index}].scene_id", "Branch names an unknown scene."))
        if set(branch.requires) - runtime_flag_keys:
            findings.append(_finding("unbacked_world_flag", f"branches[{index}].requires", "Branch conditions must use runtime WorldFlag keys."))
        for policy_id in branch.policy_ids:
            policy = policy_by_id.get(policy_id)
            if policy is None or branch.scene_id not in policy.scene_ids:
                findings.append(_finding("unknown_branch_policy", f"branches[{index}].policy_ids", "Branch policy must be reviewed for the same scene."))
        if set(branch.outcome_ids) - set(outcome_ids):
            findings.append(_finding("unknown_branch_outcome", f"branches[{index}].outcome_ids", "Branch references an unknown outcome."))
    for quest in candidate.quests:
        actual_approaches = {branch.approach for branch in branches_by_quest.get(quest.quest_id, [])}
        if len(actual_approaches) < 2 or not set(quest.required_approaches) <= actual_approaches:
            findings.append(_finding("quest_branch_coverage", f"quests.{quest.quest_id}", "Quest must contain each declared materially different approach."))

    outcome_by_id = {outcome.outcome_id: outcome for outcome in candidate.outcomes}
    for index, outcome in enumerate(candidate.outcomes):
        branch = branch_by_id.get(outcome.branch_id)
        if branch is None:
            findings.append(_finding("unknown_outcome_branch", f"outcomes[{index}].branch_id", "Outcome names an unknown branch."))
            continue
        if outcome.outcome_id not in branch.outcome_ids:
            findings.append(_finding("outcome_not_reachable_from_branch", f"outcomes[{index}]", "Branch must explicitly expose this outcome."))
        selected_policy_ids = [selection.policy_id for selection in outcome.policy_selections]
        if len(selected_policy_ids) != len(set(selected_policy_ids)) or not set(selected_policy_ids) <= set(branch.policy_ids):
            findings.append(_finding("outcome_policy_mismatch", f"outcomes[{index}].policy_selections", "Outcome may select each of its branch's reviewed policies once."))
        if set(outcome.requires) - runtime_flag_keys:
            findings.append(_finding("unbacked_world_flag", f"outcomes[{index}].requires", "Outcome conditions must use runtime WorldFlag keys."))
        if branch is not None:
            quest = quest_by_id.get(branch.quest_id)
            conditions = [branch.requires, outcome.requires]
            if quest is not None:
                conditions.insert(0, quest.prerequisites)
            conditions.extend(
                policy_by_id[selection.policy_id].requires
                for selection in outcome.policy_selections
                if selection.policy_id in policy_by_id
            )
            if any(
                _predicate_conflicts(left, right)
                for position, left in enumerate(conditions)
                for right in conditions[position + 1 :]
            ):
                findings.append(_finding("unreachable_outcome", f"outcomes[{index}]", "Quest, branch, outcome and selected policy predicates cannot all be true together."))
        effects = []
        for selection in outcome.policy_selections:
            policy = policy_by_id.get(selection.policy_id)
            if policy is None:
                findings.append(_finding("unknown_outcome_policy", f"outcomes[{index}].policy_selections", "Outcome selects an unknown reviewed policy."))
                continue
            if selection.result == "automatic" and policy.mode != "automatic":
                findings.append(_finding("invalid_policy_result", f"outcomes[{index}].policy_selections", "Automatic result requires an automatic reviewed policy."))
            if selection.result in {"success", "failure"} and policy.mode != "check":
                findings.append(_finding("invalid_policy_result", f"outcomes[{index}].policy_selections", "Check result requires a check-based reviewed policy."))
            if selection.result == "failure" and policy.failure_mode == "no_progress":
                findings.append(_finding("unmodeled_failure", f"outcomes[{index}].policy_selections", "Failure outcome needs an authored supported consequence."))
            effects.extend(_policy_effects(policy, selection.result))
        if outcome.result == "partial" and not any(
            selection.result == "failure" and policy_by_id.get(selection.policy_id) and policy_by_id[selection.policy_id].failure_mode == "partial"
            for selection in outcome.policy_selections
        ):
            findings.append(_finding("unmodeled_partial_success", f"outcomes[{index}]", "Partial outcome must use a reviewed partial-failure policy."))
        for ending_id in outcome.ending_entity_ids:
            entity = entity_by_id.get(ending_id)
            if entity is None or entity.kind != "ending":
                findings.append(_finding("unknown_ending", f"outcomes[{index}].ending_entity_ids", "Outcome ending must reference an existing ending entity."))
            elif not any(effect.kind == "discover" and effect.target_id == ending_id for effect in effects):
                findings.append(_finding("unreachable_ending", f"outcomes[{index}].ending_entity_ids", "Outcome needs an engine-validated discovery effect for its ending."))
        for compatible_id in outcome.compatible_with:
            compatible = outcome_by_id.get(compatible_id)
            if compatible is None or compatible_id == outcome.outcome_id:
                findings.append(_finding("invalid_compatibility_reference", f"outcomes[{index}].compatible_with", "Compatibility must name a different existing outcome."))
                continue
            other_branch = branch_by_id.get(compatible.branch_id)
            other_effects = _selected_effects(compatible, policy_by_id) if all(
                row.policy_id in policy_by_id for row in compatible.policy_selections
            ) else []
            if _predicate_conflicts(outcome.requires, compatible.requires) or _effect_conflict(effects, other_effects):
                findings.append(_finding("incompatible_endings", f"outcomes[{index}].compatible_with", "Claimed outcomes require contradictory state or durable effects."))
            if other_branch is None:
                findings.append(_finding("invalid_compatibility_reference", f"outcomes[{index}].compatible_with", "Compatible outcome must have a valid branch."))
    for branch in candidate.branches:
        branch_outcomes = [outcome_by_id[oid] for oid in branch.outcome_ids if oid in outcome_by_id]
        if any(outcome.branch_id != branch.branch_id for outcome in branch_outcomes):
            findings.append(_finding("outcome_branch_mismatch", f"branches.{branch.branch_id}.outcome_ids", "Every branch outcome must point back to the branch that exposes it."))
        if len(branch_outcomes) < 2 or "success" not in {row.result for row in branch_outcomes} or not ({"failure", "partial"} & {row.result for row in branch_outcomes}):
            findings.append(_finding("branch_outcome_coverage", f"branches.{branch.branch_id}", "Each branch needs a reachable success and failure or partial outcome."))
    ending_ids = {entity.entity_id for entity in canonical.entities if entity.kind == "ending"}
    prepared_ending_ids = {ending_id for outcome in candidate.outcomes for ending_id in outcome.ending_entity_ids}
    if ending_ids - prepared_ending_ids:
        findings.append(_finding("unprepared_ending", "outcomes", "Every canonical ending must have a reachable prepared outcome."))

    # Route graph reachability is checked from the reviewed opening; runtime still enforces gates.
    if canonical.opening_scene_id not in scene_ids:
        findings.append(_finding("missing_opening", "opening_scene_id", "Prepared topology needs a reviewed opening scene."))
    else:
        adjacency: dict[str, set[str]] = {scene_id: set() for scene_id in scene_ids}
        for source, destination in manifest_routes:
            adjacency[source].add(destination)
        reached = {canonical.opening_scene_id}
        pending = [canonical.opening_scene_id]
        while pending:
            current = pending.pop()
            for destination in adjacency[current] - reached:
                reached.add(destination)
                pending.append(destination)
        if reached != scene_ids:
            findings.append(_finding("unreachable_scene", "routes", "Every canonical scene must be reachable in the reviewed route graph."))
        if any(branch.scene_id not in reached for branch in candidate.branches):
            findings.append(_finding("unreachable_branch", "branches", "Every prepared branch must be reachable from the opening."))

    for index, record in enumerate(candidate.acts + candidate.quests + candidate.scene_packets + candidate.routes + candidate.extensions + candidate.branches + candidate.outcomes):
        provenance = record.provenance
        for passage_id in provenance.passage_ids:
            passage = passage_by_id.get(passage_id)
            if passage is None or passage.audience in {"excluded", "unreviewed"} or passage.text_sha256 is None:
                findings.append(_finding("invalid_provenance", f"prepared[{index}].provenance", "Provenance must reference retained, hashed source passages that are not excluded or unreviewed."))
    if candidate.unresolved_questions:
        findings.append(_finding("unresolved_questions", "unresolved_questions", "Private ambiguities must be resolved before verified preparation."))
    return PreparedValidationReport(prepared_sha256=digest, findings=findings)


def prepared_extension_chunks(
    manifest: AdventureManifest | dict,
    prepared: PreparedCampaign | dict | None = None,
) -> list[dict]:
    """Materialize private DM text as separately typed chunks after reviewed validation.

    The returned records carry no PDF page or span. Consumers must preserve extension
    provenance and enforce each `requires` predicate from committed runtime WorldFlags.
    """
    from edgar_core.manifests import (
        AdventureManifest,
        ManifestError,
        manifest_digest,
        require_published,
    )

    canonical = manifest if isinstance(manifest, AdventureManifest) else AdventureManifest.model_validate(manifest)
    reviewed = require_published(canonical)
    candidate = prepared if isinstance(prepared, PreparedCampaign) else PreparedCampaign.model_validate(prepared if prepared is not None else reviewed.prepared)
    report = validate_prepared_campaign(reviewed, candidate)
    if not report.valid or reviewed.prepared is None or reviewed.prepared != candidate:
        raise ManifestError("Prepared extension chunks require the exact validated private manifest version.")
    if reviewed.reviewed_sha256 != manifest_digest(reviewed):
        raise ManifestError("Prepared extension chunks require a current manifest review receipt.")
    return [
        {
            "collection": reviewed.source,
            "source": f"dm-extension:{extension.extension_id}",
            "source_kind": "dm_extension",
            "source_sha256": reviewed.source_sha256,
            "extraction_sha256": reviewed.extraction_sha256,
            "manifest_sha256": reviewed.reviewed_sha256,
            "rules_edition": reviewed.rules_edition,
            "passage_id": extension.passage_id,
            "extension_id": extension.extension_id,
            "page": None,
            "page_end": None,
            "source_pages": [],
            "source_span": None,
            "text_sha256": text_digest(extension.text),
            "text": extension.text,
            "scene_ids": list(extension.scene_ids),
            "entity_ids": list(extension.entity_ids),
            "audience": extension.audience,
            "requires": dict(extension.requires),
            "provenance_origin": "dm_extension",
            "provenance_passage_ids": list(extension.provenance.passage_ids),
            "provenance_rationale": extension.provenance.rationale,
        }
        for extension in candidate.extensions
    ]


def project_prepared_extension_chunks(
    manifest: AdventureManifest | dict,
    *,
    scene_id: str,
    world_flags: dict[str, str],
    audience: Literal["player", "planner"] = "player",
) -> list[dict]:
    """Return only current-scene extension text whose committed WorldFlags are earned."""
    if not isinstance(scene_id, str) or not scene_id:
        return []
    chunks = prepared_extension_chunks(manifest)
    flags = world_flags or {}
    return [
        chunk
        for chunk in chunks
        if audience == chunk["audience"]
        and scene_id in chunk["scene_ids"]
        and all(flags.get(key) == value for key, value in chunk["requires"].items())
    ]


def _effect_conflicts_with_fact(effects: list, fact: CommittedPreparedFact) -> bool:
    if fact.kind == "world_flag":
        return False
    for effect in effects:
        kind = {
            "discover": "entity", "encounter": "entity", "relationship": "entity",
            "objective": "objective", "milestone": "milestone", "clock": "clock",
            "acquire": "object", "consume": "object", "transfer": "object",
            "use": "object", "environment": "object", "move": "scene", "learn_place": "scene",
        }.get(effect.kind)
        if kind != fact.kind or effect.target_id != fact.target_id:
            continue
        if fact.irreversible and effect.status != fact.status:
            return True
        if fact.kind == "entity" and effect.kind == "discover" and fact.passage_ids and set(effect.passage_ids) - set(fact.passage_ids):
            return True
    return False


def validate_prepared_amendment(
    manifest: AdventureManifest | dict,
    prepared: PreparedCampaign | dict,
    amendment: PreparedAmendment | dict,
    revealed_facts: list[CommittedPreparedFact | dict],
    completed_outcomes: list[Identifier],
) -> PreparedAmendmentResult:
    """Validate bounded additive new branches/outcomes against committed play facts.

    Existing definitions are never supplied as replacements. Amendments may add at most
    eight branches/extensions and sixteen outcomes, must match the exact base digest, and
    must preserve source text, revealed passage digests, WorldFlags and irreversible facts.
    """
    from copy import deepcopy

    from edgar_core.manifests import AdventureManifest

    findings: list[PreparedFinding] = []
    try:
        canonical = manifest if isinstance(manifest, AdventureManifest) else AdventureManifest.model_validate(manifest)
        base = prepared if isinstance(prepared, PreparedCampaign) else PreparedCampaign.model_validate(prepared)
        candidate_amendment = amendment if isinstance(amendment, PreparedAmendment) else PreparedAmendment.model_validate(amendment)
        facts = [item if isinstance(item, CommittedPreparedFact) else CommittedPreparedFact.model_validate(item) for item in revealed_facts]
    except (TypeError, ValueError) as exc:
        return PreparedAmendmentResult(report=PreparedValidationReport(findings=[_finding("invalid_amendment", "$", str(exc)[:1000])]))
    if prepared_digest(base) != candidate_amendment.base_prepared_sha256:
        findings.append(_finding("stale_amendment_base", "base_prepared_sha256", "Amendment must target the exact current prepared digest."))
    addition_count = len(candidate_amendment.branches) + len(candidate_amendment.outcomes) + len(candidate_amendment.extensions)
    if not 1 <= addition_count <= 24 or len(candidate_amendment.branches) > 8 or len(candidate_amendment.outcomes) > 16 or len(candidate_amendment.extensions) > 8:
        findings.append(_finding("amendment_bound", "amendment", "Amendments are bounded to 8 branches, 16 outcomes and 8 extensions per change."))
    existing_branch_ids = {item.branch_id for item in base.branches}
    existing_outcome_ids = {item.outcome_id for item in base.outcomes}
    existing_extension_ids = {item.extension_id for item in base.extensions}
    existing_passage_ids = {item.passage_id for item in base.extensions}
    if existing_branch_ids & {item.branch_id for item in candidate_amendment.branches}:
        findings.append(_finding("amendment_rewrites_branch", "branches", "Amendments may add branches but cannot edit or replace an existing branch."))
    if existing_outcome_ids & {item.outcome_id for item in candidate_amendment.outcomes}:
        findings.append(_finding("amendment_rewrites_outcome", "outcomes", "Amendments may add outcomes but cannot edit or replace a completed or prepared outcome."))
    if existing_extension_ids & {item.extension_id for item in candidate_amendment.extensions} or existing_passage_ids & {item.passage_id for item in candidate_amendment.extensions}:
        findings.append(_finding("amendment_rewrites_extension", "extensions", "Amendments may add new extension identities but cannot edit an existing passage."))
    known_outcomes = {item.outcome_id: item for item in base.outcomes}
    completed = set(completed_outcomes)
    if completed - set(known_outcomes):
        findings.append(_finding("unknown_completed_outcome", "completed_outcomes", "Completed outcome IDs must belong to the current prepared campaign."))
    for fact in facts:
        if fact.kind == "world_flag":
            for row in candidate_amendment.branches:
                if fact.target_id in row.requires and row.requires[fact.target_id] != (fact.value or fact.status):
                    findings.append(_finding("amendment_contradicts_flag", f"branches.{row.branch_id}.requires", "New branch conflicts with a committed WorldFlag."))
            for row in candidate_amendment.outcomes:
                if fact.target_id in row.requires and row.requires[fact.target_id] != (fact.value or fact.status):
                    findings.append(_finding("amendment_contradicts_flag", f"outcomes.{row.outcome_id}.requires", "New outcome conflicts with a committed WorldFlag."))
            for row in candidate_amendment.extensions:
                if fact.target_id in row.requires and row.requires[fact.target_id] != (fact.value or fact.status):
                    findings.append(_finding("amendment_contradicts_flag", f"extensions.{row.extension_id}.requires", "New extension reveal conflicts with a committed WorldFlag."))
        if fact.kind == "passage":
            source = next((row for row in canonical.passages if row.passage_id == fact.target_id), None)
            extension = next((row for row in base.extensions if row.passage_id == fact.target_id), None)
            current_digest = source.text_sha256 if source else text_digest(extension.text) if extension else None
            if current_digest != fact.text_sha256:
                findings.append(_finding("revealed_text_changed", f"revealed_facts.{fact.target_id}", "An exact revealed source or extension passage changed after it was committed."))

    new_base = deepcopy(base.model_dump(mode="python"))
    new_base["branches"].extend(item.model_dump(mode="python") for item in candidate_amendment.branches)
    new_base["outcomes"].extend(item.model_dump(mode="python") for item in candidate_amendment.outcomes)
    new_base["extensions"].extend(item.model_dump(mode="python") for item in candidate_amendment.extensions)
    amended = PreparedCampaign.model_validate(new_base)
    candidate_report = validate_prepared_campaign(canonical, amended)
    findings.extend(candidate_report.findings)
    all_policies = {row.policy_id: row for row in canonical.resolution_policies}
    for outcome in candidate_amendment.outcomes:
        if completed and not completed <= set(outcome.compatible_with):
            findings.append(_finding("amendment_incompatible_with_progress", f"outcomes.{outcome.outcome_id}.compatible_with", "New outcomes must explicitly preserve every completed outcome."))
        if all(selection.policy_id in all_policies for selection in outcome.policy_selections):
            effects = _selected_effects(outcome, all_policies)
            for fact in facts:
                if _effect_conflicts_with_fact(effects, fact):
                    findings.append(_finding("amendment_rewrites_irreversible_fact", f"outcomes.{outcome.outcome_id}", "New outcome conflicts with a revealed or irreversible committed fact."))
    if findings:
        return PreparedAmendmentResult(report=PreparedValidationReport(prepared_sha256=candidate_report.prepared_sha256, findings=findings))
    return PreparedAmendmentResult(report=candidate_report, amended=amended)


__all__ = [
    "CommittedPreparedFact", "PreparedAct", "PreparedAmendment", "PreparedAmendmentResult",
    "PreparedBranch", "PreparedCampaign", "PreparedExtension", "PreparedFinding", "PreparedOutcome",
    "PreparedPolicySelection", "PreparedProvenance", "PreparedQuest", "PreparedRoute",
    "PreparedScenePacket", "PreparedValidationReport", "prepared_digest",
    "prepared_extension_chunks", "validate_prepared_amendment", "validate_prepared_campaign",
    "project_prepared_extension_chunks",
]
