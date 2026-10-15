"""Solo encounter budget (EDG-148): the engine fits a new roster to the hero's level."""

import pytest

from agent.encounter_budget import (
    adaptation_sentence,
    apply_solo_budget,
    clamp_damage,
    clamp_level,
    solo_budget,
)
from agent.graph_combat import (
    ENCOUNTER_BUILDER,
    EncounterSetup,
    EnemyStat,
    _build_enemy_roster,
    _creature_source_reference,
    combat_round_node,
)
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.supported_rules import SOLO_ADAPTATIONS
from apps.api.services.turn_runner import combat_state_public
from tests.agent.test_combat import _state
from tests.agent.test_creature_encounters import install_record

# --- the budget table ----------------------------------------------------------------------


def test_level_one_budget_keeps_the_prompt_promised_bands() -> None:
    b = solo_budget(1)
    assert b.max_foes == 2
    assert b.boss_hp == (18, 30)
    assert b.foe_hp == (5, 11)
    assert b.total_hp == 30 + 11
    assert b.attack_bonus_max == 5
    assert b.damage_avg_max == 5


@pytest.mark.parametrize(
    ("level", "max_foes", "boss_hp", "foe_hp", "attack_max", "damage_max"),
    [
        (2, 2, (23, 38), (6, 14), 5, 6),
        (4, 2, (32, 53), (9, 19), 5, 9),
        (5, 3, (36, 60), (10, 22), 6, 10),
        (8, 3, (50, 83), (14, 30), 6, 14),
        (9, 4, (54, 90), (15, 33), 7, 15),
        (12, 4, (68, 113), (19, 41), 7, 19),
        (13, 4, (72, 120), (20, 44), 8, 20),
        (20, 4, (104, 173), (29, 63), 9, 29),
    ],
)
def test_budget_scales_with_level(level, max_foes, boss_hp, foe_hp, attack_max, damage_max) -> None:
    b = solo_budget(level)
    assert (b.max_foes, b.boss_hp, b.foe_hp) == (max_foes, boss_hp, foe_hp)
    assert b.total_hp == boss_hp[1] + (max_foes - 1) * foe_hp[1]
    assert (b.attack_bonus_max, b.damage_avg_max) == (attack_max, damage_max)


def test_every_level_from_one_to_twenty_is_monotonic_and_well_formed() -> None:
    budgets = [solo_budget(level) for level in range(1, 21)]
    for lo, hi in zip(budgets, budgets[1:], strict=False):
        assert hi.max_foes >= lo.max_foes and hi.total_hp >= lo.total_hp
        assert hi.boss_hp[1] >= lo.boss_hp[1] and hi.foe_hp[1] >= lo.foe_hp[1]
        assert hi.attack_bonus_max >= lo.attack_bonus_max
    assert all(b.foe_hp[0] < b.foe_hp[1] < b.boss_hp[0] < b.boss_hp[1] for b in budgets)


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 1), ("", 1), ("x", 1), (0, 1), (-4, 1), ("7", 7), (25, 20)]
)
def test_level_is_clamped_to_the_supported_range(raw, expected) -> None:
    assert clamp_level(raw) == expected
    assert solo_budget(raw).level == expected


# --- clamping and trimming -----------------------------------------------------------------


def foe(name, *, hp=9, bonus=3, dice="1d6+1", boss=False, **extra):
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp,
        "ac": 12,
        "attack_bonus": bonus,
        "damage_dice": dice,
        "initiative_bonus": 1,
        "is_boss": boss,
        "is_player": False,
        "disposition": "active",
        "alive": True,
        **extra,
    }


def test_in_budget_roster_is_returned_unchanged_with_no_adaptation() -> None:
    roster = [foe("Goblin", hp=7), foe("Goblin 2", hp=6)]
    out, summary = apply_solo_budget(roster, 1)
    assert out == roster
    assert summary == {"level": 1, "adapted": False, "notes": []}
    assert adaptation_sentence(summary) == ""


def test_out_of_scale_numbers_are_clamped_into_the_level_one_budget() -> None:
    out, summary = apply_solo_budget([foe("Ogre", hp=120, bonus=10, dice="4d12+5", boss=True)], 1)
    (ogre,) = out
    assert ogre["hp_max"] == ogre["hp_current"] == 30
    assert ogre["attack_bonus"] == 5
    assert ogre["damage_dice"] == "1d4+2"
    assert summary["adapted"] and len(summary["notes"]) == 3


def test_low_hp_is_raised_to_the_band_floor() -> None:
    out, _ = apply_solo_budget([foe("Rat", hp=1)], 1)
    assert out[0]["hp_max"] == out[0]["hp_current"] == 5


def test_extra_foes_are_left_out_and_bosses_are_kept_first() -> None:
    roster = [foe("Goblin"), foe("Goblin 2"), foe("Goblin 3"), foe("Captain", hp=20, boss=True)]
    out, summary = apply_solo_budget(roster, 1)
    assert [e["name"] for e in out] == ["Goblin", "Captain"]
    assert any("Goblin 2 left out" in n for n in summary["notes"])
    assert any("Goblin 3 left out" in n for n in summary["notes"])


def test_total_hp_over_budget_leaves_out_the_later_foe_not_the_boss() -> None:
    # Two bosses at level 1 would total 60 HP against a budget of 41.
    out, summary = apply_solo_budget(
        [foe("First Boss", hp=30, boss=True), foe("Second Boss", hp=30, boss=True)], 1
    )
    assert [e["name"] for e in out] == ["First Boss"]
    assert any("total enemy HP limit of 41" in n for n in summary["notes"])


def test_a_single_foe_is_never_removed_even_when_it_must_be_clamped() -> None:
    out, _ = apply_solo_budget([foe("Dragon", hp=120, bonus=10, dice="4d12+5")], 1)
    assert len(out) == 1 and out[0]["hp_max"] == 11 and out[0]["attack_bonus"] == 5


def test_an_empty_roster_stays_empty() -> None:
    assert apply_solo_budget([], 1) == ([], {"level": 1, "adapted": False, "notes": []})


def test_budget_is_pure_and_deterministic() -> None:
    roster = [foe("Goblin", hp=200, dice="3d10+2"), foe("Goblin 2", hp=50)]
    snapshot = repr(roster)
    first = apply_solo_budget(roster, 3)
    assert repr(roster) == snapshot, "input must not be mutated"
    assert apply_solo_budget(roster, 3) == first


def test_exact_creature_identity_and_record_are_preserved() -> None:
    record = {
        "creature_id": "e" * 64,
        "creature_name": "Goblin",
        "text": "Goblin\nArmor Class 15\nHit Points 7\n" + "Synthetic trait. " * 40,
        "source": "Synthetic Manual",
        "page": 167,
        "source_sha256": "a" * 64,
    }
    provenance = _creature_source_reference(record)
    exact = foe(
        "Goblin", hp=120, bonus=10, creature_id=record["creature_id"], source_reference=provenance
    )
    out, summary = apply_solo_budget([exact], 1)
    kept = out[0]
    assert kept["name"] == "Goblin"
    assert kept["creature_id"] == record["creature_id"]
    assert kept["source_reference"] == provenance
    assert "text" not in kept["source_reference"]
    # EDG-004: an exact record is never rescaled, so no numeric stat changed. The out-of-band
    # values are declared as unscaled instead.
    assert kept == exact
    assert summary["adapted"] is False
    assert any(n.startswith("unscaled exact creature: Goblin") for n in summary["notes"])


# --- damage fitting ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "cap", "expected"),
    [
        ("1d6+1", 5, "1d6+1"),
        ("2d6", 5, "1d6"),
        ("1d12+5", 5, "1d4+2"),
        ("1d8", 5, "1d8"),
        ("1d20", 5, "1d8"),
        ("not dice", 5, "not dice"),
    ],
)
def test_damage_is_fitted_deterministically(expr, cap, expected) -> None:
    assert clamp_damage(expr, cap) == expected


# --- engine roster build (LLM-proposed numbers) ----------------------------------------------


def _builder(fake, *enemies):
    fake.responses[EncounterSetup] = EncounterSetup(enemies=list(enemies))


def _built_state(level: int = 1) -> dict:
    return {
        "player_input": "Attack Goblin",
        "character": {"name": "Eda", "level": level, "class": "Fighter"},
    }


@pytest.mark.asyncio
async def test_llm_numbers_are_ignored_and_only_the_foe_count_is_limited(
    fake_llm_schema, monkeypatch
):
    # EDG-004: the builder's numbers never apply to an exact record; the record's do (7 HP here).
    install_record(monkeypatch)
    _builder(
        fake_llm_schema,
        EnemyStat(name="Goblin", hp_max=120, attack_bonus=10, damage_dice="4d12+5", ac=20),
        *[EnemyStat(name="Goblin", hp_max=100) for _ in range(4)],
    )
    budget_summary: dict = {}
    roster = await _build_enemy_roster(_built_state(1), ["Goblin"], budget_summary=budget_summary)
    assert len(roster) == 2
    assert all(e["hp_max"] == 7 and e["ac"] == 15 for e in roster)
    assert [e["name"] for e in roster] == ["Goblin", "Goblin 2"]
    assert budget_summary["adapted"] is True
    assert any("left out" in n for n in budget_summary["notes"])


@pytest.mark.parametrize("level", [1, 5, 9, 20])
@pytest.mark.asyncio
async def test_roster_respects_the_budget_of_each_hero_level(fake_llm_schema, monkeypatch, level):
    install_record(monkeypatch)
    _builder(
        fake_llm_schema, *[EnemyStat(name="Goblin", hp_max=120, attack_bonus=10) for _ in range(6)]
    )
    budget = solo_budget(level)
    roster = await _build_enemy_roster(_built_state(level), ["Goblin"])
    assert 1 <= len(roster) <= budget.max_foes
    # Exact records keep their record numbers at every level; only the count is limited.
    assert all(e["hp_max"] == 7 for e in roster)


@pytest.mark.asyncio
async def test_exact_record_is_preserved_byte_for_byte_when_in_budget(fake_llm_schema, monkeypatch):
    record = install_record(monkeypatch)
    _builder(fake_llm_schema, EnemyStat(name="Goblin", hp_max=8, ac=13, attack_bonus=3))
    roster = await _build_enemy_roster(_built_state(1), ["Goblin"])
    entry = roster[0]
    assert entry["creature_id"] == record["creature_id"]
    assert entry["source_reference"] == _creature_source_reference(record)
    assert entry["name"] == "Goblin" and entry["hp_max"] == 7 and entry["ac"] == 15


@pytest.mark.asyncio
async def test_builder_failure_uses_the_exact_record_not_defaults(fake_llm_schema, monkeypatch):
    install_record(monkeypatch)
    # No EncounterSetup response: the builder call fails. Each resolved foe still uses its record.
    budget_summary: dict = {}
    roster = await _build_enemy_roster(_built_state(1), ["Goblin"], budget_summary=budget_summary)
    assert roster[0]["stat_adaptation"] == "exact_record"
    assert roster[0]["hp_max"] == 7 and roster[0]["hp_current"] == 7
    assert budget_summary == {"level": 1, "adapted": False, "notes": []}


@pytest.mark.asyncio
async def test_two_similar_names_stay_distinct_after_fitting(fake_llm_schema, monkeypatch):
    install_record(monkeypatch)
    _builder(fake_llm_schema, *[EnemyStat(name="Goblin", hp_max=8) for _ in range(3)])
    roster = await _build_enemy_roster(_built_state(1), ["Goblin"])
    names = [e["name"] for e in roster]
    assert names == ["Goblin", "Goblin 2"] and len(set(n.lower() for n in names)) == len(names)


@pytest.mark.asyncio
async def test_builder_prompt_states_the_enforced_budget(fake_llm_schema, monkeypatch):
    install_record(monkeypatch)
    _builder(fake_llm_schema, EnemyStat(name="Goblin", hp_max=8))
    await _build_enemy_roster(_built_state(1), ["Goblin"])
    prompt = next(m for schema, m in fake_llm_schema.calls if schema is EncounterSetup)
    assert "Solo budget for this level-1 hero (enforced by the engine)" in prompt[1].content
    assert "boss HP 18-30" in prompt[1].content and "other foes HP 5-11" in prompt[1].content
    assert "stay within the solo budget given in the context" in ENCOUNTER_BUILDER


# --- combat start, payload and legacy rows ---------------------------------------------------


@pytest.mark.asyncio
async def test_combat_start_tells_the_player_and_persists_the_adaptation(
    fake_llm_schema, fake_retrieval, monkeypatch
):
    install_record(monkeypatch)
    _builder(fake_llm_schema, *[EnemyStat(name="Goblin", hp_max=120) for _ in range(3)])
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="Goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = {
        **_state(None, "I attack the Goblin"),
        "parsed_input": ParsedInput(intent="combat", check="attack", target="Goblin"),
    }
    out = await combat_round_node(state)
    summary = out["combat_state"]["solo_budget"]
    assert summary["adapted"] is True and summary["level"] == 1
    assert (
        "Encounter adapted for a solo level 1 hero:"
        in out["adjudication_result"].mechanical_summary
    )
    public = combat_state_public(out["combat_state"])
    assert public["solo_budget"] == summary
    # The player combatant is the row that persists, so a reload can recover the adaptation.
    player = next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))
    assert player["solo_budget"] == summary


@pytest.mark.asyncio
async def test_legacy_combat_rows_are_not_rescaled(fake_llm_schema, fake_retrieval):
    legacy_enemy = foe("Ogre", hp=120)
    legacy_enemy.pop("creature_id", None)
    state = {
        **_state(
            [
                {
                    "name": "player",
                    "display_name": "Eda",
                    "is_player": True,
                    "hp_current": 12,
                    "hp_max": 12,
                    "ac": 18,
                    "alive": True,
                    "init": 15,
                    "init_bonus": 1,
                    "second_wind_used": False,
                },
                legacy_enemy,
            ],
            "I hit the ogre",
        ),
        "parsed_input": ParsedInput(intent="combat", check="attack", target="Ogre"),
    }
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    out = await combat_round_node(state)
    assert out["combat_state"].get("solo_budget") is None
    ogre = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Ogre")
    assert ogre["hp_max"] == 120
    assert not any(schema is EncounterSetup for schema, _ in fake_llm_schema.calls)


def test_contract_states_the_enforced_budget_not_a_prompt_hope() -> None:
    text = " ".join(SOLO_ADAPTATIONS)
    assert "engine-enforced budget" in text
    assert "not an engine-enforced" not in text
    assert "prompt guidance" not in text
