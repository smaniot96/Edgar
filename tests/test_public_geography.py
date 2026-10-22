"""Physical places group earned events without disclosing prepared event titles."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.api.services.journal import JournalPreparedLabels, project_player_journal
from edgar_core.assets import project_player_map
from edgar_core.public_places import public_place_label
from tests.test_assets import _fact, _fixture
from tests.test_journal_projection import _event, _fact as journal_fact


def test_descriptive_location_labels_quote_only_public_physical_phrases():
    assert public_place_label("A narrow track leaves the road.", "Secret ambush") == "Narrow track"
    assert (
        public_place_label("Inside the central platform, shelves crowd the walls.")
        == "Central platform"
    )
    assert (
        public_place_label("The commotion subsides.", "The hidden dungeon") == "Unidentified place"
    )
    assert public_place_label("Clattering hooves and frantic bleating.") == "Unidentified place"


def test_shared_geography_collapses_events_and_keeps_current_area_and_external_routes():
    manifest = _fixture()
    manifest.scenes[1].links = [
        manifest.scenes[0].links[0].model_copy(update={"scene_id": "vault_sentinel"})
    ]
    facts = [
        _fact(manifest, scene, kind)
        for scene, kind in (
            ("harbor", "visit"),
            ("tower", "learn_place"),
            ("vault_sentinel", "visit"),
        )
    ]
    for fact, text in zip(
        facts,
        (
            "A market town.",
            "A narrow track leaves the road.",
            "Inside the central platform, shelves crowd the walls.",
        ),
        strict=True,
    ):
        fact.player_text = text
        fact.source_refs = [
            {
                "audience": "player",
                "reference_id": "d" * 64,
                "collection": "synthetic-map",
                "source_sha256": manifest.source_sha256,
                "extraction_sha256": "e" * 64,
                "page": 1,
                "manifest_sha256": manifest.reviewed_sha256,
            }
        ]
    result = project_player_map(
        session_id=7,
        collection="synthetic-map",
        manifest=manifest,
        facts=facts,
        world_flags={},
        current_scene_id="vault_sentinel",
        scene_places={
            "harbor": {"place_id": "town", "names": ["Forbidden royal town"]},
            "tower": {"place_id": "compound", "names": ["Secret killer tower"]},
            "vault_sentinel": {"place_id": "compound", "names": ["Secret killer tower"]},
        },
    )
    assert len(result.places) == 2
    assert result.places[1].scene_ids == ["tower", "vault_sentinel"]
    assert result.places[1].visited_scene_ids == ["vault_sentinel"]
    assert result.places[1].scene_id == "vault_sentinel"
    assert result.places[1].title == "Central platform"
    assert result.places[1].current_area == "Central platform"
    assert len(result.routes) == 1
    assert result.routes[0].to_scene_id == "vault_sentinel"
    assert "Secret killer" not in result.model_dump_json()
    assert "Forbidden royal" not in result.model_dump_json()
    assert result.places[1].detail == "Inside the central platform, shelves crowd the walls."


def test_journal_groups_physical_places_and_earns_names_from_delivered_narration():
    now = datetime.now(UTC)
    first = journal_fact(
        kind="visit",
        canonical_id="arrival",
        event_id=1,
        turn_id="a",
        status="visited",
        text="A sheep approaches.",
    )
    later = journal_fact(
        kind="visit",
        canonical_id="aftermath",
        event_id=2,
        turn_id="b",
        status="visited",
        text="The sheep stays close.",
    )
    event = _event(1, 7, "a", now)
    event.payload["narration"] = "You are in the market town. A sheep approaches."
    labels = JournalPreparedLabels(
        scene_places={
            ("synthetic", "arrival"): {
                "place_id": "town",
                "names": ["Secret capital", "market town"],
            },
            ("synthetic", "aftermath"): {
                "place_id": "town",
                "names": ["Secret capital", "market town"],
            },
        }
    )
    result = project_player_journal(
        7,
        {"facts": [first.model_dump(), later.model_dump()]},
        [event, _event(2, 7, "b", now + timedelta(seconds=1))],
        current_scene_id="aftermath",
        prepared=labels,
    )
    places = [entry for entry in result.entries if entry.category == "place"]
    assert len(places) == 1
    assert result.current_place.id == places[0].id
    assert places[0].title == "market town"
    assert places[0].text == "A sheep approaches."
    assert len(places[0].history) == 2
    assert "Secret capital" not in result.model_dump_json()


def test_departure_keeps_old_threat_in_history_but_removes_it_as_current_lead():
    now = datetime.now(UTC)
    facts = [
        journal_fact(
            kind="visit",
            canonical_id="town",
            event_id=1,
            turn_id="a",
            status="visited",
            text="The town.",
        ),
        journal_fact(
            kind="discover",
            canonical_id="threat",
            event_id=1,
            turn_id="a",
            text="Pursuers approach.",
        ),
        journal_fact(
            kind="visit",
            canonical_id="trail",
            event_id=2,
            turn_id="b",
            status="visited",
            text="A narrow trail.",
        ),
    ]
    result = project_player_journal(
        7,
        {"facts": [fact.model_dump() for fact in facts]},
        [_event(1, 7, "a", now), _event(2, 7, "b", now + timedelta(seconds=1))],
    )
    assert result.current_lead is None
    assert any(entry.text == "Pursuers approach." for entry in result.entries)


def test_retained_geography_requires_the_exact_published_registry_receipt(tmp_path, monkeypatch):
    from apps.api.services.campaign_prep import runtime
    from apps.api.services.campaign_prep.store import digest

    (tmp_path / "graph.sqlite3").touch()
    compiled = {
        "manifest": {"source_sha256": "a" * 64},
        "manifest_sha256": "b" * 64,
        "registry": {
            "event_one": {"kind": "event", "value": {"location_id": "town"}},
            "town": {
                "kind": "entity",
                "value": {"kind": "place", "name": "Private Town", "aliases": []},
            },
        },
    }
    artifacts = {"compiled": compiled, "publication": {"compiled_sha256": digest(compiled)}}
    monkeypatch.setattr(runtime.meta_store, "adventure_path", lambda *_: tmp_path)
    monkeypatch.setattr(
        runtime,
        "Store",
        lambda _: SimpleNamespace(
            state=lambda: {"status": "ready"}, get=lambda key: artifacts[key]
        ),
    )
    manifest = SimpleNamespace(source_sha256="a" * 64, reviewed_sha256="b" * 64)
    assert runtime.published_scene_places("synthetic", manifest)["event_one"]["place_id"] == "town"
    compiled["registry"]["town"]["value"]["name"] = "Changed without review"
    assert runtime.published_scene_places("synthetic", manifest) == {}


def test_stale_place_metadata_cannot_regroup_older_facts():
    from apps.api.services.journal import place_fact_identity

    fact = journal_fact(
        kind="visit",
        canonical_id="arrival",
        event_id=1,
        turn_id="a",
        status="visited",
        text="A sheep approaches.",
    )
    labels = JournalPreparedLabels(
        scene_places={
            ("synthetic", "arrival"): {
                "place_id": "changed_town",
                "names": [],
                "manifest_sha256": "c" * 64,
            },
        }
    )
    assert place_fact_identity(fact, labels) == fact.fact_id


@pytest.mark.asyncio
async def test_grouping_a_known_event_with_current_area_does_not_reveal_hidden_world_route(
    monkeypatch,
):
    from apps.api.routers.session_assets import _player_world_map
    from apps.api.services.campaign_prep import runtime
    from edgar_core.assets import PlayerMapProjection

    world = {
        "title": "Earned region",
        "places": [
            {
                "place_id": "yard",
                "label": "Yard",
                "x": 10,
                "y": 10,
                "symbol": "building",
                "public": False,
            },
            {
                "place_id": "vault",
                "label": "Vault",
                "x": 20,
                "y": 20,
                "symbol": "building",
                "public": False,
            },
        ],
        "routes": [
            {"from_place_id": "yard", "to_place_id": "vault", "kind": "tunnel", "hidden": True}
        ],
        "scene_places": {"current_event": "yard", "known_event": "vault"},
    }
    monkeypatch.setattr(runtime, "published_world_map", lambda _: world)
    projection = PlayerMapProjection(
        session_id=7,
        source_version="a" * 64,
        topology="reviewed_manifest",
        current_scene_id="current_event",
        routes=[],
        places=[
            {
                "scene_id": "current_event",
                "scene_ids": ["current_event", "known_event"],
                "visited_scene_ids": ["current_event"],
                "title": "Compound",
                "status": "current",
            }
        ],
    )
    result = await _player_world_map("synthetic", projection)
    assert {place.place_id for place in result.places} == {"yard", "vault"}
    assert next(place for place in result.places if place.place_id == "vault").status == "known"
    assert result.routes == []
