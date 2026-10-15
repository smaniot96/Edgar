"""Reviewed disclosure decisions are independent, committed and fail closed."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from agent.models.adjudication import AdjudicationResult
from agent.nodes.story_effects import validate_story_effects
from agent.resolution_policies import describe_policy_result
from apps.api.services.opening_journal import opening_journal_effects
from edgar_core.artifacts import text_digest
from edgar_core.manifests import (
    AdventureManifest,
    ManifestError,
    manifest_digest,
    require_published,
)
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryRef, accept_story_effect, player_story_effect


def reviewed_contract(*, clock_reveal=None):
    texts = {
        "opening": "Hall. The tower lies beyond the river. Seek the tower later.",
        "hint": "Hint: reflections show the safe path.",
        "clock": "Water is rising.",
        "private": "Private ending: the duke betrays everyone.",
    }
    manifest = AdventureManifest.model_validate(
        {
            "source": "fixture.pdf",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "opening_scene_id": "hall",
            "scenes": [
                {"scene_id": "hall", "title": "Hall"},
                {"scene_id": "tower", "title": "tower"},
            ],
            "entities": [{"entity_id": "help", "kind": "hint", "title": "Hint"}],
            "objectives": [
                {
                    "canonical_id": "seek",
                    "title": "Seek",
                    "scene_ids": ["hall"],
                    "passage_ids": ["opening"],
                    "statuses": ["announced", "active", "completed", "failed"],
                    "announcement_passage_ids": ["opening"],
                }
            ],
            "clocks": [
                {
                    "canonical_id": "water",
                    "title": "Water",
                    "scene_ids": ["hall"],
                    "passage_ids": ["clock"],
                    "statuses": ["advanced", "completed"],
                    "clock_max": 5,
                    "clock_reveal": clock_reveal,
                }
            ],
            "passages": [
                dict(
                    passage_id=pid,
                    page=1,
                    start=i * 100,
                    end=i * 100 + len(text),
                    text_sha256=text_digest(text),
                    scene_ids=["hall"],
                    entity_ids=["help"] if pid == "hint" else [],
                    audience="planner" if pid == "private" else "player",
                    known_scene_ids=["tower"] if pid == "opening" else [],
                )
                for i, (pid, text) in enumerate(texts.items())
            ],
            "opening_journal": [
                {
                    "kind": "visit",
                    "target_id": "hall",
                    "status": "visited",
                    "passage_ids": ["opening"],
                },
                {
                    "kind": "learn_place",
                    "target_id": "tower",
                    "status": "known",
                    "passage_ids": ["opening"],
                },
                {
                    "kind": "objective",
                    "target_id": "seek",
                    "status": "announced",
                    "passage_ids": ["opening"],
                },
            ],
            "reviewed_by": "fixture reviewer",
        }
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = [
        dict(
            collection="fixture",
            source="fixture.pdf",
            source_sha256="a" * 64,
            extraction_sha256="b" * 64,
            manifest_sha256=manifest.reviewed_sha256,
            rules_edition="2014",
            kind="adventure",
            page=1,
            passage_id=p.passage_id,
            audience=p.audience,
            text=texts[p.passage_id],
            scene_id="hall",
        )
        for p in manifest.passages
    ]
    state = dict(
        manifest_status="reviewed",
        adventure_manifests={"fixture": manifest.model_dump()},
        current_scene_id="hall",
        world_flags={},
        turn_id="turn-1",
        story_state={"facts": []},
        rules_edition="2014",
        adventure_context=[c for c in chunks if c["audience"] == "player"],
        planner_context=[c for c in chunks if c["audience"] == "planner"],
    )
    return manifest, chunks, state


def proposal(manifest, chunks, kind, target_kind, target, status, pid, quantity=None):
    return StoryEffect(
        kind=kind,
        target=StoryRef(
            collection="fixture",
            source_sha256=manifest.source_sha256,
            manifest_sha256=manifest.reviewed_sha256,
            kind=target_kind,
            canonical_id=target,
        ),
        status=status,
        source_reference_ids=[
            source_citation(c, "2014")["reference_id"] for c in chunks if c["passage_id"] == pid
        ],
        quantity=quantity,
    )


def accepted(state, effect):
    return validate_story_effects(
        AdjudicationResult(success=True, story_effects=[effect]), state
    ).accepted_story_effects


def test_announced_goals_require_explicit_announcement_and_exact_public_evidence():
    manifest, chunks, state = reviewed_contract()
    effect = proposal(manifest, chunks, "objective", "objective", "seek", "announced", "opening")
    assert accepted(state, effect)[0].player_text == chunks[0]["text"]
    edited = manifest.model_dump()
    edited["objectives"][0]["announcement_passage_ids"] = []
    edited["opening_journal"] = []
    edited = AdventureManifest.model_validate(edited)
    edited.reviewed_sha256 = manifest_digest(edited)
    state["adventure_manifests"]["fixture"] = edited.model_dump()
    for c in chunks:
        c["manifest_sha256"] = edited.reviewed_sha256
    effect = proposal(edited, chunks, "objective", "objective", "seek", "announced", "opening")
    assert accepted(state, effect) == []


def test_hint_type_is_stamped_from_reviewed_definition_never_model_metadata():
    manifest, chunks, state = reviewed_contract()
    effect = proposal(manifest, chunks, "discover", "entity", "help", "discovered", "hint")
    result = accepted(state, effect)[0]
    assert result.knowledge_kind == "hint"
    assert result.player_text == "Hint: reflections show the safe path."
    with pytest.raises(ValidationError):
        StoryEffect.model_validate({**effect.model_dump(), "knowledge_kind": "hint"})
    state["adventure_context"] = []
    assert accepted(state, effect) == []


def test_knowing_place_cannot_visit_or_authorize_movement():
    manifest, chunks, state = reviewed_contract()
    known = proposal(manifest, chunks, "learn_place", "scene", "tower", "known", "opening")
    assert accepted(state, known)[0].status == "known"
    assert state["current_scene_id"] == "hall"
    for kind in ["visit", "move"]:
        effect = proposal(manifest, chunks, kind, "scene", "tower", "visited", "opening")
        assert accepted(state, effect) == []
    unsupported = proposal(manifest, chunks, "learn_place", "scene", "tower", "known", "clock")
    assert accepted(state, unsupported) == []


@pytest.mark.parametrize(
    "reveal,flags,expected",
    [
        (None, {}, (None, None, "advanced")),
        ({"current_requires": {}}, {}, (5, None, "advanced")),
        ({"maximum_requires": {}}, {}, (None, 5, "advanced")),
        ({"completion_requires": {}}, {}, (None, None, "completed")),
        (
            {"current_requires": {"water_scale": "discovered"}, "maximum_requires": {}},
            {},
            (None, 5, "advanced"),
        ),
        (
            {"current_requires": {"water_scale": "discovered"}},
            {"water_scale": "discovered"},
            (5, None, "advanced"),
        ),
    ],
)
def test_clock_fields_disclose_independently_and_summary_uses_same_boundary(
    reveal, flags, expected
):
    manifest, chunks, state = reviewed_contract(clock_reveal=reveal)
    state["world_flags"] = flags
    effect = proposal(manifest, chunks, "clock", "clock", "water", "completed", "clock", quantity=5)
    result = accepted(state, effect)[0]
    public = player_story_effect(result)
    assert (public["quantity"], public["clock_maximum"], public["status"]) == expected
    summary = describe_policy_result(True, [result], [])
    if expected[0] is None:
        assert "5 intervals" not in summary
    if expected[1] is None:
        assert "maximum 5" not in summary
    if expected[2] == "advanced":
        assert "completed" not in summary


def test_disclosure_is_bound_to_sealed_content_identity():
    manifest, chunks, state = reviewed_contract(clock_reveal={"current_requires": {}})
    result = accepted(
        state, proposal(manifest, chunks, "clock", "clock", "water", "completed", "clock", 5)
    )[0]
    bare = StoryEffect.model_validate(result.model_dump(include=set(StoryEffect.model_fields)))
    assert (
        accept_story_effect(bare, turn_id=result.turn_id, source_refs=result.source_refs).effect_id
        != result.effect_id
    )
    assert (
        accept_story_effect(
            bare,
            turn_id=result.turn_id,
            source_refs=result.source_refs,
            clock_disclosure=result.clock_disclosure,
        ).effect_id
        == result.effect_id
    )


def test_opening_seeds_validate_exact_source_and_do_not_include_private_plan():
    manifest, chunks, _ = reviewed_contract()
    results = opening_journal_effects("fixture", manifest, chunks, "2014")
    assert [(e.kind, e.status) for e in results] == [
        ("visit", "visited"),
        ("learn_place", "known"),
        ("objective", "announced"),
    ]
    assert all(e.player_text == chunks[0]["text"] for e in results)
    assert "betrays" not in str([e.player_text for e in results])
    for mutation in ["missing", "text", "version"]:
        broken = deepcopy(chunks)
        if mutation == "missing":
            broken = broken[1:]
        elif mutation == "text":
            broken[0]["text"] = "fabricated opening"
        else:
            broken[0]["manifest_sha256"] = "e" * 64
        with pytest.raises(ManifestError):
            opening_journal_effects("fixture", manifest, broken, "2014")


@pytest.mark.parametrize(
    "change", ["private", "gated", "wrong_place", "unreviewed", "unknown_passage"]
)
def test_seed_edits_cannot_silently_publish_private_or_unreviewed_knowledge(change):
    manifest, _, _ = reviewed_contract()
    data = manifest.model_dump()
    if change == "private":
        data["opening_journal"][0]["passage_ids"] = ["private"]
    elif change == "gated":
        data["passages"][0]["requires"] = {"secret": "discovered"}
    elif change == "wrong_place":
        data["opening_journal"][0]["target_id"] = "tower"
    elif change == "unknown_passage":
        data["opening_journal"][0]["passage_ids"] = ["missing"]
    else:
        data["reviewed_sha256"] = None
    with pytest.raises((ValueError, ManifestError)):
        require_published(data)


def test_legacy_review_receipt_survives_absent_extensions():
    import json
    from pathlib import Path

    value = json.loads(
        (Path(__file__).parent / "fixtures/adventures/legacy_journal_manifest.json").read_text()
    )
    assert (
        require_published(value).reviewed_sha256
        == "58438804363438afe3883c59dc7928061bbf99e8a3eaafcfd5d916f324217368"
    )


async def test_opening_seed_commit_reload_retry_and_direct_player_api(
    db_session, api_client, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from langchain_core.messages import AIMessage

    from apps.api.services import session_intro
    from apps.api.services.story_state import load_story_state
    from db.postgres.models import Campaign, Session, User

    manifest, chunks, _ = reviewed_contract()
    seeds = opening_journal_effects("fixture", manifest, chunks, "2014")
    user = User(email="opening-contract@example.test")
    db_session.add(user)
    await db_session.flush()
    campaign = Campaign(
        title="Reviewed opening", system="5e", created_by=user.id, adventure_collections=["fixture"]
    )
    db_session.add(campaign)
    await db_session.flush()
    session = Session(campaign_id=campaign.id)
    other = Session(campaign_id=campaign.id)
    db_session.add_all([session, other])
    await db_session.commit()
    model = SimpleNamespace(
        ainvoke=AsyncMock(return_value=AIMessage(content="Hall. What do you do?"))
    )
    monkeypatch.setattr(session_intro, "require_campaign_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(session_intro, "_reviewed_opening", lambda *_: ("hall", chunks, seeds))
    monkeypatch.setattr(session_intro, "make_chat_model", lambda **_: model)
    result = await api_client.post(f"/api/sessions/{session.id}/intro")
    assert result.status_code == 200 and result.json()["started"] is True
    state = await load_story_state(db_session, session.id, campaign.id)
    assert len(state["facts"]) == 3
    assert all(f["event_id"] is not None for f in state["facts"])
    again = await api_client.post(f"/api/sessions/{session.id}/intro")
    assert again.json() == {"started": False}
    assert model.ainvoke.await_count == 1
    assert await load_story_state(db_session, session.id, campaign.id) == state
    journal = await api_client.get(f"/api/sessions/{session.id}/journal")
    assert journal.status_code == 200
    data = journal.json()
    assert sorted(e["status"] for e in data["entries"]) == ["announced", "known", "visited"]
    assert data["current_place"]["status"] == "visited"
    assert data["active_goal"] is None
    assert (await api_client.get(f"/api/sessions/{other.id}/journal")).json()["entries"] == []
    exported = await api_client.get(f"/api/sessions/{session.id}/journal/export")
    assert "betrays" not in exported.text and "private" not in exported.text
    assert (await api_client.get(f"/api/sessions/{session.id}/journal?q=betrays")).json()[
        "entries"
    ] == []
