"""Fake-provider sync/SSE story continuity, atomic writes, replay and privacy."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from agent.models.adjudication import AdjudicationResult, CharacterUpdate
from agent.models.parsed_input import ParsedInput
from db.postgres.models import EventLog
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from edgar_core.sources import source_citation
from edgar_core.story import StoryEffect, StoryRef, story_target_id

NOTE = "The lens tracks reflections, not the sky."
PUBLIC = f"Mara greets you. A Lens note reads “{NOTE}” Spare cord lies beside the Brace."
SECRET = "PRIVATE FUTURE VILLAIN TRUE NAME"


@pytest.fixture
def story_source(monkeypatch, api_client):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from db.vector.collections import DEFAULT_ADVENTURE_COLLECTIONS
    from db.vector.embeddings import VECTOR_SIZE
    from ingestion.extract import ExtractedPage, Extraction
    from ingestion.manifest import manifest_chunks
    from ingestion.quality import check_text

    q = QdrantClient(":memory:")
    manifests, evidence = {}, {}
    for slug in DEFAULT_ADVENTURE_COLLECTIONS:
        text = PUBLIC + "\n" + SECRET
        manifest = AdventureManifest(
            source=slug,
            rules_edition="2014",
            source_sha256="a" * 64,
            extraction_sha256=Extraction(
                slug, "a" * 64, 1, [ExtractedPage(1, text, "generated", check_text(text))]
            ).text_sha256(),
            opening_scene_id="room",
            scenes=[{"scene_id": "room", "title": "Room"}],
            entities=[
                {"entity_id": "mara", "kind": "actor", "title": "Mara"},
                {
                    "entity_id": "lens_note",
                    "kind": "clue",
                    "title": "Lens note",
                    "discovery_flag": "note_discovered",
                },
            ],
            objects=[
                {
                    "canonical_id": "note",
                    "title": "Lens note",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                },
                {
                    "canonical_id": "cord",
                    "title": "Spare cord",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                    "operations": ["tether"],
                    "consumable": True,
                },
                {
                    "canonical_id": "brace",
                    "title": "Brace",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                    "portable": False,
                    "operations": ["secure"],
                },
            ],
            objectives=[
                {
                    "canonical_id": "rescue",
                    "title": "Rescue",
                    "scene_ids": ["room"],
                    "passage_ids": ["public"],
                }
            ],
            passages=[
                {
                    "passage_id": "public",
                    "page": 1,
                    "start": 0,
                    "end": len(PUBLIC),
                    "text_sha256": text_digest(PUBLIC),
                    "scene_ids": ["room"],
                    "entity_ids": ["mara", "lens_note"],
                    "audience": "player",
                },
                {
                    "passage_id": "private",
                    "page": 1,
                    "start": len(PUBLIC) + 1,
                    "end": len(text),
                    "text_sha256": text_digest(SECRET),
                    "scene_ids": ["room"],
                    "audience": "planner",
                },
            ],
            reviewed_by="synthetic acceptance fixture",
        )
        manifest.reviewed_sha256 = manifest_digest(manifest)
        extraction = Extraction(
            slug, "a" * 64, 1, [ExtractedPage(1, text, "generated", check_text(text))]
        )
        chunks = manifest_chunks(extraction, manifest)
        manifests[slug] = manifest.model_dump()
        evidence[slug] = chunks
        q.create_collection(
            slug, vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)
        )
        q.upsert(
            slug,
            [
                PointStruct(id=i + 1, vector=[1.0] + [0.0] * (VECTOR_SIZE - 1), payload=c)
                for i, c in enumerate(chunks)
            ],
        )
    monkeypatch.setattr(
        "apps.api.services.adventure_manifests.campaign_manifests",
        lambda slugs: {s: manifests[s] for s in slugs},
    )
    monkeypatch.setattr("db.vector.retrieval.get_qdrant_client", lambda: q)
    monkeypatch.setattr(
        "agent.nodes.world_retriever.inspect_source",
        lambda slug, **kw: {"slug": slug, "status": "ready", "usable_chunks": 2},
    )
    monkeypatch.setattr("agent.nodes.world_retriever.search_rules_context", lambda *a, **kw: [])
    slug = next(iter(manifests))
    manifest = manifests[slug]
    chunk = next(c for c in evidence[slug] if c["audience"] == "player")
    refid = source_citation({**chunk, "collection": slug}, "2014")["reference_id"]

    def effect(kind, identity, *, target_kind="object", status="discovered", **kw):
        return StoryEffect(
            kind=kind,
            status=status,
            target=StoryRef(
                collection=slug,
                source_sha256=manifest["source_sha256"],
                manifest_sha256=manifest["reviewed_sha256"],
                kind=target_kind,
                canonical_id=identity,
            ),
            source_reference_ids=[refid],
            **kw,
        )

    yield effect
    q.close()


def events(text):
    return [
        (frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:]))
        for frame in text.strip().split("\n\n")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_story_pickup_environment_reload_and_rejection(
    story_source, fake_llm_schema, api_client, monkeypatch, stream
):
    import db.postgres.session as sm
    from apps.api.services.story_state import load_story_state

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    monkeypatch.setenv("EDGAR_DEBUG", "1")
    suffix = "/stream" if stream else ""

    async def turn(proposals, narration="You proceed carefully."):
        fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
            success=True,
            story_effects=proposals,
            mechanical_summary="REJECTED CLAIM OF COMPLETED FACT",
            character_update=CharacterUpdate(
                inventory_add=["Lens note", "unreviewed trophy"],
                inventory_remove=["shortsword"],
            ),
        )
        fake_llm_schema.narration = narration
        response = await api_client.post(
            f"/api/sessions/{sid}/turn{suffix}",
            json={"message": "I inspect and use what I find.", "debug": True},
        )
        assert response.status_code == 200, response.text
        assert SECRET not in response.text
        assert "REJECTED CLAIM" not in response.text
        if stream:
            frames = events(response.text)
            assert frames[-1][0] == "done", response.text
            return frames[-1][1]
        return response.json()

    effect = story_source
    await turn([effect("encounter", "mara", target_kind="entity", status="encountered")])
    pickup = effect("acquire", "note", status="acquired")
    first = await turn(
        [effect("discover", "lens_note", target_kind="entity"), pickup, pickup],
        f"You recover the Lens note: “{NOTE}”",
    )
    assert "unreviewed trophy" not in str(first["character"]["inventory"])
    stable_id = story_target_id(pickup.target)
    assert (
        sum(
            isinstance(i, dict) and i.get("story_object_id") == stable_id
            for i in first["character"]["inventory"]["items"]
        )
        == 1
    )
    await turn([effect("acquire", "cord", status="acquired")])
    await turn([effect("use", "cord", status="used", operation="tether", destination="brace")])
    async with sm.async_session_factory() as db:
        ledger = await load_story_state(db, sid)
    use = next(f for f in ledger["facts"] if f["kind"] == "use")
    await turn(
        [
            effect(
                "environment",
                "brace",
                status="changed",
                operation="secure",
                knowledge_fact_ids=[use["fact_id"]],
            )
        ]
    )
    await turn(
        [
            effect(
                "objective",
                "rescue",
                target_kind="objective",
                status="completed",
                knowledge_fact_ids=[use["fact_id"]],
            )
        ]
    )
    await turn([pickup, effect("environment", "brace", status="changed", operation="unsupported")])
    async with sm.async_session_factory() as db:
        ledger = await load_story_state(db, sid)
    assert (
        len(
            [
                f
                for f in ledger["facts"]
                if f["kind"] == "acquire" and f["target"]["canonical_id"] == "note"
            ]
        )
        == 1
    )
    assert any(f["kind"] == "environment" for f in ledger["facts"])
    assert any(f["kind"] == "objective" and f["status"] == "completed" for f in ledger["facts"])
    assert all(f["player_text"] == PUBLIC for f in ledger["facts"])
    # A later deliberate drop and pickup is a new occurrence of the same object, while
    # retries/proposals that leave ownership unchanged never create another copy.
    await turn([effect("transfer", "note", status="moved", destination="room")])
    reacquired = await turn([pickup])
    assert (
        sum(
            isinstance(i, dict) and i.get("story_object_id") == stable_id
            for i in reacquired["character"]["inventory"]["items"]
        )
        == 1
    )
    await turn([effect("consume", "cord", status="consumed")])
    await turn([effect("acquire", "cord", status="acquired")])
    async with sm.async_session_factory() as db:
        consumed_state = await load_story_state(db, sid)
    assert (
        len(
            [
                f
                for f in consumed_state["facts"]
                if f["kind"] == "acquire" and f["target"]["canonical_id"] == "cord"
            ]
        )
        == 1
    )
    restored = await api_client.get(f"/api/sessions/{sid}/messages")
    assert NOTE in restored.text
    assert SECRET not in restored.text
    # Future planning receives the exact committed object identity and quote after reload.
    prompts = "\n".join(
        str(messages) for schema, messages in fake_llm_schema.calls if schema is AdjudicationResult
    )
    assert stable_id in prompts and NOTE in prompts


@pytest.mark.asyncio
async def test_same_server_turn_replay_has_one_narration_and_object(
    story_source, fake_llm_schema, api_client
):
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services.turn_runner import persist_turn, run_turn

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, story_effects=[story_source("acquire", "note", status="acquired")]
    )
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I take the note.", db)
    result = await run_turn(initial)
    assert len(result["adjudication_result"].accepted_story_effects) == 1
    first = await persist_turn(sid, initial["player_input"], result)
    assert await persist_turn(sid, initial["player_input"], result) == first
    async with sm.async_session_factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.session_id == sid, EventLog.event_type == "narration")
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "persistence", "stale"])
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_story_turn_never_commits_effects(
    story_source, fake_llm_schema, api_client, monkeypatch, failure, stream
):
    from sqlalchemy import update

    import db.postgres.session as sm
    from apps.api.services import turn_runner
    from apps.api.services.story_state import load_story_state
    from db.postgres.models import CharacterAssignment

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, story_effects=[story_source("acquire", "note", status="acquired")]
    )
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    if failure == "provider":

        async def boom(*args, **kw):
            raise RuntimeError("private provider failure")

        async def boom_stream(*args, **kw):
            raise RuntimeError("private provider failure")
            yield

        monkeypatch.setattr(fake_llm_schema, "ainvoke", boom)
        monkeypatch.setattr(fake_llm_schema, "astream", boom_stream)
    elif failure == "persistence":

        async def boom(*args, **kw):
            raise RuntimeError("private persistence failure")

        monkeypatch.setattr(turn_runner, "maybe_complete_campaign", boom)
    else:
        original = turn_runner.persist_turn

        async def stale(*args, **kw):
            async with sm.async_session_factory() as db:
                await db.execute(update(CharacterAssignment).values(hp_current=3))
                await db.commit()
            return await original(*args, **kw)

        monkeypatch.setattr(turn_runner, "persist_turn", stale)
        monkeypatch.setattr("apps.api.routers.session.persist_turn", stale)
    suffix = "/stream" if stream else ""
    response = await api_client.post(
        f"/api/sessions/{sid}/turn{suffix}", json={"message": "I take the note."}
    )
    if stream:
        names = [name for name, _ in events(response.text)]
        assert names[-1] == "error" and "done" not in names
        if failure != "provider":
            assert "token" in names  # Tokens are provisional until a committed done frame.
    else:
        assert response.status_code in {500, 503}
    assert "private provider failure" not in response.text
    assert "private persistence failure" not in response.text
    async with sm.async_session_factory() as db:
        assert (await load_story_state(db, sid))["facts"] == []
        assert (
            await db.scalar(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.session_id == sid, EventLog.event_type == "narration")
            )
            == 0
        )
        inventory = await db.scalar(select(CharacterAssignment.inventory))
        assert not any(
            isinstance(item, dict) and item.get("story_object_id")
            for item in inventory.get("items", [])
        )


@pytest.mark.asyncio
async def test_stream_cancellation_before_done_does_not_save(
    story_source, fake_llm_schema, api_client
):
    import db.postgres.session as sm
    from apps.api.routers.session import _load_turn_initial_state
    from apps.api.services.story_state import load_story_state
    from apps.api.services.turn_runner import stream_session_turn

    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="exploration")
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=True, story_effects=[story_source("acquire", "note", status="acquired")]
    )
    sid = (await api_client.post("/api/seed")).json()["session_id"]
    async with sm.async_session_factory() as db:
        initial = await _load_turn_initial_state(sid, "I take the note.", db)
    stream = stream_session_turn(
        session_id=sid, body_message=initial["player_input"], initial_state=initial
    )
    async for frame in stream:
        if frame.startswith("event: token"):
            break
    await stream.aclose()
    async with sm.async_session_factory() as db:
        assert (await load_story_state(db, sid))["facts"] == []
        assert (
            await db.scalar(
                select(func.count())
                .select_from(EventLog)
                .where(EventLog.session_id == sid, EventLog.event_type == "narration")
            )
            == 0
        )


@pytest.mark.parametrize("stream", [False, True])
async def test_spoken_promise_survives_turn_persistence_and_reload(
    story_source, fake_llm_schema, api_client, stream
):
    import db.postgres.session as sm
    from apps.api.services.story_state import load_story_state, player_known_story
    from edgar_core.story import StoryFact, player_promise_ref

    sid = (await api_client.post("/api/seed")).json()["session_id"]
    fake_llm_schema.responses[ParsedInput] = ParsedInput(intent="social")
    suffix = "/stream" if stream else ""

    async def turn(proposal, message):
        fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
            success=True, story_effects=[proposal]
        )
        response = await api_client.post(
            f"/api/sessions/{sid}/turn{suffix}", json={"message": message}
        )
        assert response.status_code == 200, response.text
        if stream:
            assert events(response.text)[-1][0] == "done", response.text
        async with sm.async_session_factory() as db:
            return await load_story_state(db, sid)

    state = await turn(
        story_source("encounter", "mara", target_kind="entity", status="encountered"),
        "I greet Mara.",
    )
    actor = StoryFact.model_validate(state["facts"][0])
    quotation = "Mara, I promise to bring the note tomorrow."
    proposal = StoryEffect(
        kind="promise",
        target=player_promise_ref(actor.target, quotation),
        status="active",
        promise_actor=actor.target,
        knowledge_fact_ids=[actor.fact_id],
        source_reference_ids=actor.source_reference_ids,
        player_text="MODEL REPLACEMENT",
    )
    state = await turn(proposal, quotation)
    promise = StoryFact.model_validate(state["facts"][-1])
    assert promise.player_text == quotation
    assert promise.evidence_kind == "player_utterance"
    assert promise.origin_turn_id == promise.turn_id
    assert promise.event_id != actor.event_id
    state = await turn(
        proposal.model_copy(
            update={"status": "fulfilled", "knowledge_fact_ids": [promise.fact_id]}
        ),
        "I bring Mara the note as promised.",
    )
    fulfilled = StoryFact.model_validate(state["facts"][-1])
    assert fulfilled.status == "fulfilled"
    assert fulfilled.player_text == quotation
    assert fulfilled.origin_turn_id == promise.origin_turn_id
    assert fulfilled.event_id != promise.event_id
    public = player_known_story(state)["facts"][-1]
    assert public["evidence_kind"] == "player_utterance"
    assert public["text"] == quotation
    assert "MODEL REPLACEMENT" not in str(state)
