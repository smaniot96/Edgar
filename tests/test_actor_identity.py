"""Actor identity projection preserves earned names, stable links and exact quotations."""

from datetime import UTC, datetime

from apps.api.services.journal import project_player_journal
from apps.api.services.journal_identity import actor_entry_id
from db.postgres.models import EventLog
from edgar_core.story import StoryEffect, StoryFact, StoryRef, accept_story_effect, story_target_id


def actor_ref(canonical_id: str, *, source: str = "a", manifest: str = "b") -> StoryRef:
    return StoryRef(
        collection="actor-fixture",
        source_sha256=source * 64,
        manifest_sha256=manifest * 64,
        kind="entity",
        canonical_id=canonical_id,
    )


def accepted_fact(
    kind: str,
    target: StoryRef,
    *,
    event: int,
    name: str,
    text: str,
    status: str = "encountered",
    promise_actor: StoryRef | None = None,
) -> StoryFact:
    effect = accept_story_effect(
        StoryEffect(
            kind=kind,
            target=target,
            status=status,
            player_text=text,
            promise_actor=promise_actor,
        ),
        turn_id=f"turn-{event}",
        display_name=name,
        source_refs=[
            {
                "reference_id": f"{event:064x}",
                "source_sha256": target.source_sha256,
                "extraction_sha256": "c" * 64,
                "manifest_sha256": target.manifest_sha256,
                "collection": target.collection,
                "page": event,
                "audience": "player",
            }
        ],
    )
    return StoryFact(
        **effect.model_dump(),
        fact_id=story_target_id(target),
        event_id=event,
    )


def journal(facts: list[StoryFact], session_id: int = 9):
    events = [
        EventLog(
            id=event_id,
            session_id=session_id,
            event_type="narration",
            created_at=datetime(2026, 10, 16, 12, event_id, tzinfo=UTC),
            payload={"turn_id": f"turn-{event_id}"},
        )
        for event_id in sorted({fact.event_id for fact in facts if fact.event_id is not None})
    ]
    return project_player_journal(
        session_id,
        {"facts": [fact.model_dump() for fact in facts]},
        events,
    )


def test_actor_links_survive_alias_reveal_relationship_and_source_revision():
    old_ref = actor_ref("mara_quill")
    revised_ref = actor_ref("mara_quill", source="d", manifest="e")
    first = accepted_fact(
        "encounter",
        old_ref,
        event=1,
        name="Mara Quill",
        text="A guard named Mara asks you to wait.",
    )
    reveal = accepted_fact(
        "encounter",
        revised_ref,
        event=2,
        name="Mara Quill",
        text="She reveals her full name is Mara Quill.",
    )
    relationship = accepted_fact(
        "relationship",
        revised_ref,
        event=3,
        name="Mara Quill",
        text="Mara Quill now trusts you with the lantern.",
        status="friendly",
    )
    quote = "I promise to return, Mara.\nI will bring the lantern."
    promise = accepted_fact(
        "promise",
        revised_ref.model_copy(
            update={"kind": "promise", "canonical_id": "promise_return_lantern"}
        ),
        event=4,
        name="Promise",
        text=quote,
        status="active",
        promise_actor=revised_ref,
    )

    current = journal([first, reveal, relationship, promise])
    person = next(entry for entry in current.entries if entry.category == "person")
    promise_entry = next(entry for entry in current.entries if entry.category == "promise")

    assert person.id == actor_entry_id(9, old_ref) == actor_entry_id(9, revised_ref)
    assert person.actor_id == person.id
    assert person.title == "Mara Quill"
    assert person.actor_aliases == ["Mara", "Mara Quill"]
    assert person.text == "A guard named Mara asks you to wait."
    assert person.actor_reference_event_ids == [1, 2, 3, 4]
    assert promise_entry.related_entry_id == person.id
    assert promise_entry.related_actor_id == person.actor_id
    assert promise_entry.origin.actor_id == person.actor_id
    assert promise_entry.origin.actor == "Mara Quill"
    assert promise_entry.text == quote
    assert "Quill" not in journal([first]).model_dump_json()


def test_same_first_name_actors_remain_distinct_and_unknown_surname_stays_hidden():
    venn_ref = actor_ref("mara_venn")
    quill_ref = actor_ref("mara_quill")
    venn = accepted_fact(
        "encounter", venn_ref, event=1, name="Mara Venn", text="Mara Venn checks the map."
    )
    quill = accepted_fact(
        "encounter", quill_ref, event=2, name="Mara Quill", text="Mara Quill guards the gate."
    )
    unknown_ref = actor_ref("unknown_mara")
    unknown = accepted_fact(
        "encounter",
        unknown_ref,
        event=3,
        name="Mara Secretname",
        text="A watchful guard offers a lantern.",
    )

    entries = [entry for entry in journal([venn, quill, unknown]).entries if entry.category == "person"]

    assert len({entry.actor_id for entry in entries}) == 3
    assert {entry.title for entry in entries} == {
        "Mara Venn",
        "Mara Quill",
        "Person encountered on turn 3",
    }
    assert "Secretname" not in " ".join(entry.title for entry in entries)
    assert "Secretname" not in journal([unknown]).model_dump_json()
    assert actor_entry_id(9, venn_ref) != actor_entry_id(10, venn_ref)


def test_duplicate_partial_names_in_one_turn_receive_distinct_contextual_labels():
    first = accepted_fact(
        "encounter",
        actor_ref("mara_north"),
        event=1,
        name="Mara North",
        text="Mara watches from the doorway.",
    )
    second = accepted_fact(
        "encounter",
        actor_ref("mara_south"),
        event=1,
        name="Mara South",
        text="Mara watches from the doorway.",
    )

    people = [entry for entry in journal([first, second]).entries if entry.category == "person"]

    assert len({entry.title for entry in people}) == 2
    assert all("Mara" in entry.title for entry in people)
    assert "North" not in " ".join(entry.title for entry in people)
    assert "South" not in " ".join(entry.title for entry in people)


def test_duplicate_earned_aliases_gain_committed_turn_context():
    first_ref = actor_ref("first_mara")
    second_ref = actor_ref("second_mara")
    first = accepted_fact(
        "encounter", first_ref, event=1, name="Mara Venn", text="Mara waves from the bridge."
    )
    second = accepted_fact(
        "encounter", second_ref, event=2, name="Mara Quill", text="Mara guards the gate."
    )

    people = [entry for entry in journal([first, second]).entries if entry.category == "person"]

    assert len(people) == 2
    assert len({entry.title for entry in people}) == 2
    assert all("mara" in entry.title.casefold() and "turn" in entry.title.casefold() for entry in people)
    assert {entry.text for entry in people} == {
        "Mara waves from the bridge.",
        "Mara guards the gate.",
    }
