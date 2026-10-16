"""Offline joined production workers, exact local books, real maps, review and repair."""

from copy import deepcopy

import pytest
from test_preparation_grounding import _party, source_index  # noqa: F401
from test_prepared_campaign import _fixture

from apps.api.services import adventures_meta
from apps.api.services.preparation_content import CampaignDesign, CanonicalAct
from apps.api.services.preparation_stages import make_preparation_supervisor
from edgar_core.model_profiles import load_model_profiles
from edgar_core.preparation import (
    PreparationBudget,
    PreparationRunBinding,
    ProviderUsage,
    canonical_sha256,
)

SEED = (
    "The ferry keeper waits beside the old dock. A brass bell hangs above the landing, and a written timetable "
    "shows the last safe crossing before sunset. The keeper asks the traveller to persuade the guards to "
    "allow the stranded villagers aboard. The guards fear punishment from their commander, but they will "
    "accept a public promise to report the crossing truthfully. The traveller may negotiate, investigate "
    "the missing permit, or challenge the guards. A failed appeal delays the crossing and changes the "
    "keeper's trust without destroying the only route onward. The island remains visible across the water."
)


CHAPTER = (
    "Begin at the landing as the evening boats return from the island. Describe the ropes, the wet boards, "
    "and the travellers waiting with their bundles. The keeper remains because his neighbours depend on "
    "the crossing, and he refuses to leave them behind. Ask how the hero approaches the guards. A calm "
    "explanation earns a hearing; a threat brings another guard to the gate. Present the timetable exactly "
    "as established in the source. Investigation can locate the missing permit, while negotiation can secure "
    "a signed promise. On failure, let the guards demand a witness and record the delay. On partial success, "
    "the villagers cross while the hero stays to answer questions. End with the keeper remembering the hero's choice."
)


def _design_and_fragment(source="prepared_test"):
    manifest, prepared = _fixture(source)
    manifest.passages[0].start = 0
    manifest.passages[0].end = len(SEED)
    manifest.scenes[0].links[0].requires = {}
    manifest.resolution_policies[0].requires = {}
    prepared.routes[0].traversal_requires = {}
    prepared.extensions[0].requires = {}
    design = CampaignDesign.model_validate(
        {
            "title": "The Crossing",
            "premise": SEED[:200],
            "opening_scene_id": "dock",
            "continuity": ["The keeper cannot abandon the villagers."],
            "acts": [
                {
                    "act_id": "act_one",
                    "title": "The Crossing",
                    "scene_ids": ["dock"],
                    "summary": SEED[:200],
                    "role": "opening",
                    "advance_ids": ["rescue_keeper"],
                    "climax_ids": [],
                    "source_pages": [],
                    "seed_text": SEED,
                }
            ],
        }
    )
    fields = {
        name: getattr(manifest, name)
        for name in (
            "scenes",
            "entities",
            "passages",
            "objectives",
            "milestones",
            "objects",
            "promises",
            "clocks",
            "opening_journal",
            "resolution_policies",
        )
    }
    fragment = CanonicalAct(
        act_id="act_one",
        act=prepared.acts[0],
        **fields,
        **{
            name: getattr(prepared, name)
            for name in ("quests", "scene_packets", "routes", "branches", "outcomes", "extensions")
        },
    )
    return design, fragment


def _binding(*, mode="ai", level=1, rounds=2):
    party = _party(level).model_dump(mode="json")
    profile = load_model_profiles().to_dict()
    commission = {
        "act_count": 1,
        "target_level": level,
        "required_maps": True,
        "required_handouts": True,
    }
    return PreparationRunBinding(
        run_id="prep-" + "d" * 32,
        source_mode=mode,
        source_slug="prepared_test",
        source_version="v1",
        rules_edition="2014",
        source_sha256="1" * 64 if mode == "pdf" else None,
        extraction_sha256="2" * 64 if mode == "pdf" else None,
        commission=commission,
        commission_sha256=canonical_sha256(commission),
        hero_party_snapshot=party,
        hero_party_sha256=canonical_sha256(party),
        profile_snapshot=profile,
        profile_snapshot_sha256=canonical_sha256(profile),
        budget=PreparationBudget(
            max_provider_calls=200,
            max_total_tokens=8_000_000,
            max_cost_microusd=200_000_000,
            max_repair_rounds=rounds,
        ),
    )


class OfflineAuthor:
    def __init__(self, *, criticize_once=False):
        self.calls = []
        self.criticize_once = criticize_once
        self.critic_count = 0

    async def __call__(self, binding, task, schema, *, instruction, evidence, idempotency_key):
        from apps.api.services.preparation_stages import (
            ActMechanics,
            AssetRequests,
            BookSelection,
            CompleteChapter,
            CriticObservations,
            RepairDirections,
            ScenePreparation,
        )

        design, fragment = _design_and_fragment(binding.source_slug)
        self.calls.append((task.stage.value, schema.__name__, idempotency_key))
        if schema is BookSelection:
            data = {
                "requests": [
                    {"role": role, "references": headings[:1]}
                    for role, headings in evidence["headings"].items()
                ]
            }
        elif schema is CampaignDesign:
            data = design.model_dump()
        elif schema is CanonicalAct:
            data = fragment.model_dump()
        elif schema is ActMechanics:
            data = {
                "act_id": "act_one",
                "encounters": [],
                "mechanics": [],
                "resource_plan": "The guards negotiate peacefully. No mandatory resources or rest are consumed in this social act.",
                "progression_plan": "The actual hero stays at the commissioned level; resolving the crossing does not invent XP or abilities.",
            }
        elif schema is ScenePreparation:
            data = {
                "act_id": "act_one",
                "packets": [p.model_dump() for p in fragment.scene_packets],
                "extensions": [],
                "resource_plan": "No mandatory resource expenditure; the guard accepts the keeper's verifiable promise.",
            }
        elif schema is AssetRequests:
            data = {"requirements": []}
        elif schema is CompleteChapter:
            data = {"act_id": "act_one", "text": CHAPTER}
        elif schema is CriticObservations:
            from edgar_core.artifacts import text_digest

            self.critic_count += 1
            findings = []
            if self.criticize_once and self.critic_count == 1:
                excerpt = "Begin at the landing as the evening boats return from the island."
                findings = [
                    {
                        "finding_id": "unclear_keeper_motive",
                        "code": "unclear_keeper_motive",
                        "severity": "high",
                        "message": "Clarify why the keeper waits, with a causal connection to the villagers.",
                        "affected_ids": ["act_one"],
                        "evidence": [
                            {
                                "artifact_id": evidence["manuscript_artifact_id"],
                                "artifact_sha256": evidence["manuscript_artifact_sha256"],
                                "json_pointer": evidence["chapter_json_pointer"],
                                "excerpt": excerpt,
                                "excerpt_sha256": text_digest(excerpt),
                            }
                        ],
                    }
                ]
            data = {
                "reviewed_chapter_ids": [evidence["reviewed_chapter"]["chapter_id"]],
                "findings": findings,
            }
        elif schema is RepairDirections:
            data = {
                "finding_ids": [f["finding_id"] for f in evidence["findings"]],
                "instructions": "Correct the keeper's causal motivation in canonical facts, then recreate all dependent scenes and complete chapters and independently review them again.",
                "preserve_exact_source": True,
            }
        else:
            pytest.fail(f"Unexpected schema {schema}")
        return schema.model_validate(deepcopy(data)), ProviderUsage(
            provider_called=True,
            call_count=1,
            input_tokens=100,
            output_tokens=100,
            cost_microusd=2400,
            requested_model="gpt-6.1-sol",
            reasoning_effort="high",
            task_version="offline-author",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("criticize_once", [False, True])
async def test_joined_ai_all_twelve_stages_real_books_maps_and_repair(
    tmp_path, monkeypatch, source_index, criticize_once  # noqa: F811
):
    client, _ = source_index
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    from apps.api.services import adventure_assets

    monkeypatch.setattr(adventure_assets, "ASSET_ROOT", tmp_path / "assets")
    author = OfflineAuthor(criticize_once=criticize_once)
    supervisor = make_preparation_supervisor(provider=author, book_client=client)
    binding = _binding()
    supervisor.create_run(binding)
    published = []

    def publish(bundle):
        published.append(bundle)
        return bundle.bundle_sha256

    for _ in range(100):
        step = await supervisor.step(
            binding.source_slug, binding.run_id, "offline-worker", lambda _: True, publish
        )
        if step.state.status.value in ("ready", "blocked", "failed"):
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.status.value == "ready", (
        state.current_stage,
        state.last_safe_error_code,
        [(k, t.last_safe_error_code) for k, t in state.tasks.items() if t.last_safe_error_code],
    )
    assert len(published) == 1
    assert set(state.stage_statuses) == {f"P{i:02}" for i in range(1, 13)}
    assert all(status.value == "accepted" for status in state.stage_statuses.values())
    assert state.provider_calls_spent == len(author.calls)
    assert state.repair_rounds == int(criticize_once)
    coverage = next(a for a in state.artifacts.values() if a.artifact_kind == "asset_coverage")
    assert coverage.payload["receipt"]["image_calls_reserved"] == 0
    assert all(
        a["status"] == "ready" for a in coverage.payload["receipt"]["coverage"] if a["required"]
    )
    assert any(a["kind"] == "schematic_map" for a in coverage.payload["receipt"]["coverage"])
    if criticize_once:
        assert len(state.repair_history) == 1
        assert author.critic_count == 2
        assert any(a.artifact_kind == "repair_context" for a in state.artifacts.values())
        assert any(a.version == 2 for a in state.artifacts.values() if a.stage.value == "P09")
