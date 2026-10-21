"""Mechanical definitions are shared records, never repeated encounter output."""

import json
from copy import deepcopy

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture, pdf_store as pdf_store, workflow
from test_campaign_chapter_parts_v2 import Responses, evidence_fixture, response
from test_campaign_source_mechanics_v2 import PROFILE

from apps.api.services.campaign_prep import mechanics, provider
from apps.api.services.campaign_prep.chapter_parts import ChapterPart, author_chapter, blank, merge
from apps.api.services.campaign_prep.compile import assemble, closure, dependencies, records
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.mechanical_findings import mechanical_findings
from apps.api.services.campaign_prep.profiles import canonical_profiles
from apps.api.services.campaign_prep.provider import response_format
from apps.api.services.campaign_prep.store import NeedsAttention
from edgar_core.campaign_semantics import Bundles, ContinuityPatch, MechanicalProfile, Patch, Review


def fixture():
    spine, bundles = chapter_fixture()
    for actor in ("drake_a", "drake_b"):
        entity = deepcopy(spine["entities"][0])
        entity.update(id=actor, kind="npc", name=actor, description="A dragon.")
        spine["entities"].append(entity)
    first = bundles["chapters"][0]["events"][0]
    first["participants"] = ["drake_a", "drake_b"]
    first["encounters"] = [
        {
            "actor_id": actor,
            "creature": "Timber Drake",
            "quantity": 1,
            "source_stat_block": PROFILE,
            "source_pages": [1],
            "adaptation": "",
        }
        for actor in first["participants"]
    ]
    second = bundles["chapters"][1]["events"][0]
    second["participants"] = ["drake_a"]
    second["encounters"] = [deepcopy(first["encounters"][0])]
    return spine, bundles


@pytest.mark.parametrize("schema", [Bundles, Patch, ContinuityPatch, ChapterPart])
def test_new_provider_encounters_require_references_and_forbid_inline_stats(schema):
    encounter = response_format(schema)["schema"]["$defs"]["Encounter"]
    assert not {"creature", "source_stat_block", "source_pages"} & encounter["properties"].keys()
    assert "profile_id" in encounter["required"]
    assert encounter["properties"]["profile_id"]["type"] == "string"
    assert encounter["additionalProperties"] is False
    if schema is Bundles:
        transport = response_format(schema)["schema"]
        assert "mechanical_profiles" not in transport["properties"]
        assert list(transport["$defs"]["Bundle"]["properties"]).index("mechanical_profiles") < list(
            transport["$defs"]["Bundle"]["properties"]
        ).index("events")


def test_three_appearances_share_one_definition_and_one_compiled_stats_passage(monkeypatch):
    spine, old = fixture()
    unchanged = deepcopy(old)
    canonical = canonical_profiles(old)
    assert old == unchanged
    assert canonical_profiles(canonical) == canonical
    assert len(canonical["mechanical_profiles"]) == 1
    appearances = [
        e for b in canonical["chapters"] for event in b["events"] for e in event["encounters"]
    ]
    identity = canonical["mechanical_profiles"][0]["id"]
    assert {e["profile_id"] for e in appearances} == {identity}
    assert all("source_stat_block" not in e for e in appearances)
    calls = []
    original = mechanics.source_profile

    def parse(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(mechanics, "source_profile", parse)
    compiled = assemble("shared", "2014", spine, canonical, [])
    assert len(calls) == 1
    assert len(compiled["source_profiles"]) == 1
    refs = [
        entity["stat_reference"]
        for entity in compiled["manifest"]["entities"]
        if entity["entity_id"] in {"drake_a", "drake_b"}
    ]
    assert refs[0] == refs[1]
    passage = next(
        row
        for row in compiled["manifest"]["passages"]
        if row["passage_id"] == refs[0]["passage_id"]
    )
    assert set(passage["entity_ids"]) == {"drake_a", "drake_b"}
    assert set(passage["scene_ids"]) == {"event_square", "event_after"}
    assert all(
        "verified" not in row and "source_stat_block" not in row for row in compiled["grounded"]
    )


def test_legacy_source_catalogue_alias_needs_complete_verified_equivalence():
    _, old = fixture()
    old["chapters"][1]["events"][0]["encounters"][0]["source_stat_block"] = ""
    equivalent = canonical_profiles(old, [{"name": "Timber Drake", "text": PROFILE}])
    assert len(equivalent["mechanical_profiles"]) == 1
    assert mechanical_findings(equivalent, []) == []
    different = canonical_profiles(
        old, [{"name": "Timber Drake", "text": PROFILE + "Extra trait."}]
    )
    assert len(different["mechanical_profiles"]) == 2
    assert any(
        "incompatible" in finding["evidence"]
        for finding in mechanical_findings(different, [{"name": "Timber Drake"}])
    )


@pytest.mark.asyncio
async def test_profile_only_patch_changes_actual_consumers_without_touching_chapter_content(
    pdf_store,
):
    spine, old = fixture()
    canonical = canonical_profiles(old)
    identity = canonical["mechanical_profiles"][0]["id"]
    registry = records(spine, canonical)
    assert dependencies(registry)["event_square"] >= {identity}
    assert set(closure(registry, {identity})) >= {
        identity,
        "event_square",
        "event_after",
        "drake_a",
        "drake_b",
    }
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", canonical)
    workflow_before = Workflow(pdf_store)
    registry = workflow_before.registry()
    original_fingerprints = {key: workflow_before.fingerprint(registry, key) for key in registry}
    pdf_store.put(
        "findings",
        [
            {
                "owner": "critic",
                "value": {
                    "id": "profile_hp",
                    "severity": "material",
                    "record_ids": [identity],
                    "evidence": "The shared profile needs correction.",
                    "instruction": "Correct its HP once.",
                },
            }
        ],
    )
    changed = deepcopy(canonical["mechanical_profiles"][0])
    changed["source_stat_block"] = PROFILE.replace("Hit Points 75", "Hit Points 70")

    class Repair:
        async def call(self, node, prompt, schema, evidence, *args):
            assert identity in evidence["allowed_ids"]
            assert "ch_bell_content" not in evidence["allowed_ids"]
            return Patch(mechanical_profiles=[MechanicalProfile.model_validate(changed)])

    await Workflow(pdf_store, semantic=Repair()).repair({"slug": pdf_store.slug})
    updated = pdf_store.get("bundles")
    assert updated["chapters"] == canonical["chapters"]
    assert updated["mechanical_profiles"] == [changed]
    workflow_after = Workflow(pdf_store)
    updated_registry = workflow_after.registry()
    assert (
        workflow_after.fingerprint(updated_registry, "event_square")
        != original_fingerprints["event_square"]
    )
    assert (
        workflow_after.fingerprint(updated_registry, "ch_bell_content")
        == original_fingerprints["ch_bell_content"]
    )
    assert (
        workflow_after.fingerprint(updated_registry, "place_square")
        == original_fingerprints["place_square"]
    )


def test_part_profile_declarations_merge_and_reject_conflicting_same_id():
    profile = MechanicalProfile(
        id="shared_drake", creature="Timber Drake", source_stat_block=PROFILE, source_pages=[1]
    ).model_dump()
    retained = merge(blank("chapter_one"), {"mechanical_profiles": [profile]})
    assert merge(retained, {"mechanical_profiles": [profile]}) == retained
    conflicting = {**profile, "source_stat_block": PROFILE.replace("Hit Points 75", "Hit Points 1")}
    with pytest.raises(NeedsAttention, match="conflict"):
        merge(retained, {"mechanical_profiles": [conflicting]})


def test_existing_ready_inline_artifacts_and_fingerprints_are_unchanged_on_read(pdf_store):
    spine, old = fixture()
    old.pop("mechanical_profiles", None)
    for bundle in old["chapters"]:
        bundle.pop("mechanical_profiles", None)
        for event in bundle["events"]:
            for appearance in event["encounters"]:
                appearance.pop("profile_id", None)
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", old)
    pdf_store.update(status="ready")
    workflow_before = Workflow(pdf_store)
    registry = workflow_before.registry()
    fingerprints = {key: workflow_before.fingerprint(registry, key) for key in registry}
    with pdf_store.db() as db:
        original_rows = [tuple(row) for row in db.execute("SELECT * FROM artifact ORDER BY name")]
    reopened = Workflow(type(pdf_store)(pdf_store.slug))
    assert reopened.registry() == registry
    assert {key: reopened.fingerprint(reopened.registry(), key) for key in registry} == fingerprints
    assert reopened.store.state()["status"] == "ready"
    with pdf_store.db() as db:
        assert [
            tuple(row) for row in db.execute("SELECT * FROM artifact ORDER BY name")
        ] == original_rows


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_paid_legacy_schema_replays_exact_original_task_without_new_call(
    pdf_store, monkeypatch, incomplete
):
    evidence, bundle = evidence_fixture(pdf_store)
    bundle.pop("mechanical_profiles", None)
    current = provider.response_format

    def old_format(schema):
        transport = current(schema)
        if schema is Bundles:
            definitions = transport["schema"]["$defs"]
            for name in ("Bundle",):
                definitions[name]["properties"].pop("mechanical_profiles")
                definitions[name]["required"].remove("mechanical_profiles")
            definitions.pop("MechanicalProfile", None)
            encounter = definitions["Encounter"]
            encounter["properties"].pop("profile_id")
            encounter["required"].remove("profile_id")
            encounter["properties"].update(
                creature={"type": "string"},
                source_stat_block={"type": "string"},
                source_pages={"type": "array", "items": {"type": "integer"}},
            )
            encounter["required"].extend(["creature", "source_stat_block", "source_pages"])
        return transport

    monkeypatch.setattr(provider, "response_format", old_format)
    text = '{"chapters":[' + json.dumps(bundle) + ("" if incomplete else "]}")
    calls = Responses(pdf_store, lambda *_: response(text, incomplete=incomplete))
    value, _ = await author_chapter(workflow(pdf_store, calls), evidence, [])
    with pdf_store.db() as db:
        original = dict(db.execute("SELECT * FROM task").fetchone())
    monkeypatch.setattr(provider, "response_format", current)
    monkeypatch.setattr(provider, "instructions", lambda _: "New shared-profile instructions")
    replayed, _ = await author_chapter(
        workflow(type(pdf_store)(pdf_store.slug), calls),
        {**evidence, "known_mechanical_profiles": []},
        [],
    )
    assert replayed == value
    assert len(calls.requests) == 1
    assert pdf_store.task(original["id"]) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("closed_profile", [False, True])
async def test_closed_chapter_never_loses_trailing_old_root_profile(pdf_store, closed_profile):
    evidence, bundle = evidence_fixture(pdf_store)
    actor = deepcopy(evidence["shared_entities"][0])
    actor.update(id="shared_drake", kind="npc")
    evidence["shared_entities"].append(actor)
    profile = MechanicalProfile(
        id="timber_profile", creature="Timber Drake", source_stat_block=PROFILE, source_pages=[1]
    ).model_dump()
    event = bundle["events"][0]
    event["participants"] = ["shared_drake"]
    event["encounters"] = [
        {
            "actor_id": "shared_drake",
            "profile_id": "timber_profile",
            "quantity": 1,
            "adaptation": "",
        }
    ]
    text = '{"chapters":[' + json.dumps(bundle) + '],"mechanical_profiles":['
    text += (
        json.dumps(profile)
        if closed_profile
        else '{"id":"timber_profile","creature":"Timber Drake","source_stat_block":"unfinished'
    )

    def build(call, index):
        if index == 1:
            return response(text, incomplete=True)
        assert not closed_profile and call["schema"] == "ChapterPart"
        assert call["evidence"]["retained_chapter"]["events"] == bundle["events"]
        return response(
            ChapterPart(
                chapter_id=bundle["chapter_id"],
                mechanical_profiles=[profile],
                source_pages=evidence["requested_chapter"]["pages"],
                complete=True,
            ).model_dump_json(),
            identity="resp_missing_definition",
        )

    calls = Responses(pdf_store, build)
    value, receipt = await author_chapter(workflow(pdf_store, calls), evidence, [])
    assert value["mechanical_profiles"] == [profile]
    assert len(calls.requests) == (1 if closed_profile else 2)
    assert receipt["mode"] == ("closed-output-recovery" if closed_profile else "output-recovery")
    with pdf_store.db() as db:
        original = db.execute(
            "SELECT state,accepted FROM task WHERE node=?", ("chapter-" + bundle["chapter_id"],)
        ).fetchone()
    assert original["state"] == "incomplete" and original["accepted"] is None


def test_missing_profile_is_a_controlled_retained_error():
    _, old = fixture()
    old["chapters"][0]["events"][0]["encounters"] = [
        {"actor_id": "drake_a", "profile_id": "missing_profile", "quantity": 1, "adaptation": ""}
    ]
    before = deepcopy(old)
    with pytest.raises(NeedsAttention, match="Unknown shared mechanical profile: missing_profile"):
        canonical_profiles(old)
    assert old == before


@pytest.mark.asyncio
async def test_local_review_supplies_only_profiles_consumed_by_its_records(pdf_store):
    spine, old = fixture()
    old["chapters"][1]["events"][0]["participants"] = ["drake_b"]
    old["chapters"][1]["events"][0]["encounters"] = [
        {"actor_id": "drake_b", "creature": "Wolf", "quantity": 1, "adaptation": ""}
    ]
    old["chapters"][0]["events"][0]["participants"] = ["drake_a"]
    old["chapters"][0]["events"][0]["encounters"] = old["chapters"][0]["events"][0]["encounters"][
        :1
    ]
    canonical = canonical_profiles(old)
    own = canonical["chapters"][0]["events"][0]["encounters"][0]["profile_id"]
    other = canonical["chapters"][1]["events"][0]["encounters"][0]["profile_id"]
    pdf_store.put("spine", spine)
    pdf_store.put("bundles", canonical)
    pdf_store.put(
        "compiled",
        {
            "manifest": {
                "scenes": [{"scene_id": "event_square"}, {"scene_id": "event_after"}],
                "objects": [],
                "resolution_policies": [],
            },
            "grounded": [],
            "mechanical_profiles": {own: {"name": "Timber Drake"}, other: {"name": "Wolf"}},
        },
    )

    class Critic:
        async def call(self, node, prompt, schema, evidence, *args):
            assert set(evidence["mechanical_profiles"]) == {own}
            assert (
                evidence["chapter_inventory"][0]["events"][0]["encounters"][0]["creature"]
                == "Timber Drake"
            )
            return Review(covered_ids=evidence["review_ids"])

    prepared = Workflow(pdf_store, semantic=Critic())
    registry = prepared.registry()
    pdf_store.put(
        "review-coverage",
        {key: prepared.fingerprint(registry, key) for key in registry if key != "event_square"},
    )
    await prepared.review({"slug": pdf_store.slug})
