"""Known-only map projection tests: hidden topology never reaches SVG or browser payloads."""


from edgar_core.artifacts import text_digest
from edgar_core.assets import project_player_map, render_player_map_svg
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


def test_player_map_uses_session_owned_source_binding_and_known_place_statuses():
    manifest = _fixture()
    other = _fact(manifest, "tower", "visit").model_copy(
        update={"target": _fact(manifest, "tower", "visit").target.model_copy(update={"collection": "other-source"})}
    )
    projection = project_player_map(
        session_id=9,
        collection="synthetic-map",
        manifest=manifest,
        facts=[_fact(manifest, "harbor", "visit"), other],
        world_flags={},
        current_scene_id=None,
    )

    assert [(place.scene_id, place.status) for place in projection.places] == [("harbor", "visited")]
    assert "Sentinel" not in render_player_map_svg(projection).decode()

