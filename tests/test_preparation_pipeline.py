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
    ResponseUsage,
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
        "max_cost_microusd": 200_000_000,
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


def _blueprint_content(binding, design, fragments):
    from apps.api.services.preparation_blueprint import source_spans
    from apps.api.services.preparation_content import extraction_for_design

    spans = source_spans(extraction_for_design(binding, design))
    scenes, facts = [], []
    for index, act in enumerate(design.acts, 1):
        pages = set(act.source_pages if binding.source_mode == "pdf" else [index])
        sources = [span.id for span in spans if span.page in pages]
        fragment = next(fragment for fragment in fragments if fragment.act_id == act.act_id)
        for scene in fragment.scenes:
            scenes.append({"id": scene.scene_id, "title": scene.title, "sources": sources,
                           "public": sources[:1], "notes": CHAPTER,
                           "links": [{"to": link.scene_id} for link in scene.links]})
        facts.extend({"key": key, "kind": "objective", "title": "Allow the stranded villagers aboard",
                      "completion_condition": "The villagers are safely aboard the crossing ferry.",
                      "scenes": act.scene_ids, "sources": sources[1:2] or sources[:1]}
                     for key in act.advance_ids)
    facts.append({"key": "crossing_ending", "kind": "ending", "title": "The crossing remains possible",
                  "scenes": design.acts[-1].scene_ids, "sources": [spans[-1].id]})
    return {"scenes": scenes, "facts": facts,
            "resource_plan": "This social crossing requires no mandatory resource expenditure or rest.",
            "progression_plan": "The actual hero stays at the commissioned level without invented XP or abilities."}


class OfflineAuthor:
    def __init__(self, *, criticize_once=False, content=None):
        self.calls = []
        self.criticize_once = criticize_once
        self.critic_count = 0
        self.reviewed_chapters = set()
        self.content = content

    async def __call__(self, binding, task, schema, *, instruction, evidence, idempotency_key):
        from apps.api.services.preparation_blueprint import CampaignBlueprint
        from apps.api.services.preparation_corrections import ProposalCorrection
        from apps.api.services.preparation_stages import BookSelection, CriticObservations

        design, fragment = _design_and_fragment(binding.source_slug)
        fragments = [fragment]
        if self.content:
            design, fragments = self.content
        self.calls.append((task.stage.value, schema.__name__, idempotency_key))
        if schema is BookSelection:
            data = {"requests": [{"role": role, "references": headings[:1]}
                                 for role, headings in evidence["headings"].items()]}
        elif schema is CampaignDesign:
            data = design.model_dump()
        elif schema is CampaignBlueprint:
            data = _blueprint_content(binding, design, fragments)
        elif schema is ProposalCorrection:
            import json

            data = {"base_proposal_sha256": evidence["base_proposal_sha256"],
                    "edits": [{"op": "replace", "path": "/scenes/0/notes",
                               "value_json": json.dumps(evidence["previous_proposal"]["scenes"][0]["notes"]
                                   + " The keeper waits because his neighbours depend on the crossing.")} ]}
        elif schema is CriticObservations:
            from edgar_core.artifacts import text_digest

            reviewed = evidence["reviewed_chapters"]
            assert reviewed and all(chapter["text"] for chapter in reviewed)
            self.reviewed_chapters.update(chapter["chapter_id"] for chapter in reviewed)
            self.critic_count += 1
            findings = []
            if self.criticize_once and self.critic_count == 1:
                excerpt = reviewed[0]["text"][:80]
                findings = [{"finding_id": "unclear_keeper_motive", "code": "unclear_keeper_motive",
                             "severity": "high", "message": "Clarify why the keeper waits for the villagers.",
                             "affected_ids": [design.acts[0].act_id], "evidence": [{
                                 "artifact_id": evidence["manuscript_artifact_id"],
                                 "artifact_sha256": evidence["manuscript_artifact_sha256"],
                                 "json_pointer": reviewed[0]["text_json_pointer"],
                                 "excerpt": excerpt, "excerpt_sha256": text_digest(excerpt)}]}]
            data = {"reviewed_chapter_ids": [chapter["chapter_id"] for chapter in reviewed],
                    "findings": findings}
        elif schema.__name__ == "SourceStudy":
            data = {"page_numbers": [p["page"] for p in evidence["pages"]], "summary": SEED,
                    "commitments": ["Preserve the exact printed crossing and its quotation."]}
        else:
            pytest.fail(f"Unexpected schema {schema}")
        return schema.model_validate(deepcopy(data)), ProviderUsage(
            provider_called=True, call_count=1, input_tokens=100, output_tokens=100,
            cost_microusd=2400, requested_model="gpt-6.1-sol", reasoning_effort="high",
            task_version="offline-author", responses=(ResponseUsage(
                response_id=f"resp-offline-{len(self.calls)}", input_tokens=100,
                output_tokens=100, cost_microusd=2400),))


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
    commission = {**binding.commission, "act_count": count, "max_cost_microusd": 1_000_000_000}
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
    assert len(author.reviewed_chapters) == count
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
            return await super().__call__(binding, task, schema, **kwargs)

    from apps.api.services import preparation_stages
    from edgar_core.assets import AssetRequirement

    original_requirements = preparation_stages.asset_requirements
    printed_map = AssetRequirement.model_validate({
        "asset_id": "printed_map", "kind": "source_map", "required": True,
        "act_id": "act_one_0", "scene_ids": ["dock_0"], "source_crop": {
            "collection": binding.source_slug, "source_sha256": binding.source_sha256,
            "extraction_sha256": binding.extraction_sha256, "book_title": "Synthetic crossing",
            "edition": "2014", "page": 1, "crop_id": "blank_corner",
            "crop": {"x": 0.7, "y": 0.7, "width": 0.2, "height": 0.2}}})
    monkeypatch.setattr(preparation_stages, "asset_requirements",
                        lambda artifacts, manifest: [*original_requirements(artifacts, manifest), printed_map])

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


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_rounds", [0, 1])
async def test_joined_repair_estimate_does_not_block_and_preserves_archive_integrity(
    tmp_path, monkeypatch, source_index, repair_rounds  # noqa: F811
):
    import json

    from apps.api.services import adventure_assets
    from apps.api.services.preparation_store import PreparationStoreError, preparation_path

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(adventure_assets, "ASSET_ROOT", tmp_path / "assets")
    author = OfflineAuthor(criticize_once=True)
    supervisor = make_preparation_supervisor(provider=author, book_client=source_index[0])
    binding = _binding(rounds=repair_rounds)
    supervisor.create_run(binding)
    published = []
    for _ in range(100):
        result = await supervisor.step(binding.source_slug, binding.run_id, "offline", lambda _: True,
                                       lambda bundle: published.append(bundle) or bundle.bundle_sha256)
        if result.state.status.value in {"blocked", "failed", "ready"}:
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.status.value == "ready", (state.current_stage, state.last_safe_error_code)
    assert len(published) == 1
    assert state.repair_rounds == 1
    assert sum(stage == "P02" for stage, _, _ in author.calls) == 1
    assert sum(stage == "P03" for stage, _, _ in author.calls) == 1
    assert state.provider_calls_spent == len(author.calls)
    assert state.cost_microusd_spent == 2400 * len(author.calls)
    assert len(state.repair_history) == 1
    if state.repair_history:
        path = preparation_path(binding.source_slug, binding.run_id)
        checkpoint = json.loads(path.read_text())
        checkpoint["repair_history"][0]["findings"][0]["message"] = "tampered archive"
        path.write_text(json.dumps(checkpoint))
        with pytest.raises(PreparationStoreError, match="Repair evidence"):
            supervisor.store.load_run(binding.source_slug, binding.run_id)


@pytest.mark.asyncio
async def test_joined_commissioned_images_use_frozen_budget_and_real_asset_path(
    tmp_path, monkeypatch, source_index  # noqa: F811
):
    import base64
    import io
    import json

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from PIL import Image

    from apps.api.services import adventure_assets as assets
    from edgar_core.assets import IndependentVisualReviewDecision
    from edgar_core.model_profiles import effective_image_profile

    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    monkeypatch.setattr(assets, "ASSET_ROOT", tmp_path / "assets")
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PRIVATE_KEY", base64.b64encode(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )).decode())
    monkeypatch.setenv("EDGAR_VISUAL_REVIEW_PUBLIC_KEY", base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )).decode())
    binding = _binding()
    profiles = load_model_profiles()
    profile = effective_image_profile("image.illustration", profiles)
    monkeypatch.setenv("EDGAR_TRUSTED_IMAGE_PRICE_RECORD", json.dumps({
        "revision": "offline-fixture-price", "model": profile.model,
        "size": profile.size, "quality": profile.quality, "unit_cost_microusd": 250_000,
    }))
    frozen = assets.commissioned_image_budget(
        profile_snapshot=binding.profile_snapshot, max_image_calls=1,
        max_image_cost_microusd=250_000,
    )
    commission = {**binding.commission, "max_image_calls": 1,
                  "max_image_cost_microusd": 250_000, "image_budget": frozen}
    binding = binding.model_copy(update={
        "commission": commission, "commission_sha256": canonical_sha256(commission),
        "budget": binding.budget.model_copy(update={"max_cost_microusd": 199_750_000}),
    })
    image_calls = []

    class OfflineImageProvider:
        async def generate(self, prompt, effective):
            assert effective.to_dict() == frozen["effective_profile"]
            image_calls.append(prompt)
            data = io.BytesIO()
            Image.new("RGB", (64, 64), color="navy").save(data, format="PNG")
            return assets.GeneratedImage(data=data.getvalue(), media_type="image/png",
                                         cost_microusd=100_000)

    monkeypatch.setattr(assets, "OpenAIImageProvider", OfflineImageProvider)

    class IllustratedAuthor(OfflineAuthor):
        async def __call__(self, binding, task, schema, *, images=(), **kwargs):
            if schema is IndependentVisualReviewDecision:
                assert images[0][0].startswith(b"\x89PNG")
                self.calls.append((task.stage.value, schema.__name__, kwargs["idempotency_key"]))
                candidate = kwargs["evidence"]["candidate"]
                return schema(candidate_id=candidate["candidate_id"],
                              artifact_sha256=candidate["artifact_sha256"],
                              decision="approved", reasons=["Synthetic fixture pixels."]), ProviderUsage(
                    provider_called=True, call_count=1, input_tokens=100, output_tokens=100,
                    cost_microusd=2400, task_version="offline-visual",
                )
            return await super().__call__(binding, task, schema, **kwargs)

    from apps.api.services import preparation_stages
    from edgar_core.assets import AssetRequirement

    original_requirements = preparation_stages.asset_requirements
    decoration = AssetRequirement.model_validate({
        "asset_id": "dock_decoration", "kind": "generated_illustration", "required": False,
        "act_id": "act_one", "scene_ids": ["dock"],
        "prompt": "A plain blue evening sky over the ferry dock, with no labels."})
    monkeypatch.setattr(preparation_stages, "asset_requirements",
                        lambda artifacts, manifest: [*original_requirements(artifacts, manifest), decoration])

    author = IllustratedAuthor()
    supervisor = make_preparation_supervisor(provider=author, book_client=source_index[0])
    supervisor.create_run(binding)
    published = []
    for _ in range(100):
        step = await supervisor.step(binding.source_slug, binding.run_id, "offline", lambda _: True,
                                     lambda bundle: published.append(bundle) or bundle.bundle_sha256)
        if step.state.status.value in {"ready", "blocked", "failed"}:
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.status.value == "ready", (state.current_stage, state.last_safe_error_code)
    assert len(published) == 1 and len(image_calls) == 1
    assert state.provider_calls_spent == len(author.calls)
    receipt = next(a for a in state.artifacts.values() if a.artifact_kind == "asset_coverage").payload["receipt"]
    assert receipt["image_calls_reserved"] == 1
    assert receipt["image_cost_succeeded_microusd"] == 100_000
    assert receipt["image_cost_refunded_microusd"] == 150_000
    usage = assets.image_usage(collection=binding.source_slug, source_sha256="0" * 64,
                               manifest_sha256=receipt["manifest_sha256"],
                               prepared_sha256=receipt["prepared_sha256"], run_id=binding.run_id)
    assert usage["reserved"] == 1 and usage["reserved_cost_microusd"] == 100_000
    assert state.cost_microusd_spent + usage["reserved_cost_microusd"] <= commission["max_cost_microusd"]


@pytest.mark.asyncio
async def test_design_rejection_is_retained_and_corrected_without_repeating_studies(
    tmp_path, monkeypatch, source_index,  # noqa: F811
):
    import json

    client, _ = source_index
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    author = OfflineAuthor()
    design_calls = []
    all_calls = []

    async def reject_once(binding, task, schema, **kwargs):
        all_calls.append(task.task_id)
        result, usage = await author(binding, task, schema, **kwargs)
        if schema is CampaignDesign:
            design_calls.append(kwargs)
            if len(design_calls) == 1:
                result = result.model_copy(update={"opening_scene_id": "unplanned_opening"})
            else:
                assert kwargs["evidence"]["previous_rejected_proposal"]["opening_scene_id"] == "unplanned_opening"
                assert "opening" in kwargs["evidence"]["code_validation_feedback"][0]["message"]
        return result, usage

    supervisor = make_preparation_supervisor(provider=reject_once, book_client=client)
    binding = _binding()
    supervisor.create_run(binding)
    for _ in range(12):
        step = await supervisor.step(binding.source_slug, binding.run_id, "correction-test", lambda _: True)
        if step.state.tasks.get("P03:design_campaign") and step.state.tasks["P03:design_campaign"].status.value == "accepted":
            break
    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    assert state.tasks["P03:design_campaign"].status.value == "accepted"
    assert all_calls.count("study_books") == 1
    assert len(design_calls) == 2
    assert design_calls[0]["idempotency_key"] != design_calls[1]["idempotency_key"]
    assert state.provider_calls_spent == 3
    assert state.tokens_spent == 600
    files = list((tmp_path / "adventures").rglob("proposals/*/*.json"))
    rejected = [json.loads(p.read_text()) for p in files if json.loads(p.read_text())["diagnostics"]]
    assert len(rejected) == 1
    assert rejected[0]["proposal"]["opening_scene_id"] == "unplanned_opening"
