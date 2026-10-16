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
    def __init__(self, *, criticize_once=False, content=None):
        self.calls = []
        self.criticize_once = criticize_once
        self.critic_count = 0
        self.content = content

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
        if self.content:
            design, fragments = self.content
            fragment = next((f for f in fragments if f.act_id in task.scope_keys), fragments[0])
        act_id = fragment.act_id
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
                "act_id": act_id,
                "encounters": [],
                "mechanics": [],
                "resource_plan": "The guards negotiate peacefully. No mandatory resources or rest are consumed in this social act.",
                "progression_plan": "The actual hero stays at the commissioned level; resolving the crossing does not invent XP or abilities.",
            }
        elif schema is ScenePreparation:
            data = {
                "act_id": act_id,
                "packets": [p.model_dump() for p in fragment.scene_packets],
                "extensions": [],
                "resource_plan": "No mandatory resource expenditure; the guard accepts the keeper's verifiable promise.",
            }
        elif schema is AssetRequests:
            data = {"requirements": []}
        elif schema is CompleteChapter:
            data = {"act_id": act_id, "text": CHAPTER}
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
        elif schema.__name__ == "SourceStudy":
            data = {
                "page_numbers": [p["page"] for p in evidence["pages"]],
                "summary": SEED,
                "commitments": ["Preserve the exact printed crossing and its quotation."],
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
    tmp_path,
    monkeypatch,
    source_index,  # noqa: F811
    criticize_once,  # noqa: F811
):
    client, _ = source_index
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    from apps.api.services import adventure_assets

    monkeypatch.setattr(adventure_assets, "ASSET_ROOT", tmp_path / "assets")
    author = OfflineAuthor(criticize_once=criticize_once)
    supervisor = make_preparation_supervisor(provider=author, book_client=client)
    binding = _binding()
    from apps.api.services import preparation_runtime as runtime
    from db.vector import versions
    from ingestion import embed

    token = "b" * 32
    adventures_meta.save_meta(
        binding.source_slug,
        {
            "private_preparation": True,
            "preparation_run_id": binding.run_id,
            "status": "processing",
            "attempt": {"id": token},
            "job": {
                "job_id": binding.run_id.removeprefix("prep-"),
                "attempt_id": token,
                "state": "running",
            },
        },
    )
    monkeypatch.setattr(runtime, "_current_source_matches", lambda *_: True)
    monkeypatch.setattr(embed, "_embed_and_upsert", lambda chunks, *_args, **_kwargs: len(chunks))
    supervisor.create_run(binding)
    published = []

    def publish(bundle):
        assert not runtime.private_preparation_ready(binding.source_slug)
        result = runtime._publish_candidate(binding.source_slug, token, bundle)
        assert not runtime.private_preparation_ready(binding.source_slug)
        published.append(bundle)
        return result

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
    # Crash/restart after the durable READY checkpoint can finish metadata without
    # another semantic request or embedding activation.
    runtime._finalize_ready(binding.source_slug, token, state)
    meta = adventures_meta.load_meta(binding.source_slug)
    monkeypatch.setattr(
        versions, "active_index_version", lambda _: meta["prepared_bundle"]["index_version"]
    )
    assert runtime.private_preparation_ready(binding.source_slug)
    # Retained asset corruption revokes usability even when old flags claim ready.
    asset_path = next((tmp_path / "assets").rglob("*.svg"))
    original = asset_path.read_bytes()
    asset_path.write_bytes(b"corrupt")
    assert not runtime.private_preparation_ready(binding.source_slug)
    asset_path.write_bytes(original)
    assert runtime.private_preparation_ready(binding.source_slug)
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


def _campaign_content(count, *, pdf_text=None):
    """Synthetic distinct identities across a linear campaign; no live prose claim."""
    design, original = _design_and_fragment()
    fragments, acts = [], []
    identities = {
        "act_one",
        "dock",
        "source_intro",
        "rescue_keeper",
        "rescue_check",
        "keeper_quest",
        "dm_note",
        "keeper_note",
        "negotiate",
        "fight",
        "negotiation_success",
        "negotiation_partial",
        "fight_success",
        "fight_partial",
    }
    for index in range(count):
        mapping = {name: f"{name}_{index}" for name in identities}

        def remap(value, mapping=mapping):
            if isinstance(value, str):
                return mapping.get(value, value)
            if isinstance(value, list):
                return [remap(v) for v in value]
            if isinstance(value, dict):
                return {k: remap(v) for k, v in value.items()}
            return value

        fragment = CanonicalAct.model_validate(remap(original.model_dump(mode="json")))
        fragment.act.order = index + 1
        fragment.passages[0].page = index + 1
        text = pdf_text if pdf_text is not None else SEED
        fragment.passages[0].end = len(text)
        from edgar_core.artifacts import text_digest

        fragment.passages[0].text_sha256 = text_digest(text)
        if index + 1 < count:
            from edgar_core.manifests import Link
            from edgar_core.prepared import PreparedRoute

            destination = f"dock_{index + 1}"
            fragment.scenes[0].links.append(Link(scene_id=destination))
            fragment.routes.append(
                PreparedRoute(
                    from_scene_id=fragment.scenes[0].scene_id,
                    to_scene_id=destination,
                    visibility="known",
                    provenance=fragment.act.provenance,
                )
            )
        fragments.append(fragment)
        act = remap(design.acts[0].model_dump(mode="json"))
        act.update(
            role="opening" if index == 0 else "resolution" if index == count - 1 else "development",
            source_pages=[index + 1] if pdf_text is not None else [],
            seed_text=text,
        )
        acts.append(act)
    result = design.model_dump(mode="json")
    result.update(acts=acts, opening_scene_id=fragments[0].scenes[0].scene_id)
    return CampaignDesign.model_validate(result), fragments


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,count,level", [("ai", 24, 1), ("ai", 1, 12), ("pdf", 1, 1)])
async def test_joined_long_high_level_and_pdf_are_independent_scopes(
    tmp_path, monkeypatch, source_index, mode, count, level  # noqa: F811
):  # noqa: F811
    from apps.api.services import adventure_assets
    from apps.api.services.preparation_sources import retain_pdf
    from edgar_core.preparation import PreparationJobStatus

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventure_assets, "ASSET_ROOT", tmp_path / "assets")
    binding = _binding(mode=mode, level=level)
    pdf_text = None
    if mode == "pdf":
        import pymupdf

        path = tmp_path / "input.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            assert page.insert_textbox(pymupdf.Rect(36, 36, 560, 780), SEED, fontsize=10) >= 0
            pdf.save(path)
        extraction = retain_pdf(binding.source_slug, binding.run_id, path, binding.rules_edition)
        pdf_text = extraction.pages[0].text
        binding = binding.model_copy(
            update={
                "source_sha256": extraction.source_sha256,
                "extraction_sha256": extraction.text_sha256(),
            }
        )
    commission = {**binding.commission, "act_count": count}
    binding = binding.model_copy(
        update={
            "commission": commission,
            "commission_sha256": canonical_sha256(commission),
            "budget": binding.budget.model_copy(
                update={
                    "max_provider_calls": 300,
                    "max_total_tokens": 40_000_000,
                    "max_cost_microusd": 1_000_000_000,
                }
            ),
        }
    )
    author = OfflineAuthor(content=_campaign_content(count, pdf_text=pdf_text))
    supervisor = make_preparation_supervisor(provider=author, book_client=source_index[0])
    supervisor.create_run(binding)
    published = []
    for _ in range(250):
        step = await supervisor.step(
            binding.source_slug,
            binding.run_id,
            "offline",
            lambda _: True,
            lambda bundle: published.append(bundle) or bundle.bundle_sha256,
        )
        if step.state.status in (
            PreparationJobStatus.READY,
            PreparationJobStatus.FAILED,
            PreparationJobStatus.BLOCKED,
        ):
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.status == PreparationJobStatus.READY, (
        state.current_stage,
        state.last_safe_error_code,
        [
            (t.task.task_id if hasattr(t, "task") else k, t.last_safe_error_code)
            for k, t in state.tasks.items()
            if t.last_safe_error_code
        ],
    )
    manuscript = next(
        a for a in state.artifacts.values() if a.artifact_kind == "retained_manuscript"
    )
    assert len(manuscript.payload["manuscript"]["chapters"]) == count
    assert author.critic_count == count
    assert len(published) == 1
    if mode == "pdf":
        canonical = next(
            a for a in state.artifacts.values() if a.artifact_kind == "canonical_extraction"
        )
        assert canonical.payload["pages"] == [
            {"page": 1, "text": pdf_text, "method": extraction.pages[0].method}
        ]
        assert canonical.payload["source_sha256"] == binding.source_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_joined_pdf_visual_critic_receives_pixels_and_required_rejection_blocks(
    tmp_path, monkeypatch, source_index, approved  # noqa: F811
):  # noqa: F811
    import base64

    import pymupdf
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from apps.api.services import adventure_assets
    from apps.api.services.preparation_sources import retain_pdf
    from edgar_core.assets import IndependentVisualReviewDecision

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventure_assets, "ASSET_ROOT", tmp_path / "assets")
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "EDGAR_VISUAL_REVIEW_PRIVATE_KEY",
        base64.b64encode(
            key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
        ).decode(),
    )
    monkeypatch.setenv(
        "EDGAR_VISUAL_REVIEW_PUBLIC_KEY",
        base64.b64encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ).decode(),
    )
    binding = _binding(mode="pdf")
    path = tmp_path / "input.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        assert page.insert_textbox(pymupdf.Rect(36, 36, 560, 780), SEED, fontsize=10) >= 0
        pdf.save(path)
    extraction = retain_pdf(binding.source_slug, binding.run_id, path, "2014")
    binding = binding.model_copy(
        update={
            "source_sha256": extraction.source_sha256,
            "extraction_sha256": extraction.text_sha256(),
        }
    )

    class VisualAuthor(OfflineAuthor):
        async def __call__(self, binding, task, schema, *, images=(), **kwargs):
            if schema is IndependentVisualReviewDecision:
                self.calls.append((task.stage.value, schema.__name__, kwargs["idempotency_key"]))
                assert len(images) == 1 and images[0][1] == "image/png"
                assert images[0][0].startswith(b"\x89PNG")
                candidate = kwargs["evidence"]["candidate"]
                return schema(
                    candidate_id=candidate["candidate_id"],
                    artifact_sha256=candidate["artifact_sha256"],
                    decision="approved" if approved else "rejected",
                    reasons=["Synthetic pixel review fixture."],
                ), ProviderUsage(
                    provider_called=True,
                    call_count=1,
                    input_tokens=100,
                    output_tokens=100,
                    cost_microusd=2400,
                    requested_model="gpt-6.1-sol",
                    reasoning_effort="high",
                    task_version="offline-visual",
                )
            result, usage = await super().__call__(binding, task, schema, **kwargs)
            if schema.__name__ == "AssetRequests":
                result = schema(
                    requirements=[
                        {
                            "asset_id": "printed_map",
                            "kind": "source_map",
                            "required": True,
                            "act_id": "act_one_0",
                            "scene_ids": ["dock_0"],
                            "source_crop": {
                                "collection": binding.source_slug,
                                "source_sha256": binding.source_sha256,
                                "extraction_sha256": binding.extraction_sha256,
                                "book_title": "Synthetic crossing",
                                "edition": "2014",
                                "page": 1,
                                "crop_id": "blank_corner",
                                "crop": {"x": 0.7, "y": 0.7, "width": 0.2, "height": 0.2},
                            },
                        }
                    ]
                )
            return result, usage

    author = VisualAuthor(content=_campaign_content(1, pdf_text=extraction.pages[0].text))
    supervisor = make_preparation_supervisor(provider=author, book_client=source_index[0])
    supervisor.create_run(binding)
    published = []
    for _ in range(100):
        step = await supervisor.step(
            binding.source_slug,
            binding.run_id,
            "offline",
            lambda _: True,
            lambda bundle: published.append(bundle) or bundle.bundle_sha256,
        )
        if step.state.status.value in ("ready", "blocked", "failed"):
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.status.value == ("ready" if approved else "blocked"), (
        state.current_stage,
        state.last_safe_error_code,
    )
    assert bool(published) is approved
    assert any(name == "IndependentVisualReviewDecision" for _, name, _ in author.calls)
    assert state.provider_calls_spent == len(author.calls)
