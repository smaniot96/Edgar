"""Independent journal review: noninterference and committed chronology edge cases."""

from datetime import UTC, datetime

from apps.api.services.journal import project_player_journal
from db.postgres.models import EventLog
from edgar_core.story import (
    ClockDisclosure,
    StoryEffect,
    StoryFact,
    StoryRef,
    accept_story_effect,
    story_target_id,
)


def fact(kind, name, *, event=1, status="discovered", text=None, audience="player", **kwargs):
    target_override = kwargs.pop("target", None)
    knowledge_kind = kwargs.pop("knowledge_kind", None)
    clock_disclosure = kwargs.pop("clock_disclosure", None)
    related_object = kwargs.pop("related_object", None)
    target = target_override or StoryRef(
        collection="review-fixture", source_sha256="a" * 64, manifest_sha256="b" * 64,
        kind={
            "visit": "scene", "move": "scene", "learn_place": "scene",
            "promise": "promise", "clock": "clock", "objective": "objective",
            "acquire": "object", "transfer": "object", "consume": "object",
            "environment": "object",
        }.get(kind, "entity"),
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
        knowledge_kind=knowledge_kind,
        clock_disclosure=clock_disclosure,
        related_object=related_object,
    )
    return StoryFact(**accepted.model_dump(), fact_id=story_target_id(target), event_id=event)


def project(facts, session=1, *, active_character_id=None):
    event_ids = sorted({fact.event_id for fact in facts if fact.event_id is not None}) or [1]
    events = [EventLog(id=i, session_id=session, event_type="narration",
                       created_at=datetime(2026, 10, 15, 12, i, tzinfo=UTC),
                       payload={"turn_id": f"turn-{i}"}) for i in event_ids]
    return project_player_journal(
        session,
        {"facts": [f.model_dump() for f in facts]},
        events,
        active_character_id=active_character_id,
    )


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


def test_known_people_keep_committed_engine_outcomes_from_concurrent_combat_work():
    met = fact("encounter", "Mara Quill", status="encountered")
    for outcome in ("captive", "released", "dead", "defeated"):
        later = fact("encounter", "Mara Quill", event=2, status=outcome)
        person = project([met, later]).entries[0]
        assert person.status == outcome
        assert [change.status for change in person.history] == ["encountered", outcome]
        assert person.title == "Mara Quill"


def test_known_places_and_announced_goals_do_not_claim_visit_or_active_goal():
    known = fact("learn_place", "Tower", status="known", text="The tower lies beyond the river.")
    announced = fact("objective", "Seek tower", status="announced", text="Seek the tower later.")
    visited = fact("visit", "Hall", status="visited", event=2, text="You enter the hall.")

    journal = project([known, announced, visited])

    assert journal.current_place.title == "Hall"
    assert journal.active_goal is None
    assert {(entry.category, entry.status) for entry in journal.entries} >= {
        ("place", "known"), ("place", "visited"), ("goal", "announced")
    }


def test_learning_a_visited_scene_cannot_downgrade_its_journal_status():
    visited = fact("visit", "Hall", status="visited")
    learned_after_visit = fact("learn_place", "Hall", status="known", event=2)
    journal = project([visited, learned_after_visit])

    assert len(journal.entries) == 1
    assert journal.entries[0].status == "visited"
    assert [item.status for item in journal.entries[0].history] == ["visited"]
    assert journal.current_place.id == journal.entries[0].id


def test_person_and_promise_use_the_latest_earned_name_without_replacing_original_text():
    person_ref = StoryRef(
        collection="review-fixture", source_sha256="a" * 64,
        manifest_sha256="b" * 64, kind="entity", canonical_id="mara",
    )
    first = fact("encounter", "Unknown person", text="A figure waves.", target=person_ref)
    named = fact("encounter", "Mara Quill", event=2, text="She gives her name as Mara Quill.", target=person_ref)
    promise = fact(
        "promise", "Promise", event=3, status="active", text="I will bring the cord.",
        promise_actor=person_ref,
    )

    journal = project([first, named, promise])
    person = next(entry for entry in journal.entries if entry.category == "person")
    promise_entry = next(entry for entry in journal.entries if entry.category == "promise")

    assert person.title == "Mara Quill"
    assert person.text == "A figure waves."
    assert promise_entry.origin.actor == "Mara Quill"
    assert promise_entry.text == "I will bring the cord."


def test_earned_hint_has_its_own_category_and_unearned_hint_metadata_is_not_invented():
    earned = fact("discover", "Reflections", knowledge_kind="hint", text="Use reflections to find the safe path.")
    ordinary = fact("discover", "Lens note", text="The lens tracks reflections.", event=2)

    journal = project([earned, ordinary])

    assert {entry.category for entry in journal.entries} == {"hint", "clue"}


def test_clock_quantity_maximum_and_completion_require_independent_disclosures():
    current_only = fact(
        "clock", "Water", status="completed", quantity=3,
        clock_disclosure=ClockDisclosure(current=True),
    )
    maximum_only = fact(
        "clock", "Wind", status="completed", quantity=4, event=2,
        clock_disclosure=ClockDisclosure(maximum=6),
    )
    completion_only = fact(
        "clock", "Tide", status="completed", quantity=5, event=3,
        clock_disclosure=ClockDisclosure(completion=True),
    )

    entries = {entry.title: entry for entry in project([current_only, maximum_only, completion_only]).entries}

    assert (entries["Water"].status, entries["Water"].quantity, entries["Water"].clock_maximum) == (
        "advanced", 3, None
    )
    assert (entries["Wind"].status, entries["Wind"].quantity, entries["Wind"].clock_maximum) == (
        "advanced", None, 6
    )
    assert (entries["Tide"].status, entries["Tide"].quantity, entries["Tide"].clock_maximum) == (
        "completed", None, None
    )


def test_item_ownership_tracks_acquire_transfer_reacquire_and_consume_for_active_character():
    object_ref = StoryRef(
        collection="review-fixture", source_sha256="a" * 64,
        manifest_sha256="b" * 64, kind="object", canonical_id="lens",
    )
    clue = fact("discover", "Lens note", related_object=object_ref, text="The lens tracks reflections.")
    acquire = fact("acquire", "Lens", status="discovered", event=2, destination="character:7")
    transfer = fact("transfer", "Lens", status="moved", event=3, destination="hall")
    reacquire = fact("acquire", "Lens", status="discovered", event=4, destination="character:7")
    consume = fact("consume", "Lens", status="consumed", event=5)

    states = [
        ([clue, acquire], "carried"),
        ([clue, acquire, transfer], "left_behind"),
        ([clue, acquire, transfer, reacquire], "carried"),
        ([clue, acquire, transfer, reacquire, consume], "consumed"),
    ]
    for facts, expected in states:
        entry = project(facts, active_character_id=7).entries[0]
        assert entry.category == "clue"
        assert entry.ownership == expected
        assert "lens" not in entry.id

    other_character = project(
        [clue, fact("acquire", "Lens", event=2, destination="character:8")],
        active_character_id=7,
    )
    assert other_character.entries[0].ownership == "with_another_character"
    assert project([clue, fact("acquire", "Lens", event=2, destination="character:7")]).entries[0].ownership == "untracked"


def test_object_consequence_links_only_to_earned_clue_and_known_place_entries():
    object_ref = StoryRef(
        collection="review-fixture", source_sha256="a" * 64,
        manifest_sha256="b" * 64, kind="object", canonical_id="lens",
    )
    clue = fact("discover", "Lens note", related_object=object_ref, text="The lens tracks reflections.")
    place = fact("visit", "Star pool", status="visited", event=2)
    consequence = fact("environment", "The ward changes", status="changed", event=3, target=object_ref)

    journal = project([clue, place, consequence])
    change = next(entry for entry in journal.entries if entry.category == "consequence")
    clue_entry = next(entry for entry in journal.entries if entry.category == "clue")
    place_entry = next(entry for entry in journal.entries if entry.title == "Star pool")

    assert set(change.related_entry_ids) == {clue_entry.id, place_entry.id}
