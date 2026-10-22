"""Selected-session journal endpoints expose only committed player-safe facts."""

import hashlib
from datetime import UTC, datetime

from apps.api.services.journal import journal_tracking_marker
from db.postgres.models import EventLog, Session, StoryFact as StoryFactRow
from edgar_core.story import StoryFact, StoryRef, story_target_id


async def _add_fact(db_session, session_id: int, *, canonical_id: str, text: str, audience: str) -> None:
    turn_id = f"turn-{session_id}-{canonical_id}"
    event = EventLog(
        session_id=session_id,
        event_type="narration",
        payload={"turn_id": turn_id},
        created_at=datetime.now(UTC),
    )
    db_session.add(event)
    await db_session.flush()
    target = StoryRef(
        collection="journal-fixture",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        kind="entity",
        canonical_id=canonical_id,
    )
    fact = StoryFact(
        kind="discover",
        target=target,
        status="discovered",
        player_text=text,
        source_reference_ids=[],
        knowledge_fact_ids=[],
        quantity=None,
        destination=None,
        operation=None,
        promise_actor=None,
        effect_id=hashlib.sha256(f"{session_id}:{canonical_id}".encode()).hexdigest(),
        turn_id=turn_id,
        display_name="",
        source_refs=[
            {
                "audience": audience,
                "reference_id": "d" * 64,
                "source_sha256": "a" * 64,
                "extraction_sha256": "e" * 64,
                "manifest_sha256": "b" * 64,
                "collection": "journal-fixture",
                "page": 5,
                "private_file": "/secret/private.pdf",
            }
        ],
        evidence_kind="source",
        origin_turn_id=turn_id,
        fact_id=story_target_id(target),
        event_id=event.id,
    )
    db_session.add(
        StoryFactRow(
            session_id=session_id,
            effect_id=fact.effect_id,
            fact_id=fact.fact_id,
            event_id=event.id,
            payload=fact.model_dump(),
        )
    )
    await db_session.commit()


async def test_journal_routes_are_safe_and_session_scoped(api_client, db_session, fake_retrieval) -> None:
    first_id = (await api_client.post("/api/seed")).json()["session_id"]
    campaign_id = (await api_client.get(f"/api/sessions/{first_id}")).json()["campaign_id"]
    second_response = await api_client.post("/api/sessions", json={"campaign_id": campaign_id})
    assert second_response.status_code == 200
    second_id = second_response.json()["id"]
    await _add_fact(
        db_session,
        first_id,
        canonical_id="public_note",
        text="The lens tracks reflections, not the sky.",
        audience="player",
    )
    await _add_fact(
        db_session,
        first_id,
        canonical_id="private_note",
        text="PRIVATE FUTURE VILLAIN TRUE NAME",
        audience="planner",
    )
    await _add_fact(
        db_session,
        second_id,
        canonical_id="public_note",
        text="The lens has not been found in this session.",
        audience="player",
    )

    first = await api_client.get(f"/api/sessions/{first_id}/journal")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    first_json = first.json()
    assert len(first_json["entries"]) == 1
    first_entry = first_json["entries"][0]
    assert first_entry["text"] == "The lens tracks reflections, not the sky."
    # The fixture collection is not a directly extracted book: no page number is claimed.
    assert first_entry["origin"]["citation"] is None
    assert first_entry["origin"]["history_event_id"] is not None
    assert "PRIVATE FUTURE VILLAIN TRUE NAME" not in first.text
    assert "private_file" not in first.text

    second = await api_client.get(f"/api/sessions/{second_id}/journal")
    assert second.status_code == 200
    second_entry_id = second.json()["entries"][0]["id"]
    assert second_entry_id != first_entry["id"]
    assert (
        await api_client.get(f"/api/sessions/{second_id}/journal/{first_entry['id']}")
    ).status_code == 404
    assert (await api_client.get(f"/api/sessions/{first_id}/journal/missing")).status_code == 404

    filtered = await api_client.get(
        f"/api/sessions/{first_id}/journal", params={"q": "private future villain"}
    )
    assert filtered.status_code == 200
    assert filtered.json()["entries"] == []

    exported = await api_client.get(
        f"/api/sessions/{first_id}/journal/export", params={"q": "lens tracks"}
    )
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "private, no-store"
    assert "attachment;" in exported.headers["content-disposition"]
    assert [entry["text"] for entry in exported.json()["entries"]] == [
        "The lens tracks reflections, not the sky."
    ]
    assert "PRIVATE FUTURE" not in exported.text


async def _add_narration(db_session, session_id: int, *, tracked: bool) -> None:
    payload = {"turn_id": f"turn-{session_id}", **(journal_tracking_marker() if tracked else {})}
    db_session.add(EventLog(session_id=session_id, event_type="narration", payload=payload))
    await db_session.commit()


async def test_new_session_is_tracked_and_pre_journal_session_is_legacy_in_journal_and_map(
    api_client, db_session
) -> None:
    first_id = (await api_client.post("/api/seed")).json()["session_id"]
    campaign_id = (await api_client.get(f"/api/sessions/{first_id}")).json()["campaign_id"]
    new_session = Session(campaign_id=campaign_id)
    legacy_session = Session(campaign_id=campaign_id)
    db_session.add_all([new_session, legacy_session])
    await db_session.commit()
    new_id, legacy_id = new_session.id, legacy_session.id
    await _add_narration(db_session, new_id, tracked=True)
    await _add_narration(db_session, legacy_id, tracked=False)

    for session_id, expected in ((new_id, "available"), (legacy_id, "legacy_untracked")):
        journal = await api_client.get(f"/api/sessions/{session_id}/journal")
        assert journal.status_code == 200
        assert journal.json()["tracking"] == expected
        assert journal.json()["entries"] == []

        map_response = await api_client.get(f"/api/sessions/{session_id}/map")
        assert map_response.status_code == 200
        assert map_response.json()["tracking"] == expected
