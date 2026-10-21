"""Sequential AI generation: premise -> cast -> outline -> chapter loop -> shared tail."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.api.services import adventures_meta
from apps.api.services.campaign_prep import ai_generation, graph, publication, runtime
from apps.api.services.campaign_prep.ai_generation import (
    AI_WORKFLOW,
    SIZE_PROFILES,
    chapter_bundle,
    check_chapter,
    check_outline,
    outline_schema,
)
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.provider import SemanticClient, response_format
from apps.api.services.campaign_prep.store import NeedsAttention, Store, digest
from edgar_core.campaign_semantics import Review

CHAPTERS = ("arrival", "reef", "engine")


def premise():
    return {
        "title": "The Bell Beneath Greywake",
        "premise": "A drowned bell rings at low tide and the harbor's water is failing.",
        "setting": "Greywake, a fishing town on a tidal causeway.",
        "tone": "Salt-worn mystery with moments of wonder",
        "hook": "The tide bell rings at noon, and the wells taste of salt.",
        "central_question": "Who is draining Greywake's fresh water, and why?",
        "antagonist": {
            "name": "Vex Marrow",
            "nature": "A disgraced engineer",
            "motive": "Wants the reef engine to restore his name",
            "methods": "Bribes divers and sabotages the sluices",
            "weakness": "Fears public exposure",
        },
        "scheme": [
            {
                "when": "before the adventure",
                "action": "Reopens the tide engine",
                "consequence": "Wells turn brackish",
            },
            {
                "when": "chapter 3",
                "action": "Floods the causeway",
                "consequence": "Greywake is cut off",
            },
        ],
        "stakes": "Greywake loses its water before winter.",
        "ending": "The engine is stopped, bargained over or turned to the town's use.",
        "level_recommendation": "Levels 2-4",
        "dm_guidance": "Let the town's mood darken each day the heroes delay.",
    }


def entity(identity, kind, name, role="supporting"):
    return {
        "id": identity,
        "kind": kind,
        "name": name,
        "aliases": [],
        "description": name + " as first seen.",
        "motive": "Their own reasons",
        "knowledge": "",
        "role": role,
    }


def cast():
    entities = [
        entity("vex_marrow", "npc", "Vex Marrow", "antagonist"),
        entity("mara_vell", "npc", "Mara Vell", "patron"),
        entity("old_tobin", "npc", "Old Tobin", "informant"),
        entity("diver_kell", "npc", "Kell", "lieutenant"),
        entity("tide_wardens", "faction", "The Tide Wardens", "rival"),
        entity("greywake", "place", "Greywake", "hub"),
        entity("outer_reef", "place", "The Outer Reef", "site"),
        entity("tide_engine", "place", "The Tide Engine", "stronghold"),
        entity("salt_ledger", "clue", "The Salt Ledger", "key evidence"),
        entity("brass_key", "clue", "A Brass Sluice Key", "key evidence"),
    ]
    secrets = [
        {
            "id": "vex_runs_engine",
            "truth": "Vex Marrow restarted the tide engine.",
            "known_by": ["vex_marrow", "diver_kell"],
            "reveal_condition": "Expose the ledger or turn Kell.",
        },
        {
            "id": "wardens_were_paid",
            "truth": "The Tide Wardens took Vex's bribes.",
            "known_by": ["tide_wardens"],
            "reveal_condition": "Search the wardens' books or press their captain.",
        },
    ]
    return {"entities": entities, "secrets": secrets}


def outline(*, reveal_first=False):
    shared = [e["id"] for e in cast()["entities"]]
    plans = []
    for index, slug in enumerate(CHAPTERS):
        final = index == len(CHAPTERS) - 1
        plans.append(
            {
                "slug": slug,
                "title": slug.title(),
                "archetype": ("hook", "site", "climax")[index],
                "brief": "Chapter brief for " + slug,
                "dramatic_purpose": "Purpose of " + slug,
                "primary_location_id": ("greywake", "outer_reef", "tide_engine")[index],
                "entity_ids": shared[index * 4 : index * 4 + 4] or shared[:2],
                "sets_up_secret_ids": ["vex_runs_engine", "wardens_were_paid"]
                if index == 0
                else [],
                "reveals_secret_ids": ["wardens_were_paid"]
                if index == 1
                else ["vex_runs_engine"]
                if final or (reveal_first and index == 0)
                else [],
                "antagonist_pressure": "Vex tightens his grip",
                "leads_onward": [] if final else ["Tobin's rumor", "The ledger's last page"],
                "chronology": "Day " + str(index + 1),
            }
        )
    if reveal_first:
        plans[0]["sets_up_secret_ids"] = ["wardens_were_paid"]
        plans[1]["sets_up_secret_ids"] = ["vex_runs_engine"]
    return {"chapters": plans, "dm_guidance": "Each chapter tightens the tide."}


def chapter(cid, plan, *, bad=False):
    location = plan["primary_location_id"]
    local_clue = cid + "_token"
    events = []
    for i in range(4):
        events.append(
            {
                "id": f"{cid}_event_{i}",
                "title": f"Scene {i}",
                "location_id": "mara_vell" if bad and i == 0 else location,
                "participants": [],
                "goal_ids": [f"{cid}_goal_{i % 2}"],
                "secret_ids": (plan["sets_up_secret_ids"] + plan["reveals_secret_ids"])
                if i == 0
                else [],
                "clue_ids": [local_clue] if i == 1 else [],
                # A visible NPC is joined into participants by local normalization.
                "visible_entity_ids": ["old_tobin"] if i == 2 else [],
                "reveals": [],
                "trigger": "The heroes arrive",
                "timing": "Any time",
                "public_description": "Wet stone and gull cries.",
                "dm_notes": "Several approaches work.",
                "next_event_ids": [f"{cid}_event_{i + 1}"] if i < 3 else [],
                "encounters": [
                    {
                        "actor_id": "diver_kell",
                        "creature": "Kraken",
                        "quantity": 1,
                        "tactics": "Ambush",
                    }
                ]
                if bad and i == 3
                else [],
            }
        )
    return {
        "new_entities": [
            entity(local_clue, "clue", "ch1_arrival token" if bad else cid + " token")
        ],
        "new_secrets": [],
        "goals": [
            {
                "id": f"{cid}_goal_{g}",
                "title": f"Goal {g}",
                "objective": "Learn what is wrong",
                "completion": "The heroes understand the threat by any means",
                "required": g == 0,
                "event_ids": [f"{cid}_event_{g}"],
                "depends_on": [],
            }
            for g in range(2)
        ],
        "events": events,
        "checks": [
            {
                "id": f"{cid}_read_tide",
                "action": "Read the tide marks",
                "event_ids": [f"{cid}_event_0"],
                "check": "Survival",
                "dc": 12,
                "success_text": "The water rose overnight.",
                "failure_text": "The marks are confusing.",
            }
        ],
        "items": [],
        "dm_guidance": "Run it briskly.",
        "running_notes": "The tide turns every six hours.",
        "continuity_summary": f"{cid} establishes the salt problem.",
    }


class AiResponses:
    """Provider transport fixture; durable dispatch, parsing and acceptance stay real."""

    def __init__(self, store, *, bad_outline=False, bad_chapter=False, always_bad=False):
        self.store = store
        self.bad_outline, self.bad_chapter, self.always_bad = bad_outline, bad_chapter, always_bad
        self.calls, self.evidence = [], []

    async def create(self, **request):
        name = request["text"]["format"]["name"]
        evidence = json.loads(request["input"][1]["content"][0]["text"])
        self.calls.append(name)
        self.evidence.append((name, evidence))
        revising = "revision" in evidence
        if name == "AiPremise":
            value = premise()
        elif name == "AiCast":
            value = cast()
        elif name == "AiOutline":
            value = outline(reveal_first=self.bad_outline and not revising)
        elif name == "AiChapterDraft":
            requested = evidence["requested_chapter"]
            bad = self.always_bad or (
                self.bad_chapter and requested["number"] == 2 and not revising
            )
            value = chapter(requested["chapter_id"], requested, bad=bad)
        elif name == Review.__name__:
            value = {"covered_ids": evidence["review_ids"], "findings": []}
        else:
            raise AssertionError("Unexpected semantic stage " + name)
        payload = {
            "id": "resp_ai_" + str(len(self.calls)),
            "model": "gpt-6-luna",
            "status": "completed",
            "metadata": request["metadata"],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}
            ],
        }
        return SimpleNamespace(model_dump=lambda **_: payload)


@pytest.fixture
def ai_store(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    store = Store("ai_sequential")
    store.initialize(
        {
            "source_kind": "ai",
            "rules_edition": "2014",
            "preferences": "",
            "generation": {"title_hint": "", "theme": None, "size": "medium"},
            "ai_workflow": AI_WORKFLOW,
        }
    )
    monkeypatch.setattr(graph, "catalogue", lambda _edition: [])
    published = []

    def publish(store):
        published.append(deepcopy(store.get("compiled")))
        store.put("publication", {"compiled_sha256": digest(store.get("compiled"))})

    monkeypatch.setattr(publication, "publish", publish)
    store.published = published
    return store


def workflow(store, responses):
    return Workflow(store, semantic=SemanticClient(store, SimpleNamespace(responses=responses)))


@pytest.mark.asyncio
async def test_medium_request_runs_sequential_steps_into_the_shared_tail(ai_store):
    responses = AiResponses(ai_store)
    await workflow(ai_store, responses).run()
    assert ai_store.state()["status"] == "ready"
    assert len(ai_store.published) == 1
    assert responses.calls[:6] == [
        "AiPremise",
        "AiCast",
        "AiOutline",
        "AiChapterDraft",
        "AiChapterDraft",
        "AiChapterDraft",
    ]
    assert set(responses.calls[6:]) == {"Review"}
    # Chapters are written in play order, each seeing the accepted ones before it.
    drafts = [e for name, e in responses.evidence if name == "AiChapterDraft"]
    assert [d["requested_chapter"]["chapter_id"] for d in drafts] == [
        "ch1_arrival",
        "ch2_reef",
        "ch3_engine",
    ]
    assert [len(d["story_so_far"]) for d in drafts] == [0, 1, 2]
    assert (
        drafts[2]["story_so_far"][1]["continuity_summary"]
        == "ch2_reef establishes the salt problem."
    )
    assert "ch1_arrival_token" in drafts[1]["existing_ids"]
    spine, bundles = ai_store.get("spine"), ai_store.get("bundles")
    assert [c["id"] for c in spine["chapters"]] == ["ch1_arrival", "ch2_reef", "ch3_engine"]
    assert [c["depends_on"] for c in spine["chapters"]] == [[], ["ch1_arrival"], ["ch2_reef"]]
    secrets = {s["id"]: s for s in spine["secrets"]}
    assert secrets["vex_runs_engine"]["setup_chapters"] == ["ch1_arrival"]
    assert secrets["vex_runs_engine"]["reveal_chapters"] == ["ch3_engine"]
    # Provenance is local and authored; the model never filled PDF fields.
    origins = [e["origin"] for b in bundles["chapters"] for e in b["events"]]
    assert all(
        o == {"kind": "authored", "pages": [], "regions": [], "note": "", "uncertain": False}
        for o in origins
    )
    scene = bundles["chapters"][0]["events"][2]
    assert "old_tobin" in scene["participants"]
    assert bundles["chapters"][0]["checks"][0]["check"] == "survival"
    assert "Vex Marrow" in spine["dm_guidance"]


@pytest.mark.asyncio
async def test_outline_problems_are_fed_back_into_one_revision(ai_store):
    responses = AiResponses(ai_store, bad_outline=True)
    await workflow(ai_store, responses).run()
    assert ai_store.state()["status"] == "ready"
    outlines = [e for name, e in responses.evidence if name == "AiOutline"]
    assert len(outlines) == 2
    problems = outlines[1]["revision"]["problems"]
    assert problems == ["Secret vex_runs_engine is revealed before it is set up."]
    assert outlines[1]["revision"]["previous_output"]["chapters"][0]["slug"] == "arrival"


@pytest.mark.asyncio
async def test_chapter_problems_trigger_a_revision_before_later_chapters(ai_store):
    responses = AiResponses(ai_store, bad_chapter=True)
    await workflow(ai_store, responses).run()
    assert ai_store.state()["status"] == "ready"
    drafts = [e for name, e in responses.evidence if name == "AiChapterDraft"]
    assert [d["requested_chapter"]["number"] for d in drafts] == [1, 2, 2, 3]
    problems = drafts[2]["revision"]["problems"]
    assert "Event ch2_reef_event_0: location_id mara_vell is not a place entity." in problems
    assert "Event ch2_reef_event_3: creature 'Kraken' is not in verified_creatures." in problems
    assert (
        "New entity ch2_reef_token reuses the name or alias 'ch1_arrival token' of "
        "ch1_arrival_token; reference ch1_arrival_token or choose a distinct name."
    ) in problems
    assert ai_store.get("ai-bundle-ch2_reef")["events"][0]["location_id"] == "outer_reef"


@pytest.mark.asyncio
async def test_exhausted_revisions_hold_and_retry_grants_another_round(ai_store):
    responses = AiResponses(ai_store, always_bad=True)
    flow = workflow(ai_store, responses)
    with pytest.raises(NeedsAttention, match="chapter-ch1_arrival' still has problems after 3"):
        await flow.run()
    assert responses.calls.count("AiChapterDraft") == 3
    # Retry adds a round: retained attempts are kept and three new revisions follow.
    ai_store.update(retry_round=1)
    responses.always_bad = False
    await workflow(ai_store, responses).run()
    assert ai_store.state()["status"] == "ready"
    assert responses.calls.count("AiPremise") == 1
    assert responses.calls.count("AiChapterDraft") == 6


def test_outline_schema_restricts_references_to_the_cast():
    schema = response_format(outline_schema(cast()))["schema"]
    plan = schema["$defs"]["AiChapterPlan"]["properties"]
    assert plan["primary_location_id"]["enum"] == ["greywake", "outer_reef", "tide_engine"]
    assert "the_drowned_observatory" not in json.dumps(schema)
    assert plan["reveals_secret_ids"]["items"]["enum"] == ["vex_runs_engine", "wardens_were_paid"]
    # AI schemas carry no PDF provenance fields at all.
    assert "regions" not in json.dumps(schema)


def test_outline_check_requires_redundant_leads_and_a_final_climax():
    value = outline()
    value["chapters"][0]["leads_onward"] = ["Only one lead"]
    value["chapters"][2]["archetype"] = "hub"
    _, problems = check_outline(value, cast(), SIZE_PROFILES["medium"])
    assert problems == [
        "Chapter 1 (arrival) needs at least two independent leads onward.",
        "The final chapter must be a climax, stronghold or aftermath; got hub.",
    ]


def test_chapter_encounters_become_shared_catalogue_profiles():
    plan = outline()["chapters"][0]
    draft = chapter("ch1_arrival", plan)
    draft["events"][3]["encounters"] = [
        {
            "actor_id": "diver_kell",
            "creature": "bandit",
            "quantity": 2,
            "tactics": "Flee at half HP",
        }
    ]
    known = {e["id"]: e["kind"] for e in cast()["entities"]}
    known.update({s["id"]: "secret" for s in cast()["secrets"]})
    normalized, problems = check_chapter(
        draft,
        plan=plan,
        chapter_id="ch1_arrival",
        profile=SIZE_PROFILES["medium"],
        known_kinds=known,
        creatures=[{"name": "Bandit", "challenge": "1/8"}],
    )
    assert problems == []
    bundle = chapter_bundle("ch1_arrival", normalized)
    event = bundle["events"][3]
    assert event["participants"] == ["diver_kell"]
    assert event["encounters"] == [
        {
            "actor_id": "diver_kell",
            "profile_id": "profile_bandit",
            "quantity": 2,
            "adaptation": "Flee at half HP",
        }
    ]
    assert bundle["mechanical_profiles"] == [
        {"id": "profile_bandit", "creature": "Bandit", "source_stat_block": "", "source_pages": []}
    ]


def test_new_ai_requests_use_the_sequential_workflow_and_retry_adds_a_round(tmp_path, monkeypatch):
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    meta = {"generation": {"kind": "ai", "size": "medium", "theme": None, "title_hint": ""}}
    assert runtime.frozen_request("x", meta)["ai_workflow"] == AI_WORKFLOW
    assert ai_generation.creature_index(
        [{"name": "Bandit", "text": "Bandit\nChallenge 1/8 (25 XP)"}]
    ) == [{"name": "Bandit", "challenge": "1/8"}]
