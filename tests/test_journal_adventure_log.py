"""EDG-055: the journal reads like the player's adventure log without exceeding what they saw."""

import hashlib
from datetime import UTC, datetime, timedelta

from apps.api.services.journal import (
    JournalPreparedLabels,
    journal_tracking_marker,
    project_player_journal,
)
from db.postgres.models import EventLog
from edgar_core.story import StoryFact, StoryRef, dm_adjudication_reference, story_target_id

NOW = datetime(2026, 10, 22, tzinfo=UTC)
QUAY = "Festival boats bump against the quay. Workers shout over the bells."
TOWPATH = "The towpath is slick with fresh silt. Workers compare incomplete dispatch slips."


def _ref(kind: str, canonical_id: str) -> StoryRef:
    return StoryRef(
        collection="ai_campaign",
        source_sha256="a" * 64,
        manifest_sha256="b" * 64,
        kind=kind,
        canonical_id=canonical_id,
    )


def _fact(kind, target, event_id, *, status="discovered", text=None, display="Entity", page=2):
    return StoryFact(
        kind=kind,
        target=target,
        status=status,
        player_text=text,
        effect_id=hashlib.sha256(f"{event_id}:{kind}:{target.canonical_id}".encode()).hexdigest(),
        turn_id=f"turn-{event_id}",
        display_name=display,
        source_refs=[
            {
                "audience": "player",
                "reference_id": hashlib.sha256(target.canonical_id.encode()).hexdigest(),
                "source_sha256": "a" * 64,
                "extraction_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "collection": "ai_campaign",
                "page": page,
            }
        ],
        fact_id=story_target_id(target),
        event_id=event_id,
    )


def _goal(event_id, status, canonical_id="find_crew"):
    target = _ref("objective", canonical_id)
    turn_id = f"turn-{event_id}"
    return StoryFact(
        kind="objective",
        target=target,
        status=status,
        effect_id=hashlib.sha256(f"goal:{event_id}:{status}".encode()).hexdigest(),
        turn_id=turn_id,
        display_name="Objective",
        evidence_kind="dm_adjudication",
        origin_turn_id=turn_id,
        achievement_basis="PRIVATE BASIS: the crew is out of the boat.",
        source_refs=[
            dm_adjudication_reference(turn_id, target, action="I haul them out.", success=True)
        ],
        fact_id=story_target_id(target),
        event_id=event_id,
    )


def _narration(event_id: int, minute: int, text: str = "") -> EventLog:
    return EventLog(
        id=event_id,
        session_id=1,
        event_type="narration",
        payload={"turn_id": f"turn-{event_id}", "narration": text, **journal_tracking_marker()},
        created_at=NOW + timedelta(minutes=minute),
    )


PREPARED = JournalPreparedLabels(
    titles={
        ("ai_campaign", "scene", "quay"): "The Waterline Falls",
        ("ai_campaign", "scene", "towpath"): "The Changed Gauges",
        ("ai_campaign", "entity", "tavi_morn"): "Tavi Morn",
        ("ai_campaign", "entity", "orin_dale"): "Orin Dale",
        ("ai_campaign", "entity", "slips"): "Festival Dispatch Fragment",
        ("ai_campaign", "objective", "find_crew"): "Find the Missing Crew",
        ("ai_campaign", "objective", "secret_goal"): "Expose Marr's Plot",
    },
    actor_ids=frozenset({("ai_campaign", "tavi_morn"), ("ai_campaign", "orin_dale")}),
    paged_collections=frozenset(),
)


def _project(facts, events, observations=(), prepared=PREPARED):
    return project_player_journal(
        1,
        {"facts": [fact.model_dump() for fact in facts]},
        events,
        prepared=prepared,
        observations=observations,
    )


def test_a_playthrough_reads_as_places_people_clues_goals_and_notes() -> None:
    events = [
        _narration(10, 0, "You arrive at the quay."),
        _narration(20, 1, "A soaked worker grins. \"Name's Tavi,\" she says."),
        _narration(30, 2, "You follow Tavi to the towpath."),
        _narration(40, 3, "Orin hands you the slips."),
    ]
    facts = [
        _fact("visit", _ref("scene", "quay"), 10, status="visited", text=QUAY, display="Scene", page=1),
        # Legacy ledgers filed the met worker as a discovered "clue".
        _fact("discover", _ref("entity", "tavi_morn"), 20, text=QUAY),
        _goal(20, "completed"),
        _fact("move", _ref("scene", "towpath"), 30, status="visited", text=TOWPATH, display="Scene", page=3),
        _fact("discover", _ref("entity", "slips"), 40, text=TOWPATH, page=3),
    ]
    observations = [
        (19, "turn-20", ["Tavi says a strange current pulled the boat inward."]),
        (39, "turn-40", ["The slips point toward checking the East Sluice House."]),
        # The same earned answer repeated on a later turn is one note.
        (45, "turn-46", ["The slips point toward checking the East Sluice House."]),
    ]
    journal = _project(facts, events + [_narration(46, 4)], observations)
    by_category = {}
    for entry in journal.entries:
        by_category.setdefault(entry.category, []).append(entry)

    [tavi] = by_category["person"]
    assert (tavi.title, tavi.status) == ("Tavi", "encountered")
    assert "clue" not in {entry.category for entry in journal.entries if entry.id == tavi.id}

    places = by_category["place"]
    # Descriptive physical names come from earned prose, never private event titles.
    assert len({entry.id for entry in places}) == 2
    assert {entry.title for entry in places} == {"Quay", "Towpath"}
    assert {entry.text for entry in places} == {QUAY, TOWPATH}
    assert journal.current_place is not None
    assert journal.current_place.title == "Towpath"
    assert journal.current_place.text == TOWPATH

    [clue] = by_category["clue"]
    assert clue.title == "The slips point toward checking the East Sluice House"
    assert clue.origin.place == "Towpath"
    assert clue.origin.place_id == journal.current_place.id
    assert clue.origin.citation is None  # assembled adventures have no book pages

    [goal] = by_category["goal"]
    assert (goal.title, goal.status, goal.text) == ("Find the Missing Crew", "completed", None)

    notes = by_category["note"]
    assert [note.text for note in notes] == [
        "The slips point toward checking the East Sluice House.",
        "Tavi says a strange current pulled the boat inward.",
    ]
    tavi_note = notes[1]
    assert tavi.id in tavi_note.related_entry_ids
    assert tavi_note.origin.turn_number == 2
    assert tavi_note.origin.place == "Quay"
    assert tavi_note.origin.place_id != journal.current_place.id
    assert clue.id in notes[0].related_entry_ids
    assert journal.current_lead is not None and journal.current_lead.category in {"note", "clue"}

    serialized = journal.model_dump_json()
    for private in ("The Changed Gauges", "The Waterline Falls", "Festival Dispatch Fragment",
                    "Morn", "PRIVATE BASIS", "Marr"):
        assert private not in serialized


def test_unsettled_adjudicated_goals_stay_unnamed_and_hidden() -> None:
    events = [_narration(10, 0)]
    journal = _project(
        [_goal(10, "active", "secret_goal")],
        events,
    )
    assert journal.entries == []
    assert journal.active_goal is None
    assert "Marr" not in journal.model_dump_json()


def test_page_citations_only_for_directly_extracted_books() -> None:
    events = [_narration(10, 0)]
    facts = [_fact("discover", _ref("entity", "slips"), 10, text=TOWPATH, page=7)]
    generated = _project(facts, events)
    assert generated.entries[0].origin.citation is None
    book = _project(
        facts,
        events,
        prepared=JournalPreparedLabels(paged_collections=frozenset({"ai_campaign"})),
    )
    assert book.entries[0].origin.citation == "Page 7"


def test_identical_clues_collapse_into_one_card() -> None:
    events = [_narration(10, 0), _narration(20, 1)]
    facts = [
        _fact("discover", _ref("entity", "slips"), 10, text=TOWPATH),
        _fact("discover", _ref("entity", "other_slips"), 20, text=TOWPATH),
    ]
    journal = _project(facts, events)
    assert len([entry for entry in journal.entries if entry.category == "clue"]) == 1


def test_a_name_is_earned_only_as_a_capitalised_name_the_player_was_shown() -> None:
    events = [_narration(10, 0, "An old dale stretches below; nobody here says who they are.")]
    journal = _project(
        [_fact("discover", _ref("entity", "orin_dale"), 10, text=QUAY)],
        events,
    )
    [person] = journal.entries
    assert person.title == "Person encountered on turn 1"
    assert "Orin" not in journal.model_dump_json()


def test_failed_private_goal_does_not_disclose_its_title() -> None:
    journal = _project([_goal(10, "failed", "secret_goal")], [_narration(10, 0)])
    assert journal.entries == []
    assert "Marr" not in journal.model_dump_json()


def test_only_the_matching_committed_turn_delivers_an_answer() -> None:
    events = [_narration(20, 0), _narration(40, 1)]
    journal = _project([], events, [
        (19, "abandoned-turn", ["UNDISPATCHED ANSWER"]),
        (30, "turn-20", ["ANSWER AFTER ITS NARRATION"]),
        (35, "turn-40", ["The East Sluice is worth investigating."]),
    ])
    assert [entry.text for entry in journal.entries] == ["The East Sluice is worth investigating."]
    assert journal.entries[0].origin.history_event_id == 40


def test_current_lead_prefers_the_answer_delivered_with_a_clue() -> None:
    clue = _fact("discover", _ref("entity", "slips"), 20, text=TOWPATH)
    journal = _project([clue], [_narration(20, 0)], [
        (19, "turn-20", ["The sluice keeper can explain the missing dispatches."]),
    ])
    assert journal.current_lead is not None
    assert journal.current_lead.category == "note"
    assert journal.current_lead.text == "The sluice keeper can explain the missing dispatches."


def test_ambiguous_same_turn_answers_stay_separate_from_clues() -> None:
    facts = [
        _fact("discover", _ref("entity", "slips"), 20, text=TOWPATH),
        _fact("discover", _ref("entity", "other"), 20, text=QUAY),
    ]
    journal = _project(facts, [_narration(20, 0)], [(19, "turn-20", ["The boat is damaged."])])
    note = next(entry for entry in journal.entries if entry.category == "note")
    assert not note.related_entry_ids


def test_merging_duplicate_clues_preserves_note_links() -> None:
    facts = [
        _fact("discover", _ref("entity", "slips"), 10, text=TOWPATH),
        _fact("discover", _ref("entity", "other_slips"), 20, text=TOWPATH),
    ]
    # Two notes keep the clue heading grounded in its source passage on both turns.
    journal = _project(facts, [_narration(10, 0), _narration(20, 1)], [
        (19, "turn-20", ["The ink has faded.", "There is a second date."]),
    ])
    clues = [entry for entry in journal.entries if entry.category == "clue"]
    assert len(clues) == 1
    for note in (entry for entry in journal.entries if entry.category == "note"):
        assert note.related_entry_ids == [clues[0].id]


def test_failed_goal_already_known_to_the_player_leaves_the_active_list() -> None:
    active = _fact(
        "objective", _ref("objective", "find_crew"), 10, status="active",
        text="Find the Missing Crew", display="Find the Missing Crew",
    )
    journal = _project([active, _goal(20, "failed")], [_narration(10, 0), _narration(20, 1)])
    [goal] = journal.entries
    assert goal.status == "failed"
    assert goal.title == "Find the Missing Crew"
    assert journal.active_goal is None


def test_export_search_matches_the_journals_readable_status_labels() -> None:
    from apps.api.services.journal import filter_player_journal

    journal = _project([
        _fact("learn_place", _ref("scene", "towpath"), 10, status="known", text=TOWPATH),
    ], [_narration(10, 0)])
    assert len(filter_player_journal(journal, query="heard of, not visited").entries) == 1
    assert len(filter_player_journal(journal, query="place").entries) == 1
