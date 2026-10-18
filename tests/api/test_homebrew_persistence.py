"""EDG-169: homebrew consequences through the real turn route, commit guard and module parity.

Model output is scripted with the fake provider. Everything else (validator, persist_turn,
story ledger, NPC sync, journal projection) is the production code against Postgres.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.resolution import CheckResult
from apps.api.services.journal import journal_tracking_marker
from apps.api.services.journal_identity import actor_entry_id
from apps.api.services.story_state import (
    StoryStateConflict,
    load_story_state,
    persist_story_effects,
)
from db.postgres.models import NPC, EventLog, Session as SessionModel
from edgar_core.story import (
    StoryEffect,
    StoryState,
    accept_story_effect,
    homebrew_target,
    story_target_id,
)
from tests.api.test_story_turn_effects import story_source  # noqa: F401  (module-mode fixture)

PERSON = {
    "kind": "person",
    "name": "Brother Aldo",
    "text": "Brother Aldo, the lamp-keeper at the gate, says the bell has been silent since dusk.",
}
PLACE = {
    "kind": "place",
    "name": "Bell Tower",
    "text": "The Bell Tower rises above the chapel roof, its rope cut clean at the top.",
}
CLUE = {
    "kind": "clue",
    "name": "Wax drips",
    "text": "Wax drips down the cut rope: fresh candle wax, so someone held a flame while cutting.",
}


def _forced_success(_character, parsed: ParsedInput, **_kwargs):
    if parsed.check is None:
        return None, []
    return (
        CheckResult(
            check=parsed.check,
            kind="skill",
            ability="wis",
            modifier=1,
            dc=10,
            against="DC",
            roll_expression="1d20+1",
            natural=15,
            total=16,
            success=True,
            dice_log=[],
        ),
        [],
    )


@pytest.fixture
def homebrew_turns(api_client, fake_llm_schema, monkeypatch):
    """A seeded campaign switched to explicit homebrew play, plus a turn helper."""
    monkeypatch.setattr("agent.nodes.rules_adjudicator.resolve_from_parsed", _forced_success)
    fake_llm_schema.narration = "The DM narrates."

    async def start():
        seed = (await api_client.post("/api/seed")).json()
        response = await api_client.patch(
            f"/api/campaigns/{seed['campaign_id']}", json={"source_mode": "homebrew"}
        )
        assert response.status_code == 200, response.text
        return seed

    async def turn(sid, proposals, *, check=None, message="I look around."):
        fake_llm_schema.responses[ParsedInput] = ParsedInput(
            intent="exploration", check=check, difficulty="easy"
        )
        fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult.model_validate(
            {"success": True, "homebrew_proposals": proposals, "mechanical_summary": "ok"}
        )
        response = await api_client.post(f"/api/sessions/{sid}/turn", json={"message": message})
        assert response.status_code == 200, response.text
        return response.json()

    return start, turn


@pytest.mark.asyncio
async def test_homebrew_facts_persist_reload_and_project_to_npcs_and_journal(
    api_client, db_session, homebrew_turns
) -> None:
    import db.postgres.session as sm

    start, turn = homebrew_turns
    seed = await start()
    sid, cid = seed["session_id"], seed["campaign_id"]

    await turn(sid, [PERSON])
    await turn(sid, [PLACE])
    await turn(sid, [CLUE], check="insight")

    # (a) facts persist in the ledger and survive a fresh database session (reload).
    async with sm.async_session_factory() as fresh:
        ledger = await load_story_state(fresh, sid, cid)
    facts = {f["display_name"]: f for f in ledger["facts"]}
    assert set(facts) == {"Brother Aldo", "Bell Tower", "Wax drips"}
    assert all(f["target"]["collection"] == "homebrew" for f in facts.values())
    assert all(f["knowledge_kind"] is None for f in facts.values())

    # (b) the person gets an NPC row via _sync_npc; place and clue create none.
    person_fact_id = story_target_id(homebrew_target("person", "Brother Aldo"))
    npcs = (await db_session.execute(select(NPC).where(NPC.session_id == sid))).scalars().all()
    assert [(n.name, n.story_identity) for n in npcs] == [("Brother Aldo", person_fact_id)]
    listed = await api_client.get(f"/api/campaigns/{cid}/npcs?session_id={sid}")
    assert listed.status_code == 200
    assert "Brother Aldo" in [n["name"] for n in listed.json()]

    # (c) the journal shows homebrew entries with provenance and no module citation.
    journal = (await api_client.get(f"/api/sessions/{sid}/journal")).json()
    by_title = {e["title"]: e for e in journal["entries"]}
    assert set(by_title) == {"Brother Aldo", "Bell Tower", "Wax drips"}
    assert by_title["Brother Aldo"]["category"] == "person"
    assert by_title["Bell Tower"]["category"] == "place"
    assert by_title["Wax drips"]["category"] == "clue"
    for entry in journal["entries"]:
        assert entry["origin"]["provenance"] == "homebrew"
        assert entry["origin"]["citation"] is None
    assert by_title["Brother Aldo"]["actor_id"] is not None
    assert by_title["Brother Aldo"]["actor_id"] == actor_entry_id(
        sid, homebrew_target("person", "Brother Aldo")
    )
    body = str((await api_client.get(f"/api/sessions/{sid}/journal")).text)
    assert "source_sha256" not in body and "homebrew-source" not in body.lower()


@pytest.mark.asyncio
async def test_commit_guard_refuses_a_reused_earned_name_without_partial_writes(
    api_client, db_session, homebrew_turns
) -> None:
    import db.postgres.session as sm

    start, turn = homebrew_turns
    seed = await start()
    sid, cid = seed["session_id"], seed["campaign_id"]
    await turn(sid, [PERSON])

    # The validator would already refuse a reused name, so a duplicate can only reach commit
    # through a stale or bypassed proposal. The commit-time guard must still refuse it.
    game_session = await db_session.get(SessionModel, sid)
    event = EventLog(session_id=sid, event_type="narration", payload=journal_tracking_marker())
    db_session.add(event)
    await db_session.flush()
    fresh_person = accept_story_effect(
        StoryEffect(
            kind="encounter",
            target=homebrew_target("person", "Sister Venn"),
            status="encountered",
            source_reference_ids=[],
            player_text="Sister Venn steps from the roof's shadow.",
        ),
        turn_id="turn-guard",
        source_refs=[],
        display_name="Sister Venn",
    )
    reused = accept_story_effect(
        StoryEffect(
            kind="learn_place",
            target=homebrew_target("place", "Brother Aldo"),
            status="known",
            source_reference_ids=[],
            player_text="Brother Aldo is also a stone wall.",
        ),
        turn_id="turn-guard",
        source_refs=[],
        display_name="Brother Aldo",
    )
    before = await load_story_state(db_session, sid, cid)
    before_npcs = len(
        (await db_session.execute(select(NPC).where(NPC.session_id == sid))).scalars().all()
    )
    with pytest.raises(StoryStateConflict, match="homebrew name"):
        await persist_story_effects(
            db_session, game_session, [fresh_person, reused], event_id=event.id
        )
    await db_session.rollback()
    assert await load_story_state(db_session, sid, cid) == before
    after_npcs = len(
        (await db_session.execute(select(NPC).where(NPC.session_id == sid))).scalars().all()
    )
    assert after_npcs == before_npcs

    # The same guard runs in the real route: a reused name is simply not accepted there.
    await turn(sid, [{**PERSON, "text": "Brother Aldo returns to the gate and nods."}])
    async with sm.async_session_factory() as fresh:
        assert await load_story_state(fresh, sid, cid) == before


@pytest.mark.asyncio
async def test_module_session_ignores_homebrew_proposals_and_still_persists(
    story_source, fake_llm_schema, api_client, monkeypatch  # noqa: F811
) -> None:
    """Module parity: the persist path is unchanged and improvised proposals never commit."""
    import db.postgres.session as sm

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult.model_validate(
        {"success": True, "homebrew_proposals": [PERSON, PLACE]}
    )
    fake_llm_schema.narration = "The module narrates."
    seed = (await api_client.post("/api/seed")).json()
    response = await api_client.post(
        f"/api/sessions/{seed['session_id']}/turn", json={"message": "I look around."}
    )
    assert response.status_code == 200, response.text
    assert response.json()["narration"] == "The module narrates."
    async with sm.async_session_factory() as fresh:
        ledger = await load_story_state(fresh, seed["session_id"], seed["campaign_id"])
    assert all(f["target"]["collection"] != "homebrew" for f in ledger["facts"])
    assert StoryState.model_validate(ledger).facts == []
    journal = (await api_client.get(f"/api/sessions/{seed['session_id']}/journal")).json()
    assert all(e["origin"]["provenance"] == "module" for e in journal["entries"])
    assert "Brother Aldo" not in str(journal)
