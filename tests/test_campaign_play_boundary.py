"""Focused new-workflow publication, private export and hero-selection boundaries."""

from types import SimpleNamespace

import pytest

from apps.api.services import adventure_manifests, adventures_meta, campaign_sources, manuscript
from apps.api.services.campaign_prep import runtime
from apps.api.services.campaign_prep.store import VERSION, Store, digest
from apps.api.services.prepared_play import PreparedPlayError, require_prepared_play
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, ManifestError, manifest_digest


@pytest.fixture
def published_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    graph = Store("fresh")
    graph.initialize({"source_kind": "ai", "rules_edition": "2014"})
    public = "A silent bell hangs over the square."
    manifest = AdventureManifest(
        source="fresh",
        source_sha256="a" * 64,
        extraction_sha256="b" * 64,
        rules_edition="2014",
        opening_scene_id="event_square",
        scenes=[{"scene_id": "event_square", "title": "The square"}],
        passages=[
            {
                "passage_id": "square_public",
                "page": 1,
                "start": 0,
                "end": len(public),
                "text_sha256": text_digest(public),
                "scene_ids": ["event_square"],
                "audience": "player",
            }
        ],
    )
    manifest.reviewed_by = "independent-final-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    guide = "# The Quiet Bell\n\nPRIVATE: The keeper is hiding at the mill.\n"
    compiled = {"manifest": manifest.model_dump(mode="json"), "guide": guide}
    graph.put("compiled", compiled)
    publication = {
        "compiled_sha256": digest(compiled),
        "manifest_sha256": manifest.reviewed_sha256,
        "index_version": "index-1",
    }
    graph.put("publication", publication)
    graph.update(status="ready")
    (graph.directory / "dm-guide.md").write_text(guide, encoding="utf-8")
    adventures_meta.save_meta(
        "fresh",
        {
            "preparation_workflow": VERSION,
            "private_preparation": True,
            "status": "ready",
            "rules_edition": "2014",
            "title": "The Quiet Bell",
            "manifest": manifest.model_dump(mode="json"),
            "ingestion": {
                "source_sha256": manifest.source_sha256,
                "extraction_sha256": manifest.extraction_sha256,
                "index_version": "index-1",
            },
            "preparation_publication_receipt": publication,
            "job": {"job_id": "1" * 32, "kind": "generation", "state": "completed"},
        },
    )
    return graph


@pytest.mark.parametrize("operation", ["compile", "edit", "publish"])
def test_manual_manifest_paths_cannot_replace_graph_publication(published_graph, operation):
    before = adventures_meta.meta_path("fresh").read_bytes()
    with pytest.raises(ManifestError, match="preparation workflow"):
        if operation == "compile":
            adventure_manifests.compile_draft("fresh")
        elif operation == "edit":
            adventure_manifests.edit_draft(
                "fresh",
                AdventureManifest.model_validate(published_graph.get("compiled")["manifest"]),
            )
        else:
            adventure_manifests.publish_manifest(
                "fresh", reviewed_digest="c" * 64, reviewer="manual"
            )
    assert adventures_meta.meta_path("fresh").read_bytes() == before


def test_play_uses_later_selected_hero_without_preparation_rebinding(published_graph, monkeypatch):
    monkeypatch.setattr(runtime, "private_preparation_ready", lambda *_: True)
    before = published_graph.path.read_bytes()
    require_prepared_play(["fresh"], "2014")  # campaign creation has no selected hero
    for hero in (
        SimpleNamespace(id=17, level=1, race="halfling", character_class="rogue"),
        SimpleNamespace(id=28, level=9, race="elf", character_class="wizard"),
    ):
        require_prepared_play(["fresh"], "2014", character=hero, require_hero=True)
    assert published_graph.path.read_bytes() == before
    with pytest.raises(PreparedPlayError) as caught:
        require_prepared_play(["fresh"], "2014", require_hero=True)
    assert caught.value.readiness_code == "choose_character"
    with pytest.raises(PreparedPlayError) as caught:
        require_prepared_play(["fresh"], "2024", character_id=17, require_hero=True)
    assert caught.value.readiness_code == "rules_edition_mismatch"


def test_graph_dm_guide_exports_exact_published_bytes_offline(
    published_graph, tmp_path, monkeypatch
):
    def no_vector_read(*_):
        raise AssertionError("Private export must not depend on an available vector service")

    monkeypatch.setattr(campaign_sources, "active_index_version", no_vector_read)
    destination = tmp_path / "private-guide.md"
    resolved = manuscript.export_canonical_manuscript("fresh", destination)
    assert destination.read_bytes() == (published_graph.directory / "dm-guide.md").read_bytes()
    assert resolved.index_version == "index-1"
    assert resolved.version == text_digest(published_graph.get("compiled")["guide"])


def test_graph_guide_tampering_and_incomplete_publication_are_rejected(published_graph, tmp_path):
    path = published_graph.directory / "dm-guide.md"
    path.write_text("replaced private guide", encoding="utf-8")
    with pytest.raises(manuscript.ManuscriptUnavailable, match="integrity check"):
        manuscript.export_canonical_manuscript("fresh", tmp_path / "must-not-exist.md")
    assert not (tmp_path / "must-not-exist.md").exists()
    published_graph.update(status="running")
    with pytest.raises(manuscript.ManuscriptUnavailable, match="publication receipt"):
        manuscript.resolve_canonical_manuscript("fresh")


def test_invalid_ready_receipt_is_failed_instead_of_processing_forever(
    published_graph, monkeypatch
):
    monkeypatch.setattr(runtime, "private_preparation_ready", lambda *_: False)
    source = campaign_sources.source_read("fresh")
    assert source.status == "failed"
    assert source.usable_chunks == 0
    assert source.job.state == "failed"
    assert not source.job.retryable
    assert "PRIVATE" not in source.model_dump_json()
    assert "keeper" not in source.model_dump_json()


def test_unavailable_graph_store_has_safe_source_status(published_graph, monkeypatch):
    def unavailable(*_):
        raise ConnectionError("PRIVATE provider prompt at /private/data")

    monkeypatch.setattr(runtime, "private_preparation_ready", unavailable)
    source = campaign_sources.source_read("fresh")
    assert source.status == "unavailable"
    assert "PRIVATE" not in source.model_dump_json()
    assert "/private/data" not in source.model_dump_json()


def test_archived_nonprivate_ingestion_never_advertises_retired_retry(published_graph):
    meta = {
        "status": "failed",
        "job": {
            "job_id": "2" * 32,
            "kind": "ingestion",
            "state": "failed",
            "retryable": True,
            "retained_source_attempt_id": "3" * 32,
        },
    }
    projected = campaign_sources.safe_job_read(meta, "fresh")
    assert projected.retryable is False
    assert projected.action_label is None
    assert projected.failure.retryable is False
    assert projected.failure.recovery_action == "read_history"
    assert "Create a fresh adventure" in projected.explanation


@pytest.mark.asyncio
@pytest.mark.parametrize("reveal_entity", ["ribbon", "mira"])
async def test_compiled_visible_actor_and_earned_clue_use_public_evidence(
    monkeypatch, reveal_entity
):
    from agent.models.adjudication import AdjudicationResult
    from agent.nodes.story_effects import (
        prepare_story_validation_state,
        story_candidates,
        validate_story_effects,
    )
    from apps.api.services.campaign_prep.compile import assemble, derived, make_extraction
    from edgar_core.campaign_semantics import Bundles, Spine
    from edgar_core.manifests import select_scene_context
    from edgar_core.sources import source_citation
    from edgar_core.story import StoryEffect
    from ingestion.manifest import manifest_chunks

    origin = {"kind": "authored"}
    spine = Spine.model_validate(
        {
            "title": "The Quiet Bell",
            "premise": "Find the keeper",
            "setting": "A river town",
            "hook": "The bell is silent",
            "ending": "The keeper is safe",
            "dm_guidance": "Allow creative rescue",
            "chapters": [
                {
                    "id": "ch_bell",
                    "title": "The bell",
                    "brief": "Find the keeper",
                    "source_order": 1,
                    "play_order": 1,
                }
            ],
            "entities": [
                {
                    "id": "square",
                    "kind": "place",
                    "name": "Square",
                    "description": "The village square",
                    "origin": origin,
                },
                {
                    "id": "mira",
                    "kind": "npc",
                    "name": "Mira",
                    "description": "A worried witness",
                    "origin": origin,
                },
                {
                    "id": "ribbon",
                    "kind": "clue",
                    "name": "The mill mark",
                    "description": "A ribbon identifies the mill",
                    "origin": origin,
                },
            ],
        }
    ).model_dump()
    bundles = Bundles.model_validate(
        {
            "chapters": [
                {
                    "chapter_id": "ch_bell",
                    "dm_guidance": "Give useful clues",
                    "goals": [
                        {
                            "id": "keeper_safe",
                            "title": "The keeper is safe",
                            "objective": "Help the keeper",
                            "completion": "Any credible rescue or safe refuge",
                            "event_ids": ["square_event"],
                            "origin": origin,
                        }
                    ],
                    "events": [
                        {
                            "id": "square_event",
                            "title": "The quiet square",
                            "location_id": "square",
                            "participants": ["mira"],
                            "clue_ids": ["ribbon"],
                            "visible_entity_ids": ["square", "mira"],
                            "goal_ids": ["keeper_safe"],
                            "trigger": "The hero arrives",
                            "timing": "Before the festival",
                            "public_description": "Mira waits beside the silent bell. A blue ribbon hangs near the tower.",
                            "dm_notes": "PRIVATE: The keeper is hiding at the mill; the mayor is secretly responsible.",
                            "reveals": [
                                {
                                    "entity_id": reveal_entity,
                                    "public_text": "The ribbon carries the miller's mark.",
                                    "condition": "Inspect the ribbon using any credible approach",
                                }
                            ],
                            "origin": origin,
                        }
                    ],
                }
            ]
        }
    ).model_dump()
    compiled = assemble("fresh", "2014", spine, bundles, [])
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "independent-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = [
        dict(chunk, collection="fresh")
        for chunk in manifest_chunks(make_extraction(compiled), manifest)
    ]

    def retrieve(_manifests, scene, flags, **_):
        selected = select_scene_context(manifest, scene, flags)
        return {
            "adventure_context": [
                c for c in chunks if c["passage_id"] in selected["player_passage_ids"]
            ],
            "planner_context": [
                c for c in chunks if c["passage_id"] in selected["planner_passage_ids"]
            ],
        }

    monkeypatch.setattr("db.vector.retrieval.retrieve_active_scene", retrieve)
    state = {
        "manifest_status": "reviewed",
        "adventure_manifests": {"fresh": manifest.model_dump()},
        "current_scene_id": "square_event",
        "world_flags": {},
        "turn_id": "probe_turn",
        "story_state": {"facts": []},
        "rules_edition": "2014",
        "player_input": "I inspect the ribbon carefully",
        **retrieve({}, "square_event", {}),
    }
    assert "mill" not in " ".join(c["text"] for c in state["adventure_context"])
    candidates = {c["target"]["canonical_id"]: c for c in story_candidates(state)}
    public = state["adventure_context"][0]
    encounter = StoryEffect(
        kind="encounter",
        target=candidates["mira"]["target"],
        status="encountered",
        source_reference_ids=[source_citation(public, "2014")["reference_id"]],
    )
    accepted_actor = validate_story_effects(
        AdjudicationResult(success=True, story_effects=[encounter]), state
    )
    assert len(accepted_actor.accepted_story_effects) == 1
    knowledge_id = derived("square_event", "knowledge_0")
    assert candidates["mira"]["discovery_flag"] is None
    private = next(c for c in state["planner_context"] if knowledge_id in c.get("entity_ids", []))
    discovery = StoryEffect(
        kind="discover",
        target=candidates[knowledge_id]["target"],
        status="discovered",
        source_reference_ids=[source_citation(private, "2014")["reference_id"]],
    )
    result = AdjudicationResult(success=True, story_effects=[discovery])
    assert validate_story_effects(result, state).accepted_story_effects == []
    await prepare_story_validation_state(result, state)
    earned = validate_story_effects(result, state).accepted_story_effects
    assert len(earned) == 1
    assert earned[0].player_text == "The ribbon carries the miller's mark."
    assert "mayor" not in earned[0].player_text
    assert "PRIVATE" not in earned[0].player_text


def test_compiled_discoveries_keep_npc_history_and_later_clue_stages_private():
    from copy import deepcopy

    from test_campaign_graph_v2 import ORIGIN, sample

    from apps.api.services.campaign_prep.compile import assemble, derived
    from apps.api.services.story_state import session_story_flags
    from edgar_core.manifests import select_scene_context

    spine, bundles = sample()
    spine["entities"] += [
        {
            "id": "elin",
            "kind": "npc",
            "name": "Elin",
            "description": "A lamplighter",
            "origin": ORIGIN,
        },
        {
            "id": "ribbon",
            "kind": "clue",
            "name": "The ribbon",
            "description": "An old knot",
            "origin": ORIGIN,
        },
        {
            "id": "place_bridge",
            "kind": "place",
            "name": "Old Bridge",
            "description": "A distant bridge",
            "origin": ORIGIN,
        },
    ]
    opening = bundles["chapters"][0]["events"][0]
    opening.update(
        participants=["elin"],
        clue_ids=["ribbon"],
        visible_entity_ids=["place_square", "place_bridge", "elin", "ribbon"],
        public_description="Elin waits in the square. The old bridge is visible downstream.",
        reveals=[
            {
                "entity_id": "ribbon",
                "public_text": "Elin recognizes the ribbon.",
                "condition": "Ask about the knot.",
            },
            {
                "entity_id": "elin",
                "public_text": "Elin once made a promise to a river spirit.",
                "condition": "Earn Elin's trust.",
            },
        ],
    )
    later = deepcopy(opening)
    later.update(
        id="event_amends",
        title="Making amends",
        visible_entity_ids=["elin"],
        reveals=[
            {
                "entity_id": "ribbon",
                "public_text": "The spirit saved Elin's sister; she broke her promise through shame.",
                "condition": "Earn the full account.",
            }
        ],
    )
    opening["next_event_ids"] = [later["id"]]
    bundles["chapters"][0]["events"].append(later)
    compiled = assemble("fresh", "2014", spine, bundles, [])
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "independent-review"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    public = next(p for p in manifest.passages if p.passage_id == "event_square_public")
    assert public.entity_ids == ["elin", "ribbon"]
    assert public.known_scene_ids == ["event_square"]
    actor = next(e for e in manifest.entities if e.entity_id == "elin")
    assert actor.discovery_flag is None
    early_id = derived("event_square", "knowledge_0")
    history_id = derived("event_square", "knowledge_1")
    later_id = derived("event_amends", "knowledge_0")
    flags = {
        derived(early_id, "discovered"): "discovered",
        # Even stale historical actor-discovery flags cannot unlock new knowledge.
        derived("elin", "discovered"): "discovered",
    }
    selected = select_scene_context(manifest, "event_square", flags)
    assert "event_square_reveal_0" in selected["player_passage_ids"]
    assert "event_square_reveal_1" not in selected["player_passage_ids"]
    assert (
        "event_amends_reveal_0"
        not in select_scene_context(manifest, "event_amends", flags)["player_passage_ids"]
    )
    entities = {e.entity_id: e for e in manifest.entities}
    assert len({entities[k].discovery_flag for k in [early_id, history_id, later_id]}) == 3
    # A partial discovery cannot expose the referenced identity's private name
    # through a generated display title before the full account is earned.
    for key in (early_id, history_id, later_id):
        assert "Elin" not in entities[key].title
        assert "ribbon" not in entities[key].title
    # The existing engine can derive the independent flag from earned facts;
    # no live runtime change or campaign-wide disclosure is needed.
    assert session_story_flags({}, {"fresh": manifest.model_dump()}, flags) == {
        derived("elin", "discovered"): "discovered"
    }


def test_visible_nonparticipant_npc_remains_invalid():
    from test_campaign_graph_v2 import ORIGIN, sample

    from apps.api.services.campaign_prep.compile import assemble
    from apps.api.services.campaign_prep.store import NeedsAttention

    spine, bundles = sample()
    spine["entities"].append(
        {
            "id": "hidden_actor",
            "kind": "npc",
            "name": "A hidden witness",
            "description": "Hidden",
            "origin": ORIGIN,
        }
    )
    bundles["chapters"][0]["events"][0]["visible_entity_ids"] = ["hidden_actor"]
    with pytest.raises(NeedsAttention, match="must participate"):
        assemble("fresh", "2014", spine, bundles, [])
