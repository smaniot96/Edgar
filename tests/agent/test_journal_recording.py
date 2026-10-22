"""EDG-055 journal recording: what an assembled (AI) adventure turn may commit as knowledge.

Assembled adventures keep each scene's substance in a private DM passage and give the
player only a short public arrival passage. The live DM tends to cite the private one.
"""

import pytest

from agent.models.adjudication import AdjudicationResult
from agent.nodes.story_effects import prepare_story_validation_state, validate_story_effects
from apps.api.services.opening_journal import opening_journal_effects
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryFact, StoryRef, player_story_effect, story_target_id

QUAY_PUBLIC = "Festival boats bump against the quay. Orin Dale waves you over."
QUAY_DM = "Orin knows the slips point to the East Sluice House. Secret: Marr ordered it."
TOWPATH_PUBLIC = "The towpath is slick with fresh silt."
TOWPATH_DM = "Trigger: the heroes follow the crew. Marr's agents watch from the far bank."


def _manifest() -> AdventureManifest:
    def passage(passage_id, page, text, scene, audience, entities=(), known=()):
        return {
            "passage_id": passage_id,
            "page": page,
            "start": 0,
            "end": len(text),
            "text_sha256": text_digest(text),
            "scene_ids": [scene],
            "entity_ids": list(entities),
            "known_scene_ids": list(known),
            "audience": audience,
        }

    manifest = AdventureManifest.model_validate(
        {
            "source": "generated",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "quay",
            "scenes": [
                {"scene_id": "quay", "title": "The Waterline Falls", "links": [{"scene_id": "towpath"}]},
                {"scene_id": "towpath", "title": "A Current Without a Chart"},
            ],
            "entities": [
                {"entity_id": "orin_dale", "kind": "actor", "title": "Orin Dale"},
                {"entity_id": "dispatch_slips", "kind": "clue", "title": "Festival Dispatch Fragment"},
            ],
            "passages": [
                passage("quay_public", 1, QUAY_PUBLIC, "quay", "player", ["orin_dale", "dispatch_slips"], ["quay"]),
                passage("quay_dm", 1, QUAY_DM, "quay", "planner", ["orin_dale", "dispatch_slips"]),
                passage("towpath_public", 2, TOWPATH_PUBLIC, "towpath", "player", [], ["towpath"]),
                passage("towpath_dm", 2, TOWPATH_DM, "towpath", "planner"),
            ],
            "reviewed_by": "fixture",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return manifest


def _chunk(manifest, passage_id, text, audience, scene):
    return {
        "collection": "ai_campaign",
        "source": "ai_campaign",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "manifest_sha256": manifest.reviewed_sha256,
        "rules_edition": "2014",
        "kind": "adventure",
        "page": 1 if scene == "quay" else 2,
        "scene_id": scene,
        "passage_id": passage_id,
        "text": text,
        "audience": audience,
    }


@pytest.fixture
def play():
    manifest = _manifest()
    chunks = {
        "quay_public": _chunk(manifest, "quay_public", QUAY_PUBLIC, "player", "quay"),
        "quay_dm": _chunk(manifest, "quay_dm", QUAY_DM, "planner", "quay"),
        "towpath_public": _chunk(manifest, "towpath_public", TOWPATH_PUBLIC, "player", "towpath"),
        "towpath_dm": _chunk(manifest, "towpath_dm", TOWPATH_DM, "planner", "towpath"),
    }
    state = {
        "manifest_status": "reviewed",
        "adventure_manifests": {"ai_campaign": manifest.model_dump()},
        "current_scene_id": "quay",
        "world_flags": {},
        "turn_id": "turn-2",
        "story_state": {"facts": []},
        "adventure_context": [chunks["quay_public"]],
        "planner_context": [chunks["quay_dm"]],
        "rules_edition": "2014",
        "player_input": "I ask Orin what the dispatch slips say.",
    }
    return manifest, chunks, state


def _ref(manifest, kind, canonical_id):
    return StoryRef(
        collection="ai_campaign",
        source_sha256="a" * 64,
        manifest_sha256=manifest.reviewed_sha256,
        kind=kind,
        canonical_id=canonical_id,
    )


def _cite(chunk):
    return source_citation(chunk, "2014")["reference_id"]


def test_a_met_person_cited_from_private_notes_becomes_an_encounter_with_public_text(play):
    manifest, chunks, state = play
    result = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(
                    kind="discover",
                    target=_ref(manifest, "entity", "orin_dale"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                )
            ],
        ),
        state,
    )

    [effect] = result.accepted_story_effects
    assert (effect.kind, effect.status) == ("encounter", "encountered")
    assert effect.player_text == QUAY_PUBLIC
    public = player_story_effect(effect)
    assert public is not None and public["name"] == "Orin Dale"
    assert "Marr" not in (effect.player_text or "")


def test_a_clue_cited_from_private_notes_quotes_only_the_scene_player_text(play):
    manifest, chunks, state = play
    result = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(
                    kind="discover",
                    target=_ref(manifest, "entity", "dispatch_slips"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                )
            ],
        ),
        state,
    )

    [effect] = result.accepted_story_effects
    assert effect.kind == "discover"
    assert effect.player_text == QUAY_PUBLIC
    assert "East Sluice House" not in effect.player_text
    # A failed attempt still earns nothing.
    failed = validate_story_effects(
        AdjudicationResult(
            success=False,
            story_effects=[
                StoryEffect(
                    kind="discover",
                    target=_ref(manifest, "entity", "dispatch_slips"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                )
            ],
        ),
        state,
    )
    assert failed.accepted_story_effects == []


def test_rediscovering_a_committed_clue_is_ignored_instead_of_conflicting(play):
    manifest, chunks, state = play
    target = _ref(manifest, "entity", "dispatch_slips")
    first = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[StoryEffect(kind="discover", target=target, source_reference_ids=[_cite(chunks["quay_dm"])])],
        ),
        state,
    ).accepted_story_effects[0]
    state["story_state"] = {
        "facts": [StoryFact(**first.model_dump(), fact_id=story_target_id(target), event_id=1).model_dump()]
    }
    again = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(kind="discover", target=target, source_reference_ids=[_cite(chunks["quay_public"])])
            ],
        ),
        state,
    )
    assert again.accepted_story_effects == []


def test_an_event_transition_records_the_destination_arrival_text(play):
    manifest, chunks, state = play
    state["parser_scene_context"] = {"event_transitions": [{"scene_id": "towpath"}]}
    state["story_arrival_context"] = [chunks["towpath_public"], chunks["towpath_dm"]]
    result = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(
                    kind="move",
                    status="visited",
                    target=_ref(manifest, "scene", "towpath"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                    achievement_basis="The hero follows the rescued crew along the towpath.",
                )
            ],
        ),
        state,
    )

    [effect] = result.accepted_story_effects
    assert effect.evidence_kind == "dm_adjudication"
    assert effect.player_text == TOWPATH_PUBLIC
    public = player_story_effect(effect)
    assert public is not None and public["text"] == TOWPATH_PUBLIC
    assert "Marr" not in str(public)
    assert result.scene_id == "towpath"


def test_an_event_transition_without_arrival_text_stays_private(play):
    manifest, chunks, state = play
    state["parser_scene_context"] = {"event_transitions": [{"scene_id": "towpath"}]}
    # Only private destination text is available: the move commits, nothing is quoted.
    state["story_arrival_context"] = [chunks["towpath_dm"]]
    [effect] = validate_story_effects(
        AdjudicationResult(
            success=True,
            story_effects=[
                StoryEffect(
                    kind="move",
                    status="visited",
                    target=_ref(manifest, "scene", "towpath"),
                    source_reference_ids=[_cite(chunks["quay_dm"])],
                    achievement_basis="The hero follows the rescued crew along the towpath.",
                )
            ],
        ),
        state,
    ).accepted_story_effects
    assert effect.player_text is None
    assert player_story_effect(effect) is None


async def test_prepare_loads_only_player_arrival_spans_for_the_linked_move(play, monkeypatch):
    manifest, chunks, state = play
    calls = []

    def fake_retrieve(manifests, scene_id, flags, **kwargs):
        calls.append(scene_id)
        return {"adventure_context": [chunks["towpath_public"], chunks["towpath_dm"]]}

    import db.vector.retrieval as retrieval

    monkeypatch.setattr(retrieval, "retrieve_active_scene", fake_retrieve)
    adj = AdjudicationResult(
        success=True,
        story_effects=[
            StoryEffect(
                kind="move",
                status="visited",
                target=_ref(manifest, "scene", "towpath"),
                source_reference_ids=[_cite(chunks["quay_dm"])],
            )
        ],
    )
    await prepare_story_validation_state(adj, state)
    assert calls == ["towpath"]
    assert state["story_arrival_context"] == [chunks["towpath_public"]]

    # No move, or a failed read: nothing to quote.
    await prepare_story_validation_state(AdjudicationResult(success=True), state)
    assert state["story_arrival_context"] == []

    def broken(*args, **kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(retrieval, "retrieve_active_scene", broken)
    await prepare_story_validation_state(adj, state)
    assert state["story_arrival_context"] == []


def test_the_opening_scene_is_recorded_as_visited_from_its_arrival_text(play):
    manifest, chunks, _ = play
    effects = opening_journal_effects(
        "ai_campaign",
        manifest,
        [chunks["quay_public"], chunks["quay_dm"]],
        "2014",
    )
    [visit] = effects
    assert (visit.kind, visit.status, visit.target.canonical_id) == ("visit", "visited", "quay")
    assert visit.player_text == QUAY_PUBLIC

    # Without player arrival text the opening simply has no default visit.
    assert opening_journal_effects("ai_campaign", manifest, [chunks["quay_dm"]], "2014") == []
