"""Known-only map projection tests: hidden topology never reaches SVG or browser payloads."""

from edgar_core.artifacts import text_digest
from edgar_core.assets import (
    project_player_map,
    render_player_map_svg,
)
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


def _fixture():
    text = "A safe opening."
    manifest = AdventureManifest.model_validate(
        {
            "source": "synthetic-map",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "harbor",
            "scenes": [
                {
                    "scene_id": "harbor",
                    "title": "The harbor",
                    "links": [{"scene_id": "tower"}, {"scene_id": "vault_sentinel"}],
                },
                {"scene_id": "tower", "title": "The watchtower"},
                {"scene_id": "vault_sentinel", "title": "Sentinel’s hidden vault"},
            ],
            "passages": [
                {
                    "passage_id": "opening",
                    "page": 1,
                    "start": 0,
                    "end": len(text),
                    "text_sha256": text_digest(text),
                    "scene_ids": ["harbor"],
                    "audience": "player",
                }
            ],
            "reviewed_by": "synthetic fixture",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return manifest


def _fact(manifest, scene_id: str, kind: str) -> StoryFact:
    target = StoryRef(
        collection="synthetic-map",
        source_sha256=manifest.source_sha256,
        manifest_sha256=manifest.reviewed_sha256,
        kind="scene",
        canonical_id=scene_id,
    )
    effect = accept_story_effect(
        StoryEffect(
            kind=kind,
            target=target,
            status="visited" if kind == "visit" else "known",
        ),
        turn_id=f"turn-{scene_id}-{kind}",
        source_refs=[],
        display_name=scene_id.replace("_", " ").title(),
    )
    return StoryFact(**effect.model_dump(), fact_id=story_target_id(target), event_id=1)


def test_player_map_filters_unknown_topology_and_renders_only_committed_places():
    manifest = _fixture()
    projection = project_player_map(
        session_id=7,
        collection="synthetic-map",
        manifest=manifest,
        facts=[_fact(manifest, "harbor", "visit"), _fact(manifest, "tower", "learn_place")],
        world_flags={},
        current_scene_id="harbor",
    )

    assert [(place.scene_id, place.status) for place in projection.places] == [
        ("harbor", "current"),
        ("tower", "known"),
    ]
    assert [(route.from_scene_id, route.to_scene_id) for route in projection.routes] == [
        ("harbor", "tower")
    ]
    public = projection.model_dump_json() + render_player_map_svg(projection).decode()
    assert "vault_sentinel" not in public
    assert "Sentinel" not in public

    earlier_projection = project_player_map(
        session_id=7,
        collection="synthetic-map",
        manifest=manifest,
        facts=[_fact(manifest, "harbor", "visit")],
        world_flags={},
        current_scene_id="harbor",
    )
    assert earlier_projection.source_version != projection.source_version


def test_player_map_uses_session_owned_source_binding_and_known_place_statuses():
    manifest = _fixture()
    other = _fact(manifest, "tower", "visit").model_copy(
        update={
            "target": _fact(manifest, "tower", "visit").target.model_copy(
                update={"collection": "other-source"}
            )
        }
    )
    projection = project_player_map(
        session_id=9,
        collection="synthetic-map",
        manifest=manifest,
        facts=[_fact(manifest, "harbor", "visit"), other],
        world_flags={},
        current_scene_id=None,
    )

    assert [(place.scene_id, place.status) for place in projection.places] == [
        ("harbor", "visited")
    ]
    assert "Sentinel" not in render_player_map_svg(projection).decode()


def test_prepared_map_requires_reviewed_route_and_current_link_predicates():
    from tests.test_prepared_campaign import _fixture as prepared_fixture

    manifest, prepared = prepared_fixture()
    # The archived engine schema requires discoverable gates and a source-backed ending.
    # Build a complete retained fixture for this projection check.
    from edgar_core.manifests import Entity

    manifest.entities = [
        Entity(entity_id="gate", kind="clue", title="Open gate", discovery_flag="gate_open"),
        Entity(entity_id="safe", kind="ending", title="The keeper is safe"),
    ]
    manifest.passages[0].entity_ids = ["gate", "safe"]
    manifest.scenes[0].links[0].requires = {"gate_open": "discovered"}
    manifest.resolution_policies[0].requires = {"gate_open": "discovered"}
    prepared.routes[0].traversal_requires = {"gate_open": "discovered"}
    manifest.prepared = prepared
    manifest.reviewed_sha256 = manifest_digest(manifest)
    fact = _fact(manifest, "dock", "visit")

    hidden = project_player_map(
        session_id=17,
        collection="synthetic-map",
        manifest=manifest,
        facts=[fact],
        world_flags={},
        current_scene_id="dock",
    )
    shown = project_player_map(
        session_id=17,
        collection="synthetic-map",
        manifest=manifest,
        facts=[fact],
        world_flags={"gate_open": "discovered"},
        current_scene_id="dock",
    )

    assert hidden.routes == []
    assert [(route.from_scene_id, route.to_scene_id) for route in shown.routes] == [
        ("dock", "dock")
    ]


def test_invalid_prepared_topology_never_falls_back_to_manifest_links():
    from tests.test_prepared_campaign import _fixture as prepared_fixture

    manifest, prepared = prepared_fixture()
    broken_route = prepared.routes[0].model_copy(update={"to_scene_id": "secret_room"})
    manifest.prepared = prepared.model_copy(update={"routes": [broken_route]})
    manifest.reviewed_sha256 = manifest_digest(manifest)
    projection = project_player_map(
        session_id=18,
        collection="synthetic-map",
        manifest=manifest,
        facts=[_fact(manifest, "dock", "visit")],
        world_flags={"gate_open": "yes"},
        current_scene_id="dock",
    )

    assert projection.topology == "unavailable"
    assert projection.routes == []
