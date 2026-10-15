"""Independent journal review: noninterference and committed chronology edge cases."""

from datetime import UTC, datetime

from apps.api.services.journal import project_player_journal
from db.postgres.models import EventLog
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


def fact(kind, name, *, event=1, status="discovered", text=None, audience="player", **kwargs):
    target = StoryRef(
        collection="review-fixture", source_sha256="a" * 64, manifest_sha256="b" * 64,
        kind={"visit": "scene", "move": "scene", "promise": "promise", "clock": "clock"}.get(kind, "entity"),
        canonical_id=name.lower().replace(" ", "_"),
    )
    accepted = accept_story_effect(
        StoryEffect(kind=kind, target=target, status=status, player_text=text or name, **kwargs),
        turn_id=f"turn-{event}", display_name=name,
        source_refs=[{
            "reference_id": "c" * 64, "source_sha256": "a" * 64,
            "extraction_sha256": "d" * 64, "manifest_sha256": "b" * 64,
            "collection": "review-fixture", "page": 2, "audience": audience,
            "secret_asset": "private-ending.png", "title": "PRIVATE FUTURE ENDING",
        }],
    )
    return StoryFact(**accepted.model_dump(), fact_id=story_target_id(target), event_id=event)


def project(facts, session=1):
    events = [EventLog(id=i, session_id=session, event_type="narration",
                       created_at=datetime(2026, 10, 15, 12, i, tzinfo=UTC),
                       payload={"turn_id": f"turn-{i}"}) for i in (1, 2, 3)]
    return project_player_journal(session, {"facts": [f.model_dump() for f in facts]}, events)


def test_same_turn_move_takes_precedence_over_initial_visit():
    before = fact("visit", "Old room", status="visited")
    after = fact("move", "New room", status="visited")
    journal = project([before, after])
    assert journal.current_place.title == "New room"
    assert {entry.title for entry in journal.entries} == {"Old room", "New room"}


def test_private_records_do_not_change_visible_payload_or_order():
    visible = [fact("discover", "Lens note"), fact("visit", "Star pool", status="visited")]
    baseline = project(visible).model_dump()
    secret = fact("milestone", "PRIVATE FUTURE ENDING", status="completed", audience="planner")
    assert project([secret, *visible, secret]).model_dump() == baseline
    serialized = project(visible).model_dump_json()
    for private in ("secret_asset", "private-ending", "PRIVATE FUTURE", "review-fixture", "source_sha256"):
        assert private not in serialized
    assert project(visible, session=2).entries[0].id != project(visible).entries[0].id


def test_promise_keeps_exact_original_speech_and_origin_when_fulfilled():
    actor = fact("encounter", "Mara Quill", status="encountered")
    quote = "I promise to return, Mara.\nI will bring the cord."
    made = fact("promise", "Promise", text=quote, status="active", promise_actor=actor.target)
    fulfilled = fact("promise", "Promise", event=3, text=quote, status="fulfilled", promise_actor=actor.target)
    promise = next(entry for entry in project([actor, made, fulfilled]).entries if entry.category == "promise")
    assert promise.text == quote
    assert promise.status == "fulfilled"
    assert promise.origin.history_event_id == 1
    assert promise.origin.actor == "Mara Quill"
    assert [item.status for item in promise.history] == ["active", "fulfilled"]
    assert [item.history_event_id for item in promise.history] == [1, 3]


def test_private_clock_threshold_and_update_count_do_not_change_qualitative_entry():
    initial = fact("clock", "Rising water", text="Water is rising.", status="advanced", quantity=1)
    terminal = fact("clock", "Rising water", event=3, text="Water is rising.", status="completed", quantity=9)
    assert project([initial]).entries[0] == project([initial, terminal]).entries[0]
    quantified = fact("clock", "Rising water", text="The gauge reads 3 marks.", status="advanced", quantity=7)
    entry = project([quantified]).entries[0]
    assert entry.text == "The gauge reads 3 marks."
    assert entry.quantity is None


def test_private_clock_updates_cannot_reorder_visible_entries():
    initial = fact("clock", "Rising water", text="Water is rising.", status="advanced", quantity=1)
    note = fact("discover", "Lens note", event=2)
    terminal = fact("clock", "Rising water", event=3, text="Water is rising.", status="completed", quantity=9)
    assert project([initial, note]).model_dump() == project([initial, note, terminal]).model_dump()
