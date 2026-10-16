"""Edition policy and provenance survive engine resolution and public reload/export."""

import json

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from edgar_core.sources import (
    citations_for,
    eligible_context,
    public_citations,
    source_citation,
)


def passage(**changes):
    return {
        "kind": "rules",
        "collection": "rules_phb",
        "source": "PHB",
        "text": "Climbing uses Athletics.",
        "page": 175,
        "source_pages": [175],
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "rules_edition": "2014",
        **changes,
    }


def test_citation_identity_version_policy_and_private_projection():
    ref = source_citation(passage())
    assert ref["edition_status"] == "compatible"
    changed_policy = source_citation(passage(), "2024")
    assert changed_policy["edition_status"] == "incompatible"
    assert changed_policy["reference_id"] == ref["reference_id"]
    assert source_citation(passage(text="Changed text"))["reference_id"] != ref["reference_id"]
    assert source_citation(passage(source_sha256="c" * 64))["reference_id"] != ref["reference_id"]
    legacy = passage(rules_edition="unspecified", source_sha256=None)
    assert source_citation(legacy)["edition_status"] == "unverified"
    assert eligible_context([passage(), legacy], "2024") == [legacy]
    secret = passage(audience="planner", text="PRIVATE hidden villain", scene_id="private_scene")
    refs = citations_for([passage(), passage(), secret])
    assert len(refs) == 2
    assert public_citations(refs) == [ref]
    named = source_citation(
        passage(audience="player", scene_id="SECRET_ROOM", passage_id="SECRET_CLUE")
    )
    assert "SECRET" not in json.dumps(public_citations([named]))
    assert public_citations([named])[0]["reference_id"] == named["reference_id"]
    assert "PRIVATE" not in json.dumps(refs)  # references never store excerpts


async def test_only_actual_compatible_prompt_ids_can_support_ruling(fake_llm_schema):
    from agent.nodes.rules_adjudicator import adjudicate

    rule, clue = passage(), passage(kind="adventure", audience="player", source="village", page=16)
    private = passage(kind="adventure", audience="planner", source="village", page=17)
    conflicting = passage(rules_edition="2024", text="CONFLICTING RULE")
    refs = citations_for([rule, clue, private, conflicting])
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=False,
        citation_ids=[r["reference_id"] for r in refs] + ["invented"],
        citations=[{"source": "forged"}],
    )
    state = {
        "player_input": "I inspect the clue",
        "parsed_input": ParsedInput(intent="exploration"),
        "rules_edition": "2014",
        "rules_context": [rule, conflicting],
        "adventure_context": [clue],
        "planner_context": [private],
        "manifest_status": "reviewed",
    }
    result = await adjudicate(state)
    assert result.success is True  # engine still owns numeric outcome
    assert result.citation_ids == [r["reference_id"] for r in refs[:3]]
    assert result.citations == refs[:3]
    prompt = fake_llm_schema.calls[-1][1][1]["content"]
    assert "CONFLICTING RULE" not in prompt
    assert all(r["reference_id"] in prompt for r in refs[:3])
    from apps.api.services.turn_runner import public_adjudication

    public = public_adjudication(result)
    assert public["citations"] == refs[:2]
    assert refs[2]["reference_id"] not in json.dumps(public)


def test_reviewed_narrator_uses_public_evidence_without_planner_identifiers():
    from agent.nodes.narrator import build_narrator_prompt

    state = {
        "player_input": "look",
        "rules_edition": "2014",
        "manifest_status": "reviewed",
        "current_scene_id": "SECRET_SCENE",
        "world_flags": {"SECRET_VILLAIN": "alive"},
        "rules_context": [passage(), passage(rules_edition="2024", text="CONFLICT")],
        "adventure_context": [
            passage(kind="adventure", audience="player", text="Visible clue"),
            passage(audience="planner", text="SECRET_PASSAGE"),
        ],
        "planner_context": [passage(audience="planner", text="SECRET_PLANNER")],
    }
    prompt = "\n".join(str(m.content) for m in build_narrator_prompt(state))
    assert "Visible clue" in prompt and "physical pages=175" in prompt
    assert "SECRET" not in prompt and "CONFLICT" not in prompt


async def test_public_turn_reload_keeps_versions_and_ruling_evidence(
    api_client, monkeypatch, fake_llm_turn
):
    import db.postgres.session as sm
    from agent.nodes.world_state_updater import world_state_updater_node
    from apps.api.routers import session as router
    from apps.api.services import turn_runner
    from db.postgres.models import EventLog

    seed = (await api_client.post("/api/seed")).json()
    cid, sid = seed["campaign_id"], seed["session_id"]
    response = await api_client.patch(
        f"/api/campaigns/{cid}", json={"rules_edition": "2024", "source_mode": "homebrew"}
    )
    assert response.status_code == 200 and response.json()["rules_edition"] == "2024"
    assert (
        await api_client.patch(f"/api/campaigns/{cid}", json={"rules_edition": "future"})
    ).status_code == 422
    assert (
        await api_client.patch(f"/api/campaigns/{cid}", json={"rules_edition": None})
    ).status_code == 422
    rule = passage(rules_edition="2024")
    clue = passage(
        kind="adventure", source="village", audience="player", rules_edition="2024", page=16
    )
    secret = passage(
        kind="adventure",
        source="village",
        audience="planner",
        text="PRIVATE villain",
        rules_edition="2024",
        page=17,
    )
    refs = citations_for([rule, clue, secret], "2024")

    async def run(initial, **_kwargs):
        assert initial["rules_edition"] == "2024"
        adj = AdjudicationResult(
            success=True, citations=refs, citation_ids=[r["reference_id"] for r in refs]
        )
        state = {
            **initial,
            "narration": "You find the clue.",
            "rules_context": [rule],
            "adventure_context": [clue, secret],
            "adjudication_result": adj,
        }
        return {**state, **await world_state_updater_node(state)}

    monkeypatch.setattr(router, "run_turn", run)
    monkeypatch.setattr(router, "maybe_update_memory_summary", lambda *args: None)
    response = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": "I inspect"})
    assert response.status_code == 200
    public = response.json()
    assert public["citations"] == refs[:2] and public["adjudication"]["citations"] == refs[:2]
    assert refs[2]["reference_id"] not in response.text and "PRIVATE" not in response.text
    history = (await api_client.get(f"/api/sessions/{sid}/messages")).json()
    assert history[-1]["citations"] == refs[:2]
    assert history[-1]["ruling_citation_ids"] == [r["reference_id"] for r in refs[:2]]
    assert refs[2]["reference_id"] not in json.dumps(history)
    async with sm.async_session_factory() as db:
        event = (
            await db.execute(
                select(EventLog).where(
                    EventLog.session_id == sid, EventLog.event_type == "adjudication"
                )
            )
        ).scalar_one()
        assert event.payload["citations"] == refs  # private exact provenance remains in diagnostics

    # SSE uses the same public projection, independently of the persistence path.
    class Prepared:
        async def astream(self, initial, **kwargs):
            yield ("values", await run(initial))

    monkeypatch.setattr(turn_runner, "prepare_app", Prepared())
    frames = [
        frame
        async for frame in turn_runner.stream_session_turn(
            session_id=sid,
            body_message="I inspect",
            initial_state={"rules_edition": "2024", "source_mode": "homebrew", "session_id": sid},
            debug=False,
        )
    ]
    adjudication = next(frame for frame in frames if frame.startswith("event: adjudication"))
    assert refs[2]["reference_id"] not in adjudication


def test_declared_generation_edition_reaches_outline_and_chapter_prompts():
    from apps.api.services.campaign_generator import _chapter_system, _outline_system

    assert "2024 edition" in _outline_system("small", 3, "2024")
    assert "2024 edition" in _chapter_system("2024")
    assert "2014 edition" in _chapter_system()


async def test_unreviewed_module_turns_stop_before_agent_calls(api_client, monkeypatch):
    from apps.api.routers import session

    seed = (await api_client.post("/api/seed")).json()
    monkeypatch.setattr(session, "run_turn", lambda *args: pytest.fail("unreviewed agent call"))
    for suffix in ("turn", "turn/stream"):
        response = await api_client.post(
            f"/api/sessions/{seed['session_id']}/{suffix}", json={"message": "I inspect"}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "adventure_manifest_review_required"
