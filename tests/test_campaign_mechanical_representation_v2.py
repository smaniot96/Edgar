"""Representation repairs preserve source bytes and use existing engine mechanics."""

import random
from copy import deepcopy

import pytest

from agent.resolution import resolve_check
from apps.api.services.campaign_prep.compile import assemble, make_extraction
from apps.api.services.campaign_prep.mechanical_findings import mechanical_findings
from apps.api.services.campaign_prep.mechanics import source_profile
from db.vector.creatures import identified_creature
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from ingestion.manifest import manifest_chunks
from tests.test_campaign_graph_v2 import sample
from tests.test_campaign_source_mechanics_v2 import PROFILE
from tests.test_chapter_source_retention import source_fixture


def test_omitted_heading_keeps_raw_bytes_and_derived_profile_has_exact_identity():
    raw = "\n" + PROFILE.split("\n", 1)[1]
    profile = source_profile(raw, name="Timber Drake", source_evidence={"pages": [2]})

    assert profile["text"] == "Timber Drake\n" + raw
    assert profile["source_text"] == raw
    assert profile["source_text_sha256"] == text_digest(raw)
    assert profile["text_sha256"] == text_digest(profile["text"])
    assert profile["monster_stats"] == source_profile(PROFILE, name="Timber Drake")["monster_stats"]
    record = identified_creature(
        {"text": profile["text"], "section": "Timber Drake", "stat_block": True}
    )
    assert record["creature_name"] == "Timber Drake"


@pytest.mark.parametrize(
    "raw",
    [
        PROFILE.split("\n", 1)[1] + PROFILE,
        PROFILE.replace("Timber Drake\n", "Different Creature\n", 1),
        "Different Creature (verified base)\n" + PROFILE.split("\n", 1)[1],
        "Unrelated prose\n" + PROFILE.split("\n", 1)[1],
    ],
)
def test_heading_repair_never_relabels_existing_or_mixed_profiles(raw):
    with pytest.raises(ValueError, match="one complete named stat block"):
        source_profile(raw, name="Timber Drake")


def test_same_actor_with_omitted_or_exact_heading_has_one_mechanical_identity():
    encounters = [
        {
            "actor_id": "drake",
            "creature": "Timber Drake",
            "source_stat_block": text,
            "source_pages": [2],
        }
        for text in (PROFILE, PROFILE.split("\n", 1)[1])
    ]
    bundles = {
        "chapters": [
            {
                "chapter_id": "roof",
                "events": [
                    {"id": "roof_first", "encounters": [encounters[0]]},
                    {"id": "roof_again", "encounters": [encounters[1]]},
                ],
            }
        ]
    }
    before = deepcopy(bundles)
    assert mechanical_findings(bundles, []) == []
    assert bundles == before


def test_omitted_heading_compiles_into_runtime_resolvable_passage_without_changing_semantics():
    spine, bundles = sample()
    spine["chapters"][0]["pages"] = [1, 2]
    origin = {"kind": "pdf", "pages": [2], "regions": [], "note": "", "uncertain": False}
    bundle = bundles["chapters"][0]
    bundle["new_entities"].append(
        {
            "id": "drake",
            "kind": "npc",
            "name": "Timber Drake",
            "description": "A drake",
            "origin": origin,
        }
    )
    event = bundle["events"][0]
    event["participants"] = ["drake"]
    raw = PROFILE.split("\n", 1)[1]
    event["encounters"] = [
        {
            "actor_id": "drake",
            "creature": "Timber Drake",
            "quantity": 1,
            "adaptation": "",
            "source_pages": [2],
            "source_stat_block": raw,
        }
    ]
    before = deepcopy((spine, bundles))

    assert mechanical_findings(bundles, []) == []
    compiled = assemble("heading_repair", "2014", spine, bundles, [], source=source_fixture())

    assert (spine, bundles) == before
    assert (
        compiled["registry"]["event_square"]["value"]["encounters"][0]["source_stat_block"] == raw
    )
    assert compiled["source_profiles"]["drake"]["source_text"] == raw
    manifest = AdventureManifest.model_validate(compiled["manifest"])
    manifest.reviewed_by = "representation-test"
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks = manifest_chunks(make_extraction(compiled), manifest)
    passage = next(chunk for chunk in chunks if chunk["passage_id"] == "drake_statistics")
    assert passage["text"] == PROFILE
    record = identified_creature({**passage, "section": "Timber Drake", "stat_block": True})
    assert record["creature_name"] == "Timber Drake"


@pytest.mark.parametrize(
    ("raw", "canonical", "kind", "ability", "modifier"),
    [
        ("constitution_save", "con_save", "save", "CON", 6),
        ("strength", "str", "ability", "STR", 4),
        ("intelligence_arcana", "arcana", "skill", "INT", 1),
    ],
)
def test_check_aliases_compile_and_roll_with_the_original_ability_and_save_proficiency(
    raw, canonical, kind, ability, modifier
):
    spine, bundles = sample()
    check = {
        "id": "source_check",
        "action": "Attempt the source procedure",
        "event_ids": ["event_square"],
        "check": raw,
        "dc": 11,
        "success_text": "Original successful outcome.",
        "failure_text": "Original unsuccessful outcome.",
        "origin": {"kind": "authored", "pages": [], "regions": [], "note": "", "uncertain": False},
    }
    bundles["chapters"][0]["checks"] = [check]
    before = deepcopy((spine, bundles))

    assert mechanical_findings(bundles, []) == []
    compiled = assemble("check_alias", "2014", spine, bundles, [])

    assert (spine, bundles) == before
    assert compiled["registry"]["ch_bell_content"]["value"]["checks"][0]["check"] == raw
    policy = compiled["manifest"]["resolution_policies"][0]
    assert policy["check"] == canonical and policy["dc"] == 11
    assert policy["on_success"] == policy["on_failure"] == []
    procedure = next(
        page["text"] for page in compiled["pages"] if "Source check identifier:" in page["text"]
    )
    assert "Source check identifier: " + raw in procedure
    assert check["success_text"] in procedure and check["failure_text"] in procedure
    character = {"class": "fighter", "level": 5, "stats": {"STR": 18, "CON": 16, "INT": 12}}
    result = resolve_check(character, policy["check"], dc=policy["dc"], rng=random.Random(3))
    original = resolve_check(character, raw, dc=11, rng=random.Random(3))
    assert (result.check, result.kind, result.ability, result.modifier) == (
        canonical,
        kind,
        ability,
        modifier,
    )
    assert result == original
