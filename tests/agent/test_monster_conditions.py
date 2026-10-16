"""EDG-043 (fourth slice): frightened and stunned on the hero, from exact monster text.

A save action or a rider is executed only when its text reads as the Monster Manual words it: a
dragon's Frightful Presence (Adult Red Dragon, PDF p.99: "must succeed on a DC 19 Wisdom saving throw
or become frightened for 1 minute", with the repeat save and the 24-hour immunity), the mind flayer's
Mind Blast (PDF p.223: "or take 22 (4d8 + 4) psychic damage and be stunned for 1 minute") and a hit's
"must succeed on a DC 11 Wisdom saving throw or be frightened until the end of the scarecrow's next
turn" (Scarecrow Claw, PDF p.269; Empyrean Maul, PDF p.131). The fixtures are synthetic: their
creatures and numbers are written for the test. The conditions follow
PHB Appendix A (PDF pp.291, 293): frightened gives disadvantage on attack rolls and ability checks
while the source stands and forbids moving closer to it; stunned is incapacitated, fails Strength
and Dexterity saves and gives attacks against it advantage. Dice are scripted (`ScriptedDice`).
"""

import json

import pytest

from agent.combatants import (
    add_effect,
    attack_roll_mode,
    can_act,
    effects_of,
    impaired_checks,
    save_auto_fails,
)
from agent.conditions import end_fear_of_defeated, end_of_source_turn, fear_refusal
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import (
    AttackRider,
    AttackSave,
    MonsterStats,
    attack_plan,
    parse_monster_stats,
    recharge_turn_start,
    spend_recharge,
    stats_of,
)
from agent.positioning import MELEE, RANGED
from agent.resolution import resolve_check
from agent.supported_rules import (
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from tests.agent.test_concentration import _entries, _exact_foe, _next_round, _state, _summary
from tests.agent.test_monster_riders import SPITTER_TEXT
from tests.agent.test_monster_save_actions import DRAKE_TEXT
from tests.agent.test_spells import CLERIC, ScriptedDice, _foe as _make_foe
from tools.dice import use_rng

CLERIC_3 = {**CLERIC, "level": 3}  # WIS save +5 (proficient), INT save +0, DEX save +1
WAIT = ParsedInput(intent="other")
ATTACK = ParsedInput(intent="combat", check="attack", target="mind-eater")

PRESENCE = (
    "Frightful Presence. Each creature of the dragon's choice that is within 120 feet of the dragon "
    "and aware of it must succeed on a DC 14 Wisdom saving throw or become frightened for 1 minute. "
    "A creature can repeat the saving throw at the end of each of its turns, ending the effect on "
    "itself on a success. If a creature's saving throw is successful or the effect ends for it, the "
    "creature is immune to the dragon's Frightful Presence for the next 24 hours."
)
WYRM_TEXT = f"""Dread Wyrm
Huge dragon, lawful evil
Armor Class 15 (natural armor)
Hit Points 115 (10d12 + 50)
Speed 40 ft., fly 80 ft.
STR DEX CON INT WIS CHA
19 (+4) 10 (+0) 17 (+3) 14 (+2) 11 (+0) 16 (+3)
Challenge 9 (5,000 XP)
Actions
Multiattack. The dragon can use its Frightful Presence. It then makes three attacks: one with its bite and two with its claws.
Bite. Melee Weapon Attack: +8 to hit, reach 10 ft., one target. Hit: 11 (2d6 + 4) piercing damage.
Claw. Melee Weapon Attack: +8 to hit, reach 5 ft., one target. Hit: 7 (1d8 + 3) slashing damage.
{PRESENCE}
"""
BLAST = (
    "Mind Blast (Recharge 5–6). The mind-eater magically emits psychic energy in a 60-foot cone. Each "
    "creature in that area must succeed on a DC 13 Intelligence saving throw or take 7 (2d6) psychic "
    "damage and be stunned for 1 minute. A creature can repeat the saving throw at the end of each of "
    "its turns, ending the effect on itself on a success."
)
EATER_TEXT = f"""Mind-Eater
Medium aberration, lawful evil
Armor Class 13 (natural armor)
Hit Points 30 (4d8 + 12)
Speed 30 ft.
STR DEX CON INT WIS CHA
11 (+0) 12 (+1) 16 (+3) 19 (+4) 17 (+3) 17 (+3)
Challenge 4 (1,100 XP)
Actions
Claw. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 3 (1d4 + 1) slashing damage.
{BLAST}
"""


STALKER_TEXT = """Straw Stalker
Medium construct, chaotic evil
Armor Class 12 (natural armor)
Hit Points 36 (8d8)
Speed 30 ft.
STR DEX CON INT WIS CHA
11 (+0) 13 (+1) 11 (+0) 10 (+0) 10 (+0) 13 (+1)
Challenge 1 (200 XP)
Actions
Claw. Melee Weapon Attack: +4 to hit, reach 5 ft., one target. Hit: 6 (2d4 + 1) slashing damage. If the target is a creature, it must succeed on a DC 11 Wisdom saving throw or be frightened until the end of the stalker's next turn.
"""
RANGED_STALKER_TEXT = STALKER_TEXT.replace(
    "Melee Weapon Attack: +4 to hit, reach 5 ft., one target.",
    "Ranged Weapon Attack: +4 to hit, range 30/120 ft., one target.",
)
STUN_RIDER_STALKER_TEXT = STALKER_TEXT.replace("be frightened", "be stunned")
RANGED_STUN_RIDER_STALKER_TEXT = RANGED_STALKER_TEXT.replace("be frightened", "be stunned")


def _hero_row(out_or_state: dict) -> dict:
    return next(c for c in out_or_state["combat_state"]["combatants"] if c["is_player"])


def _foe_after(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _afflicted(state: dict, kind: str, source: str, **save: object) -> dict:
    """The hero already carries `kind` from `source` (a save-less effect unless `save` is given)."""
    add_effect(
        _hero_row(state),
        kind,
        source=source,
        round_applied=1,
        expires_after_round=10,
        save=save or None,
    )
    return state


# --- parsing: exact wording only -------------------------------------------------------------------


def test_frightful_presence_is_read_with_its_save_and_is_the_multiattack_opener() -> None:
    stats = parse_monster_stats(WYRM_TEXT, name="Dread Wyrm")
    presence = stats.attacks[-1]
    assert presence.name == "Frightful Presence" and presence.damage_dice == ""
    assert presence.save == AttackSave("WIS", 14, False, "frightened", once=True)
    assert stats.multiattack == (("Frightful Presence", 1), ("Bite", 1), ("Claw", 2))
    assert not any("Frightful Presence" in d for d in stats.declared)
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats


def test_mind_blast_is_read_with_its_damage_and_its_condition() -> None:
    stats = parse_monster_stats(EATER_TEXT, name="Mind-Eater")
    blast = stats.attacks[-1]
    assert (blast.name, blast.recharge, blast.damage_dice, blast.damage_type) == (
        "Mind Blast",
        5,
        "2d6",
        "psychic",
    )
    assert blast.save == AttackSave("INT", 13, False, "stunned")
    assert not any("Mind Blast" in d for d in stats.declared)


def test_a_rider_until_the_end_of_the_next_turn_is_read_without_a_repeat_save() -> None:
    stats = parse_monster_stats(STALKER_TEXT, name="Straw Stalker")
    assert stats.attacks[0].rider == AttackRider("frightened", 11, "WIS", None)
    assert not any("rider" in d for d in stats.declared)
    assert MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict()))) == stats
    stunning = STALKER_TEXT.replace("Wisdom", "Constitution").replace("frightened", "stunned")
    assert parse_monster_stats(stunning, name="Straw Stalker").attacks[0].rider == AttackRider(
        "stunned", 11, "CON", None
    )


@pytest.mark.parametrize(
    "rider",
    [
        "until the start of the stalker's next turn",
        "for 1 minute",
        "until the end of the stalker's next turn and take 3 (1d6) fire damage",
    ],
)
def test_other_rider_wording_stays_declared(rider: str) -> None:
    text = STALKER_TEXT.replace("until the end of the stalker's next turn", rider)
    attack = parse_monster_stats(text, name="Straw Stalker")
    assert attack.attacks[0].rider is None
    assert any("damage rider or extra effect not executed" in d for d in attack.declared)


@pytest.mark.parametrize(
    ("change", "old", "new"),
    [("charmed", "frightened", "charmed"), ("Strength", "Wisdom", "Strength")],
)
def test_a_rider_with_another_condition_or_ability_stays_declared(
    change: str, old: str, new: str
) -> None:
    text = STALKER_TEXT.replace(old, new)
    assert parse_monster_stats(text, name="Straw Stalker").attacks[0].rider is None, change


def test_stored_save_rows_before_conditions_read_with_none() -> None:
    legacy = {
        "name": "Ember Drake",
        "hp_max": 45,
        "ac": 14,
        "attacks": [
            {
                "name": "Cinder Breath",
                "bonus": 0,
                "damage_dice": "2d6",
                "damage_type": "fire",
                "save": {"ability": "DEX", "dc": 13, "half_on_success": True},
            }
        ],
    }
    assert stats_of({"monster_stats": legacy}).attacks[0].save == AttackSave("DEX", 13, True)


@pytest.mark.parametrize(
    "wording",
    [
        PRESENCE.replace("for 1 minute", "for 10 minutes"),
        PRESENCE.replace("within 120 feet", "within 60 feet"),
        PRESENCE.replace("Wisdom", "Charisma"),
        PRESENCE.replace("become frightened", "become charmed"),
        PRESENCE.replace(
            " If a creature's saving throw is successful or the effect ends for it, the creature is "
            "immune to the dragon's Frightful Presence for the next 24 hours.",
            "",
        ),
    ],
)
def test_other_frightful_presence_wording_stays_declared(wording: str) -> None:
    stats = parse_monster_stats(WYRM_TEXT.replace(PRESENCE, wording), name="Dread Wyrm")
    assert [a.name for a in stats.attacks] == ["Bite", "Claw"]
    assert stats.multiattack == (("Bite", 1), ("Claw", 2))  # the opener is not read either
    assert any(d.startswith("Frightful Presence") and "not executed" in d for d in stats.declared)


@pytest.mark.parametrize(
    "wording",
    [
        BLAST.replace("stunned", "paralyzed"),
        BLAST.replace("for 1 minute", "until the end of the mind-eater's next turn"),
        BLAST.replace(" and be stunned for 1 minute", " and be stunned and knocked prone"),
        BLAST.replace(
            " A creature can repeat the saving throw at the end of each of its turns,", ""
        ),
        BLAST.replace("a 60-foot cone", "a 60-foot cone, provided that it is unseen"),
    ],
)
def test_other_mind_blast_wording_stays_declared(wording: str) -> None:
    stats = parse_monster_stats(EATER_TEXT.replace(BLAST, wording), name="Mind-Eater")
    assert [a.name for a in stats.attacks] == ["Claw"]
    assert any(d.startswith("Mind Blast") and "not executed" in d for d in stats.declared)


def test_a_once_action_is_made_first_then_stays_spent_without_a_roll() -> None:
    stats = parse_monster_stats(WYRM_TEXT, name="Dread Wyrm")
    assert [a.name for a in attack_plan(stats)] == [
        "Frightful Presence",
        "Bite",
        "Claw",
        "Claw",
    ]
    foe = {"monster_stats": stats.to_dict()}
    spend_recharge(foe, stats.attacks[-1])
    spend_recharge(foe, stats.attacks[0])  # a plain attack is never recorded as spent
    assert foe["recharge_spent"] == ["Frightful Presence"]
    with use_rng(ScriptedDice([])):  # no d6: the immunity is not a recharge
        assert recharge_turn_start(foe) == []
    assert foe["recharge_spent"] == ["Frightful Presence"]
    assert [a.name for a in attack_plan(stats, spent=foe["recharge_spent"])] == [
        "Bite",
        "Claw",
        "Claw",
    ]


# --- the effects of frightened and stunned ---------------------------------------------------------


def test_frightened_and_stunned_change_the_rolls_as_appendix_a_says() -> None:
    frightened = {"effects": [{"kind": "frightened", "source": "Dread Wyrm"}]}
    stunned = {"effects": [{"kind": "stunned", "source": "Mind-Eater"}]}
    assert attack_roll_mode(frightened, {}) == "disadvantage"  # PHB PDF p.291
    assert attack_roll_mode({}, stunned) == "advantage"  # PHB PDF p.293
    assert attack_roll_mode(stunned, stunned) == "advantage"  # a stunned attacker rolls normally
    assert attack_roll_mode(frightened, stunned) == "normal"
    assert can_act({"hp_current": 5}) and not can_act({**stunned, "hp_current": 5})
    assert impaired_checks(frightened) and not impaired_checks(stunned)
    assert impaired_checks({"effects": [{"kind": "poisoned", "source": "Spitter"}]})  # PDF p.293


def test_a_stunned_combatant_fails_strength_and_dexterity_saves_only() -> None:
    stunned = {"effects": [{"kind": "stunned"}]}
    assert [save_auto_fails(stunned, a) for a in ("STR", "DEX", "CON", "INT", "WIS", "CHA")] == [
        True,
        True,
        False,
        False,
        False,
        False,
    ]
    assert not save_auto_fails({}, "DEX")


@pytest.mark.parametrize(
    ("check", "mode"),
    [("perception", "disadvantage"), ("wis", "disadvantage"), ("dex_save", "normal")],
)
def test_an_impaired_hero_has_disadvantage_on_ability_checks_only(check: str, mode: str) -> None:
    """17 and 4 are rolled for the pair: disadvantage keeps 4. A saving throw is not an ability check."""
    result = resolve_check(CLERIC_3, check, "easy", impaired=True, rng=ScriptedDice([17, 4]))
    assert result.roll_mode == mode
    assert result.natural == (4 if mode == "disadvantage" else 17)


def test_an_impaired_attack_roll_is_left_to_the_effect_modes() -> None:
    assert (
        resolve_check(CLERIC_3, "attack", dc=10, impaired=True, rng=ScriptedDice([9])).natural == 9
    )


def test_fear_restrictions_end_when_its_source_is_down_but_condition_and_save_persist() -> None:
    wyrm = _exact_foe(WYRM_TEXT, "Dread Wyrm", hp=115)
    other = _exact_foe(EATER_TEXT, "Mind-Eater", hp=30)
    hero = {"name": "player"}
    assert end_fear_of_defeated(hero, [wyrm]) is None
    add_effect(
        hero,
        "frightened",
        source="Dread Wyrm",
        source_id="Dread Wyrm 2",
        round_applied=1,
        save={"ability": "WIS", "dc": 14},
        expires_after_round=10,
    )
    second_wyrm = _exact_foe(WYRM_TEXT, "Dread Wyrm 2", hp=115)
    # The stable source ID selects the second same-name foe, not the first.
    assert end_of_source_turn(hero, second_wyrm, 2) == []  # a minute-long fear has no turn-end timer
    assert "cannot willingly move closer" in fear_refusal("Dara", hero, [(MELEE, [second_wyrm])])
    assert fear_refusal("Dara", hero, [(MELEE, [other]), (RANGED, [second_wyrm])]) is None
    wyrm["hp_current"] = 0
    wyrm["disposition"] = "dead"
    assert end_fear_of_defeated(hero, [wyrm, second_wyrm, other]) is None
    assert end_fear_of_defeated(hero, [wyrm, second_wyrm, other]) is None
    # Stable source identity distinguishes the second same-name foe from the standing first one.
    second_wyrm["hp_current"] = 0
    second_wyrm["disposition"] = "dead"
    assert end_fear_of_defeated(hero, [wyrm, second_wyrm, other]) == "Dread Wyrm"
    assert effects_of(hero)[0]["source_defeated"] is True
    assert effects_of(hero)[0]["save"] == {"ability": "WIS", "dc": 14}
    assert attack_roll_mode(hero, {}) == "normal"
    assert not impaired_checks(hero)
    assert fear_refusal("Dara", hero, [(MELEE, [second_wyrm])]) is None
    assert end_fear_of_defeated(hero, [wyrm, second_wyrm, other]) is None
    # The effect still repeats its save and expires normally.
    assert effects_of(hero)[0]["expires_after_round"] == 10


def test_narrated_fear_and_stun_survive_only_when_the_engine_recorded_them() -> None:
    text = "I try to frighten and stun the goblin"
    narration = "The goblin is frightened. It is stunned too. What do you do next?"
    unrecorded = classify_unsupported_actions(text, character=CLERIC_3)
    assert [a.kind for a in unrecorded] == ["condition"]
    cleaned = apply_narration_boundary(narration, unrecorded)
    assert "frightened" not in cleaned and "stunned" not in cleaned
    recorded = EngineApplied(effects=("frightened", "stunned"))
    assert classify_unsupported_actions(text, character=CLERIC_3, applied=recorded) == ()


def test_a_condition_until_the_end_of_a_creatures_next_turn_ends_with_that_turn() -> None:
    hero = {"name": "player"}
    stalker = _exact_foe(STALKER_TEXT, "Straw Stalker", hp=36)
    add_effect(
        hero,
        "frightened",
        source="Straw Stalker",
        round_applied=1,
        ends_with_turn_of="Straw Stalker",
    )
    assert end_of_source_turn(hero, stalker, 1) == []  # its own turn this round does not end it
    assert end_of_source_turn(hero, _exact_foe(EATER_TEXT, "Mind-Eater", hp=30), 2) == []
    assert end_of_source_turn(hero, stalker, 2) == ["frightened"]
    assert effects_of(hero) == []


# --- frightened in the round -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frightful_presence_frightens_the_hero_once_and_it_repeats_the_save(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 1: the wyrm opens its Multiattack with Frightful Presence. Dara's WIS save is 4 + 5 = 9
    vs DC 14, so she is frightened (a minute: rounds 1 to 10); its bite and two claws roll 1 and miss.
    Round 2: Dara's attack rolls a pair (18 and 3, keeping 3 for a miss, 3 + 2 vs AC 15); her repeat
    save is 15 + 5 = 20 and ends the fear; the wyrm does not use the presence again (it is immune for
    24 hours) and makes only its attacks.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact_foe(WYRM_TEXT, "Dread Wyrm", hp=115)], WAIT)
    with use_rng(ScriptedDice([4, 1, 1, 1])):
        first = await combat_round_node(state)
    assert effects_of(_hero_row(first)) == [
        {
            "kind": "frightened",
            "source": "Dread Wyrm",
            "applied_round": 1,
            "expires_after_round": 10,
            "save": {"ability": "WIS", "dc": 14},
            "source_id": "Dread Wyrm",
        }
    ]
    assert (
        "Dara fails the WIS save against Dread Wyrm's Frightful Presence (rolled 9 vs DC 14) and "
        "is frightened." in _summary(first)
    )
    assert _entries(first, "Dara WIS save (Frightful Presence)")[0]["rolls"] == [4]
    assert first["combat_state"]["combatants"][1]["recharge_spent"] == ["Frightful Presence"]
    assert len(_entries(first, "Dread Wyrm Claw attack")) == 2

    follow = _next_round(
        first,
        "I attack the dread wyrm",
        ParsedInput(intent="combat", check="attack", target="dread wyrm"),
        round_num=2,
    )
    with use_rng(ScriptedDice([18, 3, 15, 1, 1, 1])):
        second = await combat_round_node(follow)
    assert _entries(second, "attack check")[0]["roll_mode"] == "disadvantage"
    assert _entries(second, "attack check")[0]["rolls"] == [18, 3]
    assert (
        "Dara repeats its WIS save against frightened (rolled 20 vs DC 14) and ends the effect."
        in _summary(second)
    )
    assert effects_of(_hero_row(second)) == []
    assert _entries(second, "Dara WIS save (Frightful Presence)") == []
    assert len(_entries(second, "Dread Wyrm Bite attack")) == 1


@pytest.mark.asyncio
async def test_a_success_against_frightful_presence_records_nothing(
    fake_llm_schema, fake_retrieval
) -> None:
    """WIS 12 + 5 = 17 meets DC 14: Dara is not frightened, and the wyrm has still used its presence."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact_foe(WYRM_TEXT, "Dread Wyrm", hp=115)], WAIT)
    with use_rng(ScriptedDice([12, 1, 1, 1])):
        out = await combat_round_node(state)
    assert effects_of(_hero_row(out)) == []
    assert "succeeds on the WIS save" in _summary(out) and "is not frightened." in _summary(out)
    assert out["combat_state"]["combatants"][1]["recharge_spent"] == ["Frightful Presence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["frightened", "poisoned"])
async def test_an_impaired_hero_rolls_ability_checks_at_disadvantage_in_combat(
    kind: str, fake_llm_schema, fake_retrieval
) -> None:
    """Frightened (PHB PDF p.291) and poisoned (PDF p.293) give disadvantage on ability checks: the
    perception check rolls 17 and 4 and keeps 4; the Spitter's crossbow then rolls a 1 and misses."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    spitter = {**_exact_foe(SPITTER_TEXT, "Spitter", hp=16), "band": "ranged"}
    state = _afflicted(
        _state(
            "I look around",
            [spitter],
            ParsedInput(intent="exploration", check="perception", difficulty="easy"),
        ),
        kind,
        "Spitter",
    )
    with use_rng(ScriptedDice([17, 4, 1])):
        out = await combat_round_node(state)
    check = _entries(out, "perception check")[0]
    assert check["roll_mode"] == "disadvantage" and check["kept"] == 4


@pytest.mark.asyncio
async def test_a_frightened_hero_cannot_close_in_on_the_source(
    fake_llm_schema, fake_retrieval
) -> None:
    wyrm = {**_exact_foe(WYRM_TEXT, "Dread Wyrm", hp=115), "band": "ranged"}
    state = _afflicted(
        _state(
            "I close in on the dread wyrm",
            [wyrm],
            ParsedInput(intent="combat", check="attack", target="dread wyrm"),
        ),
        "frightened",
        "Dread Wyrm",
    )
    out = await combat_round_node(state)
    assert "frightened of Dread Wyrm and cannot willingly move closer" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1
    assert out["combat_state"]["combatants"][1]["band"] == "ranged"


@pytest.mark.asyncio
async def test_fear_applied_by_an_ahead_foe_blocks_close_in_after_its_turn_without_reverting_it(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {
        **_exact_foe(RANGED_STALKER_TEXT, "Straw Stalker", hp=36),
        "band": RANGED,
        "init": 20,
    }
    state = _state(
        "I close in on Straw Stalker and attack",
        [stalker],
        ParsedInput(intent="combat", check="attack", target="straw stalker"),
    )
    with use_rng(ScriptedDice([15, 1, 1, 3])):
        out = await combat_round_node(state)

    summary = _summary(out)
    assert "Dara is frightened of Straw Stalker and cannot willingly move closer" in summary
    assert "Straw Stalker remains beyond 5 feet" in summary
    assert "cannot reach Straw Stalker with a melee attack" in summary
    assert out["combat_state"]["round"] == 2
    assert _entries(out, "attack check") == []
    assert out["telemetry_events"][0]["payload"]["player_action"]["result"] == "out_of_reach"
    assert _foe_after(out, "Straw Stalker")["band"] == RANGED
    assert [effect["kind"] for effect in effects_of(_hero_row(out))] == ["frightened"]


@pytest.mark.asyncio
async def test_a_failed_same_source_short_rider_refreshes_through_the_sources_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {
        **_exact_foe(RANGED_STALKER_TEXT, "Straw Stalker", hp=36),
        "band": RANGED,
        "init": 20,
    }
    state = _state("I wait and watch", [stalker], WAIT)
    with use_rng(ScriptedDice([15, 1, 1, 1])):
        first = await combat_round_node(state)
    assert effects_of(_hero_row(first))[0]["applied_round"] == 1

    follow = _next_round(first, "I wait and watch", WAIT, round_num=2)
    follow["character"] = {**follow["character"], "hp_current": _hero_row(first)["hp_current"]}
    with use_rng(ScriptedDice([15, 2, 2, 1])):
        second = await combat_round_node(follow)

    effects = effects_of(_hero_row(second))
    assert len(effects) == 1
    assert effects[0]["kind"] == "frightened"
    assert effects[0]["source_id"] == "Straw Stalker"
    assert effects[0]["applied_round"] == 2
    assert effects[0]["expires_after_round"] == 3
    assert effects[0]["ends_with_turn_of"] == "Straw Stalker"
    assert "Dara is no longer frightened" not in _summary(second)


@pytest.mark.asyncio
async def test_a_hero_can_close_in_after_an_ahead_sources_short_fear_expires(
    fake_llm_schema, fake_retrieval
) -> None:
    """Preflight defers its fear refusal because the ahead source's turn expires fear first."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {
        **_exact_foe(RANGED_STALKER_TEXT, "Straw Stalker", hp=36),
        "band": RANGED,
        "init": 20,
    }
    first_state = _state("I wait and watch", [stalker], WAIT)
    with use_rng(ScriptedDice([15, 1, 1, 3])):
        first = await combat_round_node(first_state)
    assert [effect["kind"] for effect in effects_of(_hero_row(first))] == ["frightened"]

    follow = _next_round(
        first,
        "I close in on Straw Stalker and attack",
        ParsedInput(intent="combat", check="attack", target="straw stalker"),
        round_num=2,
    )
    follow["character"] = {**follow["character"], "hp_current": _hero_row(first)["hp_current"]}
    with use_rng(ScriptedDice([1, 10, 3])):
        second = await combat_round_node(follow)

    assert "Dara is no longer frightened." in _summary(second)
    assert _entries(second, "attack check")
    assert _foe_after(second, "Straw Stalker")["band"] == MELEE
    assert effects_of(_hero_row(second)) == []


@pytest.mark.asyncio
async def test_a_persisted_stun_ending_with_an_ahead_foe_does_not_discard_the_heros_declaration(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    spitter = {**_exact_foe(SPITTER_TEXT, "Spitter"), "band": RANGED, "init": 20}
    target = ParsedInput(intent="combat", check="attack", target="spitter")
    state = _state("I close in on Spitter and attack", [spitter], target)
    state["combat_state"]["round"] = 2
    add_effect(
        _hero_row(state),
        "stunned",
        source="Spitter",
        round_applied=1,
        expires_after_round=2,
        ends_with_turn_of="Spitter",
    )

    with use_rng(ScriptedDice([1, 1, 1])):
        out = await combat_round_node(state)

    assert "Dara is no longer stunned." in _summary(out)
    assert _entries(out, "attack check")
    assert _foe_after(out, "Spitter")["band"] == MELEE
    assert effects_of(_hero_row(out)) == []


@pytest.mark.asyncio
async def test_a_flee_declaration_waits_for_a_persisted_stuns_source_turn_to_end(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    spitter = {**_exact_foe(SPITTER_TEXT, "Spitter"), "band": RANGED, "init": 20}
    state = _state("I flee", [spitter], WAIT)
    state["combat_state"]["round"] = 2
    add_effect(
        _hero_row(state),
        "stunned",
        source="Spitter",
        round_applied=1,
        expires_after_round=2,
        ends_with_turn_of="Spitter",
    )

    with use_rng(ScriptedDice([1, 1])):
        out = await combat_round_node(state)

    assert _entries(out, "Spitter Hand Crossbow attack")
    assert "Dara is no longer stunned." in _summary(out)
    assert "Dara disengages and flees the fight." in _summary(out)
    assert out["combat_state"]["ended"] is True
    assert out["combat_state"]["outcome"] == "fled"
    assert effects_of(_hero_row(out)) == []


@pytest.mark.asyncio
async def test_a_flee_does_not_escape_if_the_ahead_source_reapplies_stun(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {
        **_exact_foe(RANGED_STUN_RIDER_STALKER_TEXT, "Straw Stalker", hp=36),
        "band": RANGED,
        "init": 20,
    }
    state = _state("I flee", [stalker], WAIT)
    state["combat_state"]["round"] = 2
    add_effect(
        _hero_row(state),
        "stunned",
        source="Straw Stalker",
        round_applied=1,
        expires_after_round=2,
        ends_with_turn_of="Straw Stalker",
    )

    with use_rng(ScriptedDice([15, 18, 1, 1, 1])):
        out = await combat_round_node(state)

    assert "is stunned." in _summary(out)
    assert "Dara flees the fight." not in _summary(out)
    assert out["combat_state"]["ended"] is False
    assert out["combat_state"]["outcome"] is None
    assert effects_of(_hero_row(out))[0]["kind"] == "stunned"
    assert effects_of(_hero_row(out))[0]["applied_round"] == 2


@pytest.mark.asyncio
async def test_dodge_does_not_cancel_advantage_after_a_later_foe_stuns_the_hero(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {
        **_exact_foe(RANGED_STUN_RIDER_STALKER_TEXT, "Straw Stalker", hp=36),
        "init": 10,
    }
    goblin = {**_make_foe("Goblin", hp=20), "band": MELEE, "init": 5}
    state = _state("I take the dodge action", [stalker, goblin], WAIT)

    with use_rng(ScriptedDice([15, 18, 1, 1, 1, 3, 12, 1])):
        out = await combat_round_node(state)

    first = _entries(out, "Straw Stalker Claw attack")[0]
    second = _entries(out, "Goblin attack")[0]
    assert first["roll_mode"] == "disadvantage" and first["rolls"] == [15, 18]
    assert second["roll_mode"] == "advantage" and second["rolls"] == [3, 12]
    assert [effect["kind"] for effect in effects_of(_hero_row(out))] == [
        "dodging",
        "stunned",
    ]


# --- stunned in the round --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mind_blast_stuns_the_hero_and_the_foe_attacks_with_advantage(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 1: the ready Mind Blast replaces the claw. Dara's INT save is 5 + 0 vs DC 13, so she takes
    3 + 4 = 7 psychic damage (12 to 5 HP) and is stunned. Round 2: Dara takes no action though she
    declares an attack; her repeat save (4) keeps the stun; the Mind-Eater's d6 (3) does not recharge
    the blast, so it claws with advantage (2 and 15 keep 15, 19 vs AC 14) for 2 + 1 = 3 (5 to 2 HP).
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact_foe(EATER_TEXT, "Mind-Eater", hp=30)], WAIT)
    with use_rng(ScriptedDice([5, 3, 4])):
        first = await combat_round_node(state)
    assert _hero_row(first)["hp_current"] == 5
    assert effects_of(_hero_row(first)) == [
        {
            "kind": "stunned",
            "source": "Mind-Eater",
            "applied_round": 1,
            "expires_after_round": 10,
            "save": {"ability": "INT", "dc": 13},
            "source_id": "Mind-Eater",
        }
    ]
    assert (
        "Dara fails the INT save against Mind-Eater's Mind Blast (rolled 5 vs DC 13) and takes 7 "
        "psychic damage and is stunned." in _summary(first)
    )
    assert not can_act(_hero_row(first))

    follow = _next_round(first, "I attack the mind-eater", ATTACK, round_num=2)
    follow["character"] = {**follow["character"], "hp_current": 5}  # the sheet carries the damage
    with use_rng(ScriptedDice([4, 3, 2, 15, 2])):
        second = await combat_round_node(follow)
    assert "Dara is stunned and cannot act." in _summary(second)
    assert _entries(second, "attack check") == []
    assert (
        "Dara repeats its INT save against stunned (rolled 4 vs DC 13) and keeps the effect."
        in (_summary(second))
    )
    claw = _entries(second, "Mind-Eater Claw attack")[0]
    assert claw["roll_mode"] == "advantage" and claw["rolls"] == [2, 15]
    assert _hero_row(second)["hp_current"] == 2
    assert second["combat_state"]["player_ac"] == 14 and not second["combat_state"]["ended"]


@pytest.mark.asyncio
async def test_the_hero_acts_again_once_the_repeat_save_ends_the_stun(
    fake_llm_schema, fake_retrieval
) -> None:
    """The stun ends at the end of Dara's turn (15 + 0 vs DC 13): that turn is still lost. The
    Mind-Eater's d6 (6) recharges its blast; her INT save (20) succeeds, so she takes no damage and
    is not stunned again."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    eater = {**_exact_foe(EATER_TEXT, "Mind-Eater", hp=30), "recharge_spent": ["Mind Blast"]}
    state = _afflicted(
        _state("I attack the mind-eater", [eater], ATTACK),
        "stunned",
        "Mind-Eater",
        ability="INT",
        dc=13,
    )
    with use_rng(ScriptedDice([15, 6, 20, 1, 1])):
        out = await combat_round_node(state)
    summary = _summary(out)
    assert "Dara is stunned and cannot act." in summary
    assert (
        "Dara repeats its INT save against stunned (rolled 15 vs DC 13) and ends the effect."
        in (summary)
    )
    assert (
        "Dara succeeds on the INT save against Mind-Eater's Mind Blast (rolled 20 vs DC 13) and "
        "takes no damage and is not stunned." in summary
    )
    assert effects_of(_hero_row(out)) == [] and _hero_row(out)["hp_current"] == 12


@pytest.mark.asyncio
async def test_a_foe_ahead_of_the_hero_in_initiative_stuns_it_before_its_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    """The Mind-Eater acts first (initiative 20 against 15): its blast stuns Dara (5 vs DC 13, 7
    damage), so her declared attack is not made this very round; her repeat save (4) keeps the stun."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    eater = {**_exact_foe(EATER_TEXT, "Mind-Eater", hp=30), "init": 20}
    state = _state("I attack the mind-eater", [eater], ATTACK)
    with use_rng(ScriptedDice([5, 3, 4, 4])):
        out = await combat_round_node(state)
    assert _entries(out, "attack check") == []
    assert "Dara is stunned and cannot act." in _summary(out)
    assert [e["kind"] for e in effects_of(_hero_row(out))] == ["stunned"]
    assert (
        out["combat_state"]["combatants"][0]["hp_current"] == 30
    )  # the Mind-Eater, first in order


@pytest.mark.asyncio
async def test_a_stunned_hero_cannot_flee(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _afflicted(
        _state("I run away from the fight", [_exact_foe(SPITTER_TEXT, "Spitter", hp=16)], WAIT),
        "stunned",
        "Spitter",
    )
    with use_rng(
        ScriptedDice([1])
    ):  # advantage against the stunned hero cancels the crossbow's disadvantage
        out = await combat_round_node(state)
    assert not out["combat_state"]["ended"]
    assert "Dara is stunned and cannot act." in _summary(out)


@pytest.mark.asyncio
async def test_a_stunned_hero_fails_a_dexterity_save_without_a_roll(
    fake_llm_schema, fake_retrieval
) -> None:
    """PHB PDF p.293: no d20 is rolled for the breath's Dexterity save, so the 3 + 4 fire damage is full."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _afflicted(
        _state("I wait and watch", [_exact_foe(DRAKE_TEXT, "Ember Drake", hp=45)], WAIT),
        "stunned",
        "Ember Drake",
    )
    with use_rng(ScriptedDice([3, 4])):
        out = await combat_round_node(state)
    assert _entries(out, "Dara DEX save (Cinder Breath)") == []
    assert (
        "Dara fails the DEX save against Ember Drake's Cinder Breath (automatic failure while "
        "stunned vs DC 13) and takes 7 fire damage." in _summary(out)
    )
    assert _hero_row(out)["hp_current"] == 5


@pytest.mark.asyncio
async def test_stun_ends_concentration_after_damage_save_succeeds(
    fake_llm_schema, fake_retrieval
) -> None:
    """The blast deals 7 damage. Dara succeeds on its DC 10 concentration save (8 + 2), then the
    failed INT save applies stun, which ends Hold Person immediately despite the successful save."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    eater = {**_exact_foe(EATER_TEXT, "Mind-Eater", hp=30), "init": 20}
    state = _state("I wait and watch", [eater], WAIT)
    hero = _hero_row(state)
    hero["concentration"] = {
        "spell": "Hold Person",
        "round_applied": 1,
        "expires_after_round": 10,
    }

    # The eater attacks with its claw after the blast; advantage against the stunned hero is 2, 2,
    # then 1 on its damage die.
    with use_rng(ScriptedDice([5, 3, 4, 8, 1, 2, 2, 1])):
        out = await combat_round_node(state)

    assert "Dara keeps concentration on Hold Person (rolled 10 vs DC 10)." in _summary(out)
    assert "Dara's concentration on Hold Person ends while incapacitated." in _summary(out)
    assert _entries(out, "Dara Constitution save (concentration)")[0]["rolls"] == [8]
    assert _hero_row(out).get("concentration") is None
    assert [effect["kind"] for effect in effects_of(_hero_row(out))] == ["stunned"]


@pytest.mark.asyncio
async def test_a_persisted_stun_drops_concentration_when_combat_is_loaded(
    fake_llm_schema, fake_retrieval
) -> None:
    """An incapacitating condition in a reloaded combat row ends concentration before the hero acts."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    eater = _exact_foe(EATER_TEXT, "Mind-Eater", hp=30)
    eater["effects"] = [{"kind": "stunned", "source": "another monster", "applied_round": 1}]
    state = _afflicted(
        _state("I attack the mind-eater", [eater], ATTACK),
        "stunned",
        "old monster effect",
        ability="INT",
        dc=13,
    )
    hero = _hero_row(state)
    hero["concentration"] = {
        "spell": "Hold Person",
        "round_applied": 1,
        "expires_after_round": 10,
    }

    with use_rng(ScriptedDice([1])):
        out = await combat_round_node(state)

    assert "Dara's concentration on Hold Person ends while incapacitated." in _summary(out)
    assert _hero_row(out).get("concentration") is None
    assert "Dara is stunned and cannot act." in _summary(out)
    assert _entries(out, "attack check") == []
    assert _entries(out, "Mind-Eater Claw attack") == []


# --- a rider until the end of the attacker's next turn, in the round --------------------------------


@pytest.mark.asyncio
async def test_a_rider_frightens_until_the_end_of_the_attackers_next_turn_when_it_acts_after_the_hero(
    fake_llm_schema, fake_retrieval
) -> None:
    """Round 1: the stalker's claw hits (15 + 4 vs AC 14) for 2 + 3 + 1 = 6, and Dara's WIS save is
    4 + 5 = 9 vs DC 11: frightened until the end of its next turn (round 2), with no repeat save.
    Round 2: Dara's attack has disadvantage (18 and 3), then the stalker's turn ends the fear.
    """
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state("I wait and watch", [_exact_foe(STALKER_TEXT, "Straw Stalker", hp=36)], WAIT)
    with use_rng(ScriptedDice([15, 2, 3, 4])):
        first = await combat_round_node(state)
    assert _hero_row(first)["hp_current"] == 6
    assert effects_of(_hero_row(first)) == [
        {
            "kind": "frightened",
            "source": "Straw Stalker",
            "applied_round": 1,
            "expires_after_round": 2,
            "ends_with_turn_of": "Straw Stalker",
            "source_id": "Straw Stalker",
        }
    ]
    assert "Dara fails the WIS save against Claw (rolled 9 vs DC 11) and is frightened." in (
        _summary(first)
    )

    follow = _next_round(
        first,
        "I attack the straw stalker",
        ParsedInput(intent="combat", check="attack", target="straw stalker"),
        round_num=2,
    )
    follow["character"] = {**follow["character"], "hp_current": 6}
    with use_rng(ScriptedDice([18, 3, 1])):
        second = await combat_round_node(follow)
    assert _entries(second, "attack check")[0]["roll_mode"] == "disadvantage"
    assert "Dara is no longer frightened." in _summary(second)
    assert effects_of(_hero_row(second)) == []


@pytest.mark.asyncio
async def test_a_rider_from_a_foe_ahead_of_the_hero_ends_before_the_heros_next_turn(
    fake_llm_schema, fake_retrieval
) -> None:
    """The stalker acts first (initiative 20). Round 1: it frightens Dara, whose attack then has
    disadvantage (18 and 3). Round 2: its turn ends the fear before Dara acts, so her roll is normal."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    stalker = {**_exact_foe(STALKER_TEXT, "Straw Stalker", hp=36), "init": 20}
    target = ParsedInput(intent="combat", check="attack", target="straw stalker")
    state = _state("I attack the straw stalker", [stalker], target)
    with use_rng(ScriptedDice([15, 2, 3, 4, 18, 3])):
        first = await combat_round_node(state)
    assert _entries(first, "attack check")[0]["roll_mode"] == "disadvantage"
    assert [e["kind"] for e in effects_of(_hero_row(first))] == ["frightened"]

    follow = _next_round(first, "I attack the straw stalker", target, round_num=2)
    follow["character"] = {**follow["character"], "hp_current": 6}
    with use_rng(ScriptedDice([1, 1])):
        second = await combat_round_node(follow)
    assert _entries(second, "attack check")[0]["rolls"] == [1]  # a single die: no disadvantage
    assert effects_of(_hero_row(second)) == []
