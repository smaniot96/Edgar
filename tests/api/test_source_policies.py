"""Independent source-policy acceptance through real sync/SSE and disposable persistence."""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from sqlalchemy import select, update

from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.models.parsed_input import ParsedInput
from db.postgres.models import CharacterAssignment, EventLog, Session as SessionModel
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from tools.dice import use_rng

NOTE = "The lens tracks reflections, not the sky."
SECRET = "PRIVATE POLICY BRANCH: VILLAIN AT MIDNIGHT"


class LowDice(random.Random):
    def randint(self, a, b):
        return a


@pytest.fixture
def policy_source(monkeypatch, api_client):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS
    from db.vector.embeddings import VECTOR_SIZE
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.manifest import manifest_chunks
    from ingestion.quality import check_text

    spans = {
        "approaches": "Mara offers a public comparison of the ledger. A Lens note lies by the Brace.",
        "note": NOTE,
        "time": "Time spent: another interval passes while you compare the marks.",
        "gear": "Rope can tether the Brace, providing a stable observation point.",
        "private": SECRET,
    }
    text = "\n".join(spans.values())
    extraction = Extraction(
        "Reviewed lens fixture.pdf",
        "a" * 64,
        1,
        [ExtractedPage(1, text, "generated", check_text(text))],
    )
    passages, offset = [], 0
    for identity, value in spans.items():
        passage = {
            "passage_id": identity,
            "page": 1,
            "start": offset,
            "end": offset + len(value),
            "text_sha256": text_digest(value),
            "scene_ids": ["room"],
            "audience": "planner" if identity == "private" else "player",
        }
        if identity in {"approaches", "note"}:
            passage["entity_ids"] = (
                ["lens_note", "mara"] if identity == "approaches" else ["lens_note"]
            )
        if identity == "note":
            passage["requires"] = {"note_known": "discovered"}
        passages.append(passage)
        offset += len(value) + 1

    def effect(kind, target, status, passage, **extra):
        return {
            "kind": kind,
            "target_id": target,
            "status": status,
            "passage_ids": [passage],
            **extra,
        }

    discover = effect("discover", "lens_note", "discovered", "note")
    common = {"scene_ids": ["room"], "passage_ids": ["approaches"]}
    manifest = AdventureManifest(
        source="Reviewed lens fixture.pdf",
        source_sha256="a" * 64,
        extraction_sha256=extraction.text_sha256(),
        rules_edition="2014",
        opening_scene_id="room",
        scenes=[{"scene_id": "room", "title": "Room"}],
        passages=passages,
        entities=[
            {
                "entity_id": "lens_note",
                "kind": "clue",
                "title": "Lens note",
                "discovery_flag": "note_known",
                "reveal_on": "always",
            },
            {"entity_id": "mara", "kind": "actor", "title": "Mara"},
        ],
        objects=[
            {
                "canonical_id": "rope",
                "title": "Rope",
                "scene_ids": ["room"],
                "passage_ids": ["gear"],
                "operations": ["tether"],
                "initial_owner": "character",
                "statuses": ["used", "consumed"],
                "consumable": True,
            },
            {
                "canonical_id": "brace",
                "title": "Brace",
                "scene_ids": ["room"],
                "passage_ids": ["gear"],
                "operations": ["secure"],
                "portable": False,
                "statuses": ["changed"],
            },
        ],
        clocks=[
            {
                "canonical_id": "time_spent",
                "title": "Time spent",
                "scene_ids": ["room"],
                "passage_ids": ["time"],
                "statuses": ["advanced", "completed"],
                "clock_max": 10,
            }
        ],
        resolution_policies=[
            {
                **common,
                "policy_id": "compare",
                "action": "Compare the ledger",
                "mode": "check",
                "check": "investigation",
                "dc": 13,
                "failure_mode": "essential_reveal",
                "on_success": [discover],
                "on_failure": [
                    discover,
                    effect("clock", "time_spent", "advanced", "time", quantity_delta=1),
                ],
            },
            {
                **common,
                "policy_id": "ask_mara",
                "action": "Accept Mara's public comparison",
                "mode": "automatic",
                "on_automatic": [
                    discover,
                    effect("relationship", "mara", "friendly", "approaches"),
                ],
            },
            {
                **common,
                "passage_ids": ["approaches", "gear"],
                "policy_id": "tether",
                "action": "Tether the Brace with Rope",
                "requires_items": ["Rope"],
                "mode": "automatic",
                "on_automatic": [
                    effect("use", "rope", "used", "gear", operation="tether", destination="brace"),
                    effect("environment", "brace", "changed", "gear", operation="secure"),
                    discover,
                ],
            },
            {
                **common,
                "passage_ids": ["approaches", "gear"],
                "policy_id": "resource_cost",
                "action": "Risk Rope to inspect the marks",
                "mode": "check",
                "check": "investigation",
                "dc": 13,
                "failure_mode": "essential_reveal",
                "on_failure": [discover, effect("consume", "rope", "consumed", "gear")],
            },
            {
                **common,
                "policy_id": "locked",
                "action": "Inspect the sealed archive",
                "requires": {"archive_open": "true"},
                "mode": "automatic",
                "on_automatic": [discover],
            },
        ],
        reviewed_by="independent synthetic acceptance",
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    manifests = {}
    q = QdrantClient(":memory:")
    for slug in DEFAULT_ADVENTURE_COLLECTIONS:
        manifests[slug] = manifest.model_dump()
        q.create_collection(
            slug, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        q.upsert(
            slug,
            [
                PointStruct(id=i + 1, vector=[1.0] + [0.0] * (VECTOR_SIZE - 1), payload=c)
                for i, c in enumerate(manifest_chunks(extraction, manifest))
            ],
        )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda slugs: {s: manifests[s] for s in slugs},
    )
    monkeypatch.setattr("db.vector.retrieval.get_qdrant_client", lambda: q)
    monkeypatch.setattr(
        "agent.nodes.world_retriever.inspect_source",
        lambda slug, **kw: {"slug": slug, "status": "ready", "usable_chunks": 5},
    )
    monkeypatch.setattr("agent.nodes.world_retriever.search_rules_context", lambda *a, **kw: [])
    yield next(iter(manifests)), manifests
    q.close()


def _frames(text):
    return [
        (f.splitlines()[0][7:], json.loads(f.splitlines()[1][6:]))
        for f in text.strip().split("\n\n")
    ]


async def _turn(client, sid, stream, message):
    response = await client.post(
        f"/api/sessions/{sid}/turn{'/stream' if stream else ''}",
        json={"message": message, "debug": True},
    )
    assert response.status_code == 200, response.text
    assert SECRET not in response.text
    assert "resolution_policy_id" not in response.text
    assert "recall_fact_id" not in response.text
    if stream:
        frames = _frames(response.text)
        assert frames[-1][0] == "done", response.text
        return frames[-1][1], next(data for name, data in frames if name == "adjudication")
    value = response.json()
    return value, value["adjudication"]


async def _ledger(sid):
    import db.postgres.session as sm
    from apps.api.services.story_state import load_story_state

    async with sm.async_session_factory() as db:
        return (await load_story_state(db, sid))["facts"]


@pytest.mark.parametrize("stream", [False, True])
async def test_failure_streak_exact_seven_turn_reread_and_isolation(
    policy_source, fake_llm_schema, api_client, monkeypatch, stream
):
    import db.postgres.session as sm

    slug, _ = policy_source
    seed = (await api_client.post("/api/seed")).json()
    sid = seed["session_id"]
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True,
        mechanical_summary="Invented victory",
        character_update=CharacterUpdate(hp_delta=50),
    )
    fake_llm_schema.narration = "You cannot remember. The incorrect note says: moon twice."
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration",
        check="investigation",
        difficulty="medium",
        resolution_policy_id=f"{slug}:compare",
    )
    with use_rng(LowDice()):
        for attempt in range(3):
            value, adj = await _turn(api_client, sid, stream, "I compare the ledger.")
            assert adj["dc"] == 13 and adj["success"] is False
            assert NOTE in value["narration"]
            assert "cannot remember" not in value["narration"]
            facts = await _ledger(sid)
            assert max(f["quantity"] for f in facts if f["kind"] == "clock") == attempt + 1
            assert len([f for f in facts if f["kind"] == "discover"]) == 1
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="rp")
    for _ in range(7):
        await _turn(api_client, sid, stream, "I wait and consider another route.")
    before = await _ledger(sid)
    value, adj = await _turn(api_client, sid, stream, "Reread the Lens note.")
    assert adj["check"] is None and adj["dice_result"] is None
    assert NOTE in value["narration"] and "Reviewed lens fixture.pdf" in value["narration"]
    assert "moon twice" not in value["narration"] and "cannot remember" not in value["narration"]
    assert adj["citations"] and all(r["audience"] == "player" for r in adj["citations"])
    assert await _ledger(sid) == before
    history = await api_client.get(f"/api/sessions/{sid}/messages")
    assert history.status_code == 200 and SECRET not in history.text
    assert history.json()[-1]["content"] == value["narration"]
    async with sm.async_session_factory() as db, db.begin():
        other = SessionModel(campaign_id=seed["campaign_id"])
        db.add(other)
        await db.flush()
        other_sid = other.id
    assert await _ledger(other_sid) == []
    other_history = await api_client.get(f"/api/sessions/{other_sid}/messages")
    assert NOTE not in other_history.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("method", ["tether", "ask_mara"])
async def test_gear_and_social_alternatives_are_durable(
    policy_source, fake_llm_schema, api_client, stream, method
):
    import db.postgres.session as sm

    slug, _ = policy_source
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db, db.begin():
        await db.execute(update(CharacterAssignment).values(inventory={"items": ["Rope"]}))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration",
        check="athletics",
        difficulty="very_hard",
        resolution_policy_id=f"{slug}:{method}",
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=False, mechanical_summary="You fail and learn nothing."
    )
    fake_llm_schema.narration = "You fail and learn nothing."
    value, adj = await _turn(
        api_client,
        sid,
        stream,
        "I tether the Brace with Rope."
        if method == "tether"
        else "I accept Mara's public comparison.",
    )
    assert adj["success"] and adj["check"] is None
    assert NOTE in value["narration"] and "learn nothing" not in value["narration"]
    kinds = {f["kind"] for f in await _ledger(sid)}
    assert (
        {"use", "environment", "discover"} <= kinds
        if method == "tether"
        else {"relationship", "discover"} <= kinds
    )
    count = len(await _ledger(sid))
    await _turn(api_client, sid, stream, "I repeat that supported approach.")
    assert len(await _ledger(sid)) == count


@pytest.mark.parametrize("policy", ["tether", "locked", "unknown"])
async def test_missing_gear_prerequisite_or_unknown_policy_does_not_roll(
    policy_source, fake_llm_schema, api_client, policy
):
    slug, _ = policy_source
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration", check="investigation", resolution_policy_id=f"{slug}:{policy}"
    )
    value, adj = await _turn(api_client, sid, False, "I attempt that approach.")
    assert not adj["success"] and adj["check"] is None
    assert "not available" in value["narration"] and await _ledger(sid) == []


@pytest.mark.parametrize("cost", ["compare", "resource_cost"])
async def test_failed_policy_replay_is_atomic_and_cost_applies_once(
    policy_source, fake_llm_schema, api_client, cost
):
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services.turn_runner import persist_turn, run_turn

    slug, _ = policy_source
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db, db.begin():
        await db.execute(update(CharacterAssignment).values(inventory={"items": ["Rope"]}))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration", resolution_policy_id=f"{slug}:{cost}"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I inspect the marks.", db)
    with use_rng(LowDice()):
        result = await run_turn(initial)
    assert len(result["adjudication_result"].accepted_story_effects) == 2
    first = await persist_turn(sid, initial["player_input"], result)
    assert await persist_turn(sid, initial["player_input"], result) == first
    assert len(await _ledger(sid)) == 2
    if cost == "resource_cost":
        assert "Rope" not in str(first["character"]["inventory"])
    async with sm.async_session_factory() as db:
        rows = list(
            await db.scalars(
                select(EventLog).where(
                    EventLog.session_id == sid, EventLog.event_type == "narration"
                )
            )
        )
    assert len(rows) == 1


@pytest.mark.parametrize("failure", ["persistence", "cancel"])
async def test_policy_transaction_rolls_back_after_real_story_and_inventory_writes(
    policy_source, fake_llm_schema, api_client, monkeypatch, failure
):
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services import story_state
    from apps.api.services.turn_runner import TurnError, persist_turn, run_turn

    slug, _ = policy_source
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db, db.begin():
        await db.execute(update(CharacterAssignment).values(inventory={"items": ["Rope"]}))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration", resolution_policy_id=f"{slug}:resource_cost"
    )
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I risk the Rope.", db)
    with use_rng(LowDice()):
        result = await run_turn(initial)
    original = story_state.persist_story_effects
    entered = asyncio.Event()

    async def fail_after_flush(*args, **kwargs):
        await original(*args, **kwargs)
        entered.set()
        if failure == "cancel":
            await asyncio.Event().wait()
        raise RuntimeError("synthetic failure after real inserts")

    monkeypatch.setattr(story_state, "persist_story_effects", fail_after_flush)
    task = asyncio.create_task(persist_turn(sid, initial["player_input"], result))
    if failure == "cancel":
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(TurnError):
            await task
    assert entered.is_set() and await _ledger(sid) == []
    async with sm.async_session_factory() as db:
        assert list(await db.scalars(select(EventLog).where(EventLog.session_id == sid))) == []
        inventory = await db.scalar(select(CharacterAssignment.inventory))
        assert inventory == {"items": ["Rope"]}


async def test_missing_coupled_cost_rejects_the_entire_reveal(
    policy_source, fake_llm_schema, api_client, monkeypatch
):
    import db.postgres.session as sm

    slug, _ = policy_source
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db, db.begin():
        await db.execute(update(CharacterAssignment).values(inventory={"items": ["Rope"]}))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="exploration", resolution_policy_id=f"{slug}:resource_cost"
    )
    monkeypatch.setattr("agent.object_effects.validate_object_effect", lambda *args: None)
    with use_rng(LowDice()):
        value, adj = await _turn(api_client, sid, False, "I risk the Rope.")
    assert "not available" in value["narration"] and NOTE not in value["narration"]
    assert not adj["success"] and await _ledger(sid) == []
