"""Source profiles keep printed mechanics; unsupported effects stay explicit."""

from copy import deepcopy

import pytest

from agent.monster_stats import AttackSave, parse_monster_stats
from apps.api.services.campaign_prep.mechanical_findings import mechanical_findings
from apps.api.services.campaign_prep.mechanics import source_profile

PROFILE = """Timber Drake
Large dragon, unaligned
Armor Class 16
Hit Points 75 (10d8 + 30)
Speed 30 ft., fly 30 ft.
STR DEX CON INT WIS CHA
19 (+4) 14 (+2) 17 (+3) 8 (-1) 8 (-1) 4 (-3)
Damage Vulnerabilities fire
Actions
Bite. Melee Weapon Attack: +6 to hit, reach 5 ft., one target. Hit: 15 (2d10 + 4) piercing damage.
Splinter Breath (Recharge 5-6). The Timber Drake sends out a shower of splinters in a 15-foot cone. Each creature in that area must make a DC 13 Dexterity saving throw, taking 24 (7d6) piercing damage on a failed save, or half as much damage on a successful one.
"""


def _bundles(text=PROFILE):
    return {
        "chapters": [
            {
                "chapter_id": "chapter_roof",
                "events": [
                    {
                        "id": "event_roof",
                        "encounters": [
                            {
                                "actor_id": "timber_drake",
                                "creature": "Timber Drake",
                                "quantity": 1,
                                "source_stat_block": text,
                                "source_pages": [7],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def test_source_profile_requires_no_catalogue_substitution_and_reports_limits():
    profile = source_profile(PROFILE, name="Timber Drake", source_evidence={"pages": [7]})
    assert (profile["monster_stats"]["hp_max"], profile["monster_stats"]["ac"]) == (75, 16)
    assert profile["text"] == PROFILE
    assert profile["source_evidence"]["pages"] == [7]
    assert mechanical_findings(_bundles(), []) == []
    assert len(profile["capability_findings"]) == 1
    assert "movement is not simulated" in profile["capability_findings"][0]


def test_source_heading_annotation_preserves_exact_evidence_and_all_numbers():
    annotated = PROFILE.replace(
        "Timber Drake\n", "Timber Drake (source-specific modification applied)\n", 1
    )
    profile = source_profile(annotated, name="Timber Drake")
    assert profile["text"] == annotated
    assert profile["monster_stats"] == source_profile(PROFILE, name="Timber Drake")["monster_stats"]
    assert mechanical_findings(_bundles(annotated), []) == []


def test_source_breath_preserves_damage_vulnerability_save_and_recharge():
    stats = parse_monster_stats(PROFILE, name="Timber Drake")
    breath = stats.attacks[-1]
    assert breath.damage_dice == "7d6" and breath.recharge == 5
    assert breath.save == AttackSave("DEX", 13, half_on_success=True)
    assert stats.vulnerabilities[0].damage_type == "fire"
    assert not any("Splinter Breath" in note for note in stats.declared)


def test_source_attack_with_two_distinct_targets_does_not_claim_full_execution():
    text = PROFILE.replace("one target. Hit:", "two seperate targets. Hit:")
    profile = source_profile(text, name="Timber Drake")
    assert any("multiple distinct targets" in note for note in profile["capability_findings"])
    assert profile["monster_stats"]["attacks"][0]["damage_dice"] == "2d10+4"


@pytest.mark.parametrize(
    "text",
    [
        PROFILE + PROFILE,
        PROFILE.replace("Timber Drake\n", "Different Name\n", 1),
        PROFILE.replace("19 (+4) 14 (+2) 17 (+3) 8 (-1) 8 (-1) 4 (-3)", "19 (+4)"),
    ],
)
def test_mixed_misnamed_or_incomplete_source_profiles_need_source_repair(text):
    with pytest.raises(ValueError):
        source_profile(text, name="Timber Drake")
    findings = mechanical_findings(_bundles(text), [])
    assert len(findings) == 1 and findings[0]["severity"] == "material"


def test_verified_base_profile_with_source_weapon_change_keeps_base_and_replacement():
    # A synthetic verified-catalogue fixture represents the exact base text supplied
    # to chapter authoring; the source page then changes only its Fist attack.
    base = """Ape Guard
Medium beast, unaligned
Armor Class 12
Hit Points 19 (3d8 + 6)
Speed 30 ft., climb 30 ft.
STR DEX CON INT WIS CHA
16 (+3) 14 (+2) 14 (+2) 6 (-2) 12 (+1) 7 (-2)
Actions
Multiattack. The ape makes two fist attacks.
Fist. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 6 (1d6 + 3) bludgeoning damage.
Rock. Ranged Weapon Attack: +5 to hit, range 25/50 ft., one target. Hit: 6 (1d6 + 3) bludgeoning damage.
"""
    adjusted = base.replace("two fist attacks", "two slash attacks").replace(
        "Fist. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 6 (1d6 + 3) bludgeoning damage.",
        "Slash. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 10 (2d6 + 3) slashing damage.",
    )
    evidence = {
        "pages": [5],
        "base_catalogue": {"name": "Ape", "source_sha256": "a" * 64},
        "modification": "Replace Fist with greatsword Slash, 10 (2d6+3) slashing damage.",
    }
    profile = source_profile(adjusted, name="Ape Guard", source_evidence=evidence)
    original_stats = parse_monster_stats(base, name="Ape Guard")
    stats = parse_monster_stats(profile["text"], name="Ape Guard")
    assert (stats.ac, stats.hp_max, stats.abilities) == (
        original_stats.ac,
        original_stats.hp_max,
        original_stats.abilities,
    )
    assert stats.multiattack == (("Slash", 2),)
    assert [(a.name, a.bonus, a.damage_dice, a.damage_type) for a in stats.attacks] == [
        ("Slash", 5, "2d6+3", "slashing"),
        ("Rock", 5, "1d6+3", "bludgeoning"),
    ]
    assert profile["source_evidence"] == evidence
    assert all("Slash" not in note for note in profile["capability_findings"])


def test_same_actor_cannot_switch_profiles_without_explicit_form_identity():
    bundles = _bundles()
    event = deepcopy(bundles["chapters"][0]["events"][0])
    event["id"] = "event_again"
    event["encounters"][0]["source_stat_block"] = PROFILE.replace("Hit Points 75", "Hit Points 30")
    bundles["chapters"][0]["events"].append(event)
    findings = mechanical_findings(bundles, [])
    assert len(findings) == 1
    assert "incompatible creature profiles" in findings[0]["evidence"]


def test_repeated_source_profile_formatting_does_not_trigger_paid_repair():
    bundles = _bundles()
    repeated = deepcopy(bundles["chapters"][0]["events"][0])
    repeated["id"] = "event_again"
    repeated["encounters"][0]["source_stat_block"] = PROFILE.replace("\n", "\n\n").replace(
        "reach 5 ft.", "reach  5 ft."
    )
    bundles["chapters"][0]["events"].append(repeated)
    before = deepcopy(bundles)
    assert mechanical_findings(bundles, []) == []
    assert bundles == before


def test_invalid_source_check_gets_specific_chapter_repair_before_compilation():
    bundles = _bundles()
    check = {
        "id": "inspect_relic",
        "action": "Inspect the relic's construction",
        "event_ids": ["event_roof"],
        "check": "intelligence_arcana",
        "dc": 15,
    }
    bundles["chapters"][0]["checks"] = [check]
    findings = mechanical_findings(bundles, [])
    assert len(findings) == 1
    assert findings[0]["severity"] == "material"
    assert findings[0]["record_ids"] == ["chapter_roof_content"]
    assert "unsupported engine check" in findings[0]["evidence"]
    assert check["dc"] == 15 and check["check"] == "intelligence_arcana"
    check["check"] = "arcana"
    assert mechanical_findings(bundles, []) == []


def test_source_spellcasting_variants_keep_slots_and_report_unsupported_entries():
    text = (
        PROFILE[: PROFILE.index("Actions\n")]
        + """Saving Throws Intelligence +5, Wisdom +4
Spellcasting. The mage is a 7th level spellcaster. His spellcasting ability is Intelligence (spell save DC 14, spell attacks +6 to hit). He has the following spells prepared:
Cantrips (at will): Sacred Flame, Mending.
First level (four slots): Cure Wounds, Jump.
Second level (three slots): Levitate.
Third level (three slots): Haste, Slow.
Fourth level (one slot): Polymorph.
Actions
Dagger. Melee Weapon Attack: +5 to hit, reach 5 ft., one target. Hit: 4 (1d4 + 2) piercing damage.
"""
    )
    stats = parse_monster_stats(text, name="Timber Drake")
    assert stats.saves == {"INT": 5, "WIS": 4}
    assert stats.spellcasting.save_dc == 14
    assert stats.spellcasting.attack_bonus == 6
    assert stats.spellcasting.slots == {"1": 4, "2": 3, "3": 3, "4": 1}
    assert stats.spellcasting.spells == ((0, "Sacred Flame"), (1, "Cure Wounds"))
    assert any(
        "unsupported spell list" in note and "not executed" in note for note in stats.declared
    )
    assert "Polymorph" in source_profile(text, name="Timber Drake")["text"]
