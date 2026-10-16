"""EDG-021: typed restraint and prone effects from supported contested maneuvers."""

import copy
import json
import random

import pytest

from agent.combatants import (
    add_effect,
    apply_defeat,
    attack_roll_mode,
    classify_combat_maneuver,
    clear_effects,
    effects_of,
    has_effect,
    narrator_roster,
    release_effect,
    view_fields,
)
from agent.graph_combat import combat_round_node, initiative_view
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes.narrator import build_narrator_prompt
from agent.supported_rules import (
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng

# --- pure effect model -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attacker", "target", "ranged", "mode"),
    [
        ({}, {}, False, "normal"),
        ({"effects": [{"kind": "restrained"}]}, {}, False, "disadvantage"),
        ({"effects": [{"kind": "prone"}]}, {}, False, "disadvantage"),
        ({}, {"effects": [{"kind": "restrained"}]}, False, "advantage"),
        ({}, {"effects": [{"kind": "restrained"}]}, True, "advantage"),
        ({}, {"effects": [{"kind": "prone"}]}, False, "advantage"),
        ({}, {"effects": [{"kind": "prone"}]}, True, "disadvantage"),
        # Opposing sources cancel to normal, as the rules do.
        ({"effects": [{"kind": "prone"}]}, {"effects": [{"kind": "restrained"}]}, False, "normal"),
        ({}, {"effects": [{"kind": "restrained"}, {"kind": "prone"}]}, True, "normal"),
    ],
)
def test_attack_roll_mode_follows_combatant_effects(attacker, target, ranged, mode) -> None:
    assert attack_roll_mode(attacker, target, ranged=ranged) == mode


@pytest.mark.parametrize(
    ("ranged", "within_five_feet", "mode"),
    [
        (False, False, "advantage"),
        # PHB Appendix A, Prone (PDF p.293): an attack roll against a prone creature has advantage
        # if the attacker is within 5 feet of it, otherwise disadvantage. A ranged attack made
        # within 5 feet of a hostile creature has its own disadvantage (PHB p.195), so they cancel.
        (True, True, "normal"),
        (True, False, "disadvantage"),
    ],
)
def test_prone_target_grants_advantage_within_five_feet_even_for_a_ranged_attack(
    ranged, within_five_feet, mode
) -> None:
    prone = {"effects": [{"kind": "prone"}]}
    assert attack_roll_mode({}, prone, ranged=ranged, within_five_feet=within_five_feet) == mode


@pytest.mark.parametrize(
    "raw",
    [None, "restrained", 7, {"kind": "restrained"}, [None, "x", {"kind": "stunned"}]],
)
def test_malformed_or_legacy_effects_read_as_none(raw) -> None:
    combatant = {"name": "Raider", "hp_current": 5}
    if raw is not None:
        combatant["effects"] = raw
    assert effects_of(combatant) == []
    assert attack_roll_mode(combatant, combatant) == "normal"


def test_add_release_and_clear_are_explicit_and_idempotent() -> None:
    raider = _enemy("Raider", hp=5)
    assert "effects" not in raider  # legacy shape stays untouched until an effect is applied
    assert add_effect(raider, "restrained", source="rope", round_applied=2) is True
    assert add_effect(raider, "restrained", source="hands", round_applied=3) is False
    assert effects_of(raider) == [{"kind": "restrained", "source": "rope", "applied_round": 2}]
    assert release_effect(raider, "restrained") is True
    assert release_effect(raider, "restrained") is False
    assert effects_of(raider) == []
    add_effect(raider, "prone", source="shove", round_applied=1)
    clear_effects([raider, _enemy("Wolf", hp=3)])
    assert effects_of(raider) == [] and "effects" in raider
    wolf = _enemy("Wolf", hp=3)
    clear_effects([wolf])
    assert "effects" not in wolf  # clearing a row without effects writes nothing

    with pytest.raises(ValueError):
        add_effect(raider, "stunned", source=None, round_applied=1)


def test_defeat_ends_the_effects_of_the_defeated_combatant() -> None:
    raider = _enemy("Raider", hp=5)
    add_effect(raider, "restrained", source="hands", round_applied=1)
    apply_defeat(raider, "nonlethal")
    assert effects_of(raider) == [] and raider["disposition"] == "unconscious"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I grapple the raider with my rope", ("restrained", "rope")),
        ("I pin him to the floor with my hands", ("restrained", "hands")),
        ("I restrain the goblin with the chain", ("restrained", "rope")),
        ("I shove the goblin and pin it to the floor", ("restrained", "hands")),
        ("I shove the raider prone", ("prone", "shove")),
        ("I knock the raider down", ("prone", "shove")),
        ("I trip the goblin", ("prone", "shove")),
        ("I let go of the raider", ("release", None)),
        ("I release the goblin", ("release", None)),
        ("I shove the raider away from me", None),
        ("I shove back", None),
        ("I attack the goblin", None),
        ("I knock him out", None),
    ],
)
def test_declared_maneuvers_are_read_from_the_players_words(text, expected) -> None:
    maneuver = classify_combat_maneuver(text)
    got = None if maneuver is None else (maneuver.kind, maneuver.source)
    assert got == expected


def test_roster_text_states_the_engine_effect_for_the_named_foe_only() -> None:
    raider = _enemy("Raider", hp=5)
    raider2 = _enemy("Raider 2", hp=5)
    add_effect(raider2, "restrained", source="rope", round_applied=1)
    text = narrator_roster([_player(), raider, raider2], ["Raider 2"])
    lines = {line.split('"')[1]: line for line in text.splitlines() if line.startswith("- id")}
    assert lines["Raider"].endswith("active, can act")
    assert lines["Raider 2"].endswith(
        "active, can act; restrained with rope: its attacks have disadvantage and attacks "
        "against it have advantage"
    )
    assert "Combatants able to act now: Raider, Raider 2." in text


def test_legacy_roster_text_is_unchanged_without_effects() -> None:
    text = narrator_roster([_player(), _enemy("Raider", hp=5)], ["Raider"])
    assert '- id "Raider", name "Raider": active, can act\n' in text
    assert "restrained" not in text and "prone" not in text


def test_effects_survive_a_json_round_trip_and_reach_the_hud_view() -> None:
    raider = _enemy("Raider", hp=5)
    add_effect(raider, "restrained", source="hands", round_applied=4)
    stored = json.loads(json.dumps([_player(init=20), raider]))  # the JSONB row
    reloaded = {c["name"]: c for c in stored}
    assert attack_roll_mode(_player(), reloaded["Raider"]) == "advantage"
    view = {c["name"]: c for c in initiative_view(stored)}
    assert view["Raider"]["effects"] == [
        {"kind": "restrained", "source": "hands", "applied_round": 4}
    ]
    assert view["Eda"]["effects"] == []
    assert view_fields(reloaded["Raider"])["effects"][0]["kind"] == "restrained"


# --- combat round: the engine rolls the contest and applies the effect ----------------------


def _contest_won(out: dict) -> bool:
    log = out["adjudication_result"].dice_log
    mine = next(d for d in log if d.get("purpose") == "athletics contest")
    foe = next(d for d in log if str(d.get("purpose", "")).endswith(" contest") and d is not mine)
    return mine["total"] > foe["total"]


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


async def _round(fake, combatants: list[dict], text: str, parsed: ParsedInput, seed: int) -> dict:
    state = _state(combatants, text)
    state["parsed_input"] = parsed
    with use_rng(random.Random(seed)):
        return await combat_round_node(state)


async def _find_contest(fake, combatants_for, text, parsed, won: bool) -> dict:
    """First seed whose engine contest has the wanted outcome (the dice decide, not the test)."""
    for seed in range(300):
        out = await _round(fake, combatants_for(), text, parsed, seed)
        if _contest_won(out) is won:
            return out
    pytest.fail(f"no seed in range produced a contest won={won}")


def _foe(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _summary(out: dict) -> str:
    return out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_won_grapple_restrains_and_the_foe_attacks_at_disadvantage(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    out = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my hands",
        parsed,
        won=True,
    )
    raider = _foe(out, "Raider")
    assert effects_of(raider) == [{"kind": "restrained", "source": "hands", "applied_round": 1}]
    assert "restrains Raider with hands." in _summary(out)
    assert "contest: Eda athletics" in _summary(out)
    # Restrained creatures still attack (5e), but with disadvantage: the kept die is the lower one.
    attack = _entries(out, "Raider attack")[0]
    assert attack["roll_mode"] == "disadvantage"
    assert attack["kept"] == min(attack["rolls"]) and len(attack["rolls"]) == 2
    assert attack["total"] == attack["kept"] + attack["modifier"]
    assert "(rolled" in _summary(out) and " at disadvantage vs AC" in _summary(out)


@pytest.mark.asyncio
async def test_lost_grapple_records_no_effect_and_the_foe_attacks_normally(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    out = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my hands",
        parsed,
        won=False,
    )
    assert effects_of(_foe(out, "Raider")) == []
    assert "fails to hold Raider." in _summary(out)
    attack = _entries(out, "Raider attack")[0]
    assert "mode" not in attack and len(attack["rolls"]) == 1
    # The engine still ruled the maneuver: the summary states that no effect was recorded.
    assert "Narrative ruling: this maneuver has no engine effect this turn." in _summary(out)


@pytest.mark.asyncio
async def test_restraint_persists_across_rounds_and_the_players_attack_gets_advantage(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    first = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5, hp=30)],
        "I grapple the raider with my rope",
        parsed,
        won=True,
    )
    # The round's combatants round-trip through JSON (the persisted row) before round two.
    saved = json.loads(json.dumps(first["combat_state"]["combatants"]))
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="raider"
    )
    follow = _state(saved, "I attack the raider")
    follow["parsed_input"] = ParsedInput(intent="combat", check="attack", target="raider")
    with use_rng(random.Random(4)):
        second = await combat_round_node(follow)
    assert effects_of(_foe(second, "Raider")) == [
        {"kind": "restrained", "source": "rope", "applied_round": 1}
    ]
    player_attack = _entries(second, "attack check")[0]
    assert player_attack["roll_mode"] == "advantage"
    assert _entries(second, "Raider attack")[0]["roll_mode"] == "disadvantage"


@pytest.mark.asyncio
async def test_declared_release_ends_the_restraint_explicitly(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    won = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my hands",
        parsed,
        won=True,
    )
    released = await _round(
        fake_llm_schema,
        won["combat_state"]["combatants"],
        "I let go of the raider",
        ParsedInput(intent="combat", target="raider"),
        seed=2,
    )
    assert effects_of(_foe(released, "Raider")) == []
    assert "releases Raider; it is no longer restrained." in _summary(released)
    # No contest is rolled for a release, and the freed foe attacks normally afterwards.
    assert _entries(released, "athletics contest") == []
    assert "mode" not in _entries(released, "Raider attack")[0]


@pytest.mark.asyncio
async def test_release_with_nothing_bound_is_a_plain_no_op(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out = await _round(
        fake_llm_schema,
        [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I release the raider",
        ParsedInput(intent="combat", target="raider"),
        seed=1,
    )
    assert "has nothing bound to release." in _summary(out)
    assert _entries(out, "athletics contest") == []


@pytest.mark.asyncio
async def test_shove_prone_gives_advantage_within_five_feet_and_disadvantage_beyond(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    won = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5, hp=30)],
        "I shove the raider to the ground",
        parsed,
        won=True,
    )
    assert effects_of(_foe(won, "Raider")) == [
        {"kind": "prone", "source": "shove", "applied_round": 1}
    ]
    assert "shoves Raider prone." in _summary(won)
    assert _entries(won, "Raider attack")[0]["roll_mode"] == "disadvantage"  # prone attackers

    # Each round mutates its combatants in place, so every follow-up gets its own copy.
    saved = won["combat_state"]["combatants"]
    melee = await _round(
        fake_llm_schema,
        copy.deepcopy(saved),
        "I attack the raider",
        ParsedInput(intent="combat", check="attack", target="raider"),
        seed=6,
    )
    assert _entries(melee, "attack check")[0]["roll_mode"] == "advantage"
    # A ranged attack from within 5 feet (the melee band) has advantage against a prone foe, and
    # its own within-5-feet disadvantage cancels it; from beyond 5 feet it has disadvantage.
    shoot = ParsedInput(intent="combat", check="attack", target="raider")
    close = await _round(
        fake_llm_schema, copy.deepcopy(saved), "I shoot the raider with my bow", shoot, seed=6
    )
    assert "roll_mode" not in _entries(close, "attack check")[0]  # one plain d20
    far = copy.deepcopy(saved)
    _foe({"combat_state": {"combatants": far}}, "Raider")["band"] = "ranged"
    distant = await _round(fake_llm_schema, far, "I shoot the raider with my bow", shoot, seed=6)
    assert _entries(distant, "attack check")[0]["roll_mode"] == "disadvantage"


@pytest.mark.asyncio
async def test_unsupported_shove_away_is_ruled_and_rolls_no_contest(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out = await _round(
        fake_llm_schema,
        [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I shove the raider away from me",
        ParsedInput(intent="combat", check="athletics", target="raider"),
        seed=3,
    )
    assert effects_of(_foe(out, "Raider")) == []
    assert _entries(out, "athletics contest") == []
    assert "Narrative ruling: this maneuver has no engine effect" in _summary(out)


@pytest.mark.asyncio
async def test_two_similarly_named_foes_only_the_named_one_is_restrained(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="Raider 2")
    out = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5), _enemy("Raider 2", init=4)],
        "I grapple Raider 2 with my hands",
        parsed,
        won=True,
    )
    assert has_effect(_foe(out, "Raider 2"), "restrained")
    assert effects_of(_foe(out, "Raider")) == []


@pytest.mark.asyncio
async def test_victory_ends_every_effect_in_the_encounter(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    for seed in range(300):
        raider = _enemy("Raider", init=5, hp=1)
        add_effect(raider, "restrained", source="rope", round_applied=1)
        out = await _round(
            fake_llm_schema,
            [_player(init=20, ac=99), raider],
            "I attack the raider",
            ParsedInput(intent="combat", check="attack", target="raider"),
            seed=seed,
        )
        if out["combat_state"]["outcome"] == "victory":
            break
    else:
        pytest.fail("no seed produced a victory")
    assert _foe(out, "Raider")["disposition"] == "defeated"
    assert all(effects_of(c) == [] for c in out["combat_state"]["combatants"])


@pytest.mark.asyncio
async def test_legacy_combatants_without_effects_fight_normally(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    legacy = _enemy("Raider", init=5)
    out = await _round(
        fake_llm_schema,
        [_player(init=20, ac=99), legacy],
        "I wait",
        ParsedInput(intent="combat", target="raider"),
        seed=1,
    )
    raider = _foe(out, "Raider")
    assert "effects" not in raider
    assert _entries(out, "Raider attack")[0].get("mode") is None


@pytest.mark.asyncio
async def test_narration_keeps_a_real_restraint_and_strips_a_claim_without_one(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    parsed = ParsedInput(intent="combat", target="raider")
    fake_llm_schema.narration = (
        "You loop your rope round the raider. It is restrained by your rope now. "
        "What do you do next?"
    )
    won = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my rope",
        parsed,
        won=True,
    )
    assert "restrained by your rope now" in won["narration"]

    fake_llm_schema.narration = (
        "You loop your rope round the raider. It is pinned to the wall by your rope. "
        "What do you do next?"
    )
    lost = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my rope",
        parsed,
        won=False,
    )
    assert "pinned to the wall" not in lost["narration"]
    assert "What do you do next?" in lost["narration"]
    assert "Narrative ruling" in lost["narration"]


@pytest.mark.asyncio
async def test_narrator_prompt_carries_the_restraint_from_the_engine(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out = await _find_contest(
        fake_llm_schema,
        lambda: [_player(init=20, ac=99), _enemy("Raider", init=5)],
        "I grapple the raider with my hands",
        ParsedInput(intent="combat", target="raider"),
        won=True,
    )
    state = _state(out["combat_state"]["combatants"], "I grapple the raider with my hands")
    prompt = build_narrator_prompt(
        {
            **state,
            "combat_state": out["combat_state"],
            "adjudication_result": out["adjudication_result"],
        }
    )[1].content
    assert 'name "Raider": active, can act; restrained with hands' in prompt


# --- pure contract helpers used by the narrator/adjudicator boundary -----------------------


def test_classifier_keeps_unsupported_maneuvers_and_drops_only_applied_ones() -> None:
    text = "I pin the goblin to the floor"
    assert [a.kind for a in classify_unsupported_actions(text)] == ["maneuver"]
    applied = classify_unsupported_actions(text, applied=EngineApplied(effects=("restrained",)))
    assert applied == ()
    shove_away = classify_unsupported_actions(
        "I shove the goblin away", applied=EngineApplied(effects=("restrained",))
    )
    assert [a.kind for a in shove_away] == ["maneuver"]


def test_sanitizer_strips_only_claims_the_engine_did_not_apply() -> None:
    narration = "The goblin is pinned to the wall. What now?"
    unapplied = classify_unsupported_actions("I pin the goblin")
    cleaned = apply_narration_boundary(narration, unapplied)
    assert "pinned" not in cleaned and "What now?" in cleaned
    assert "Narrative ruling" in cleaned
    applied = classify_unsupported_actions(
        "I pin the goblin", applied=EngineApplied(effects=("restrained",))
    )
    assert apply_narration_boundary(narration, applied) == narration
