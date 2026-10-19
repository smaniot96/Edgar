"""EDG-145: class features beyond Second Wind (Action Surge, Sneak Attack, Rage).

Pure rules, the combat round (engine dice, uses, recharge, advantage interaction), the declared-action
boundary and narration guard, and the sheet PATCH that must keep engine-owned uses. The DB-backed
persistence test lives in tests/test_class_feature_persistence.py.
"""

import random
from types import SimpleNamespace

import pytest

from agent.class_features import (
    ACTION_SURGE,
    FEATURE_EFFECTS,
    RAGE,
    SNEAK_ATTACK,
    class_feature_after_rest,
    class_feature_status,
    max_uses,
    rage_damage_bonus,
    rage_resists,
    requested_features,
    resource_uses,
    ruling,
    spend_use,
    uses_left,
)
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterAttack, MonsterStats, combat_fields
from agent.resolution import (
    CLASS_FEATURES_KEY,
    ENGINE_STATS_KEYS,
    CheckResult,
    enforce_side_effects,
    resolve_attack,
)
from agent.supported_rules import (
    EngineApplied,
    apply_narration_boundary,
    classify_unsupported_actions,
)
from tests.agent.test_spells import ScriptedDice
from tools.dice import use_rng

ABILITIES = {"STR": 16, "DEX": 14, "CON": 14, "INT": 10, "WIS": 10, "CHA": 8}


def _sheet(
    cls: str, level: int = 1, *, weapons=("longsword",), features=None, hp: int = 12
) -> dict:
    stats = dict(ABILITIES)
    if features is not None:
        stats[CLASS_FEATURES_KEY] = features
    return {
        "id": 3,
        "name": "Brann",
        "class": cls,
        "level": level,
        "hp_current": hp,
        "hp_max": 12,
        "stats": stats,
        "inventory": {"weapons": list(weapons), "armor": ""},
    }


FIGHTER_2 = _sheet("Fighter", 2)
BARBARIAN_1 = _sheet("Barbarian", 1, weapons=("greataxe",))
ROGUE_1 = _sheet("Rogue", 1, weapons=("dagger",))
WIZARD_1 = _sheet("Wizard", 1, weapons=("dagger",))


def _enemy(name: str, *, hp: int, ac: int, init: int, attack_bonus: int = 3, dmg: str = "1d6+1"):
    return {
        "name": name,
        "hp_max": hp,
        "hp_current": hp,
        "ac": ac,
        "attack_bonus": attack_bonus,
        "damage_dice": dmg,
        "initiative_bonus": 1,
        "init": init,
        "init_bonus": 1,
        "is_boss": False,
        "is_player": False,
        "alive": True,
    }


def _hero(sheet: dict, *, ac: int = 30, init: int = 20) -> dict:
    return {
        "name": "player",
        "display_name": sheet["name"],
        "is_player": True,
        "hp_current": sheet["hp_current"],
        "hp_max": sheet["hp_max"],
        "ac": ac,
        "alive": True,
        "init": init,
        "init_bonus": 1,
    }


def _state(
    sheet: dict, foes: list[dict], message: str, *, parsed: ParsedInput | None = None
) -> dict:
    return {
        "player_input": message,
        "session_id": 1,
        "campaign_id": 1,
        "messages": [],
        "character": sheet,
        "world_flags": {},
        "npcs": [],
        "adventure_collections": [],
        "parsed_input": parsed or ParsedInput(intent="combat", check="attack", target="ogre"),
        "combat_state": {
            "id": 7,
            "round": 1,
            "ended": False,
            "combatants": [_hero(sheet), *foes],
        },
    }


def _purpose(dice_log: list[dict], purpose: str) -> list[dict]:
    return [d for d in dice_log if d.get("purpose") == purpose]


def _attack_checks(dice_log: list[dict]) -> list[dict]:
    return _purpose(dice_log, "attack check")


# --------------------------------------------------------------------------------------------
# Pure rules: tables, legacy sheets, recharge
# --------------------------------------------------------------------------------------------


def test_class_feature_tables_follow_the_levels() -> None:
    assert max_uses(_sheet("Fighter", 1), ACTION_SURGE) == 0  # Action Surge starts at level 2
    assert max_uses(FIGHTER_2, ACTION_SURGE) == 1
    assert max_uses(_sheet("Fighter", 17), ACTION_SURGE) == 2
    assert [max_uses(_sheet("Barbarian", lvl), RAGE) for lvl in (1, 3, 6, 12, 17)] == [
        2,
        3,
        4,
        5,
        6,
    ]
    assert max_uses(ROGUE_1, RAGE) == 0 and max_uses(ROGUE_1, ACTION_SURGE) == 0
    assert max_uses(WIZARD_1, RAGE) == 0 and max_uses(WIZARD_1, SNEAK_ATTACK) == 0
    assert class_feature_status(WIZARD_1) is None
    assert class_feature_status(FIGHTER_2) == {ACTION_SURGE: {"uses_remaining": 1, "uses_max": 1}}


def test_legacy_and_malformed_sheets_read_as_full_uses() -> None:
    assert uses_left(BARBARIAN_1, RAGE) == 2  # no key written: the full uses are available
    assert resource_uses(BARBARIAN_1) == {RAGE: 2}
    spent = _sheet("Barbarian", 1, features={RAGE: {"uses_remaining": 0}})
    assert uses_left(spent, RAGE) == 0
    assert uses_left(_sheet("Barbarian", 1, features={RAGE: {"uses_remaining": 9}}), RAGE) == 2
    assert uses_left(_sheet("Barbarian", 1, features={RAGE: "spent"}), RAGE) == 2
    assert uses_left(_sheet("Barbarian", 1, features=["rage"]), RAGE) == 2
    # A feature the class lacks is never read from the sheet.
    assert resource_uses(_sheet("Wizard", 1, features={RAGE: {"uses_remaining": 2}})) == {}


def test_spend_and_recharge_by_rest_kind() -> None:
    assert spend_use(FIGHTER_2, ACTION_SURGE) == 0
    assert spend_use(FIGHTER_2, ACTION_SURGE, left=0) is None  # nothing spent when none is left
    assert spend_use(WIZARD_1, RAGE) is None

    spent_surge = _sheet("Fighter", 2, features={ACTION_SURGE: {"uses_remaining": 0}})
    spent_rage = _sheet("Barbarian", 1, features={RAGE: {"uses_remaining": 0}})
    # A short rest restores Action Surge but not Rage; a long rest restores both.
    assert class_feature_after_rest(spent_surge, "short") == {ACTION_SURGE: 1}
    assert class_feature_after_rest(spent_rage, "short") is None
    assert class_feature_after_rest(spent_rage, "long") == {RAGE: 2}
    # A full sheet is not rewritten, and a rest of another kind changes nothing.
    assert class_feature_after_rest(FIGHTER_2, "long") is None
    assert class_feature_after_rest(spent_surge, None) is None


def _hit(sheet: dict, *, hint: str = "") -> CheckResult:
    """A successful attack roll against AC 1 (any roll but a natural 1 hits)."""
    for seed in range(20):
        attack = resolve_attack(sheet, 1, hint=hint, rng=random.Random(seed))
        if attack.success:
            return attack
    raise AssertionError("no hit found")


def test_rage_damage_bonus_by_level_and_attack_type() -> None:
    hit = _hit(BARBARIAN_1)
    assert rage_damage_bonus(BARBARIAN_1, hit, raging=True) == 2
    assert rage_damage_bonus(BARBARIAN_1, hit, raging=False) == 0
    assert rage_damage_bonus(_sheet("Barbarian", 9, weapons=("greataxe",)), hit, raging=True) == 3
    assert rage_damage_bonus(_sheet("Barbarian", 16, weapons=("greataxe",)), hit, raging=True) == 4
    # A ranged weapon, and a finesse weapon the engine attacks with DEX, get no Strength bonus.
    bow_sheet = _sheet("Barbarian", 1, weapons=("longbow",))
    assert rage_damage_bonus(bow_sheet, _hit(bow_sheet, hint="shoot"), raging=True) == 0
    rapier_sheet = _sheet("Barbarian", 1, weapons=("rapier",))
    rapier_sheet["stats"]["DEX"] = 18  # DEX above STR: the finesse attack uses DEX
    assert rage_damage_bonus(rapier_sheet, _hit(rapier_sheet), raging=True) == 0


@pytest.mark.parametrize(
    ("damage_type", "raging", "resisted"),
    [
        # PHB Barbarian, Rage, PDF p.49: resistance to bludgeoning, piercing and slashing damage.
        ("bludgeoning", True, True),
        ("piercing", True, True),
        ("slashing", True, True),
        ("fire", True, False),
        (None, True, False),  # an attack with no stated damage type is not halved
        ("slashing", False, False),
    ],
)
def test_rage_resists_only_physical_damage_while_raging(damage_type, raging, resisted) -> None:
    assert rage_resists(BARBARIAN_1, damage_type, raging=raging) is resisted
    assert not rage_resists(FIGHTER_2, "slashing", raging=True)  # only a barbarian rages


def test_rage_gives_nothing_in_heavy_armor() -> None:
    # PHB Barbarian, Rage, PDF p.49: the benefits apply "if you aren't wearing heavy armor".
    hit = _hit(BARBARIAN_1)
    armors = {"": True, "chain mail": False, "plate": False, "half plate": True}
    for armor, benefits in armors.items():
        sheet = {**BARBARIAN_1, "inventory": {"weapons": ["greataxe"], "armor": armor}}
        assert rage_resists(sheet, "slashing", raging=True) is benefits
        assert rage_damage_bonus(sheet, hit, raging=True) == (2 if benefits else 0)


def test_rage_as_an_idiom_is_not_a_declaration() -> None:
    assert requested_features("I rage against the machine") == ()
    assert requested_features("I rage and swing the greataxe") == ("rage",)


def test_requested_features_are_declared_words_not_prose() -> None:
    assert requested_features("I use Action Surge and attack") == (ACTION_SURGE,)
    assert requested_features("I sneak attack the guard") == (SNEAK_ATTACK,)
    assert requested_features("I fly into a rage and swing") == (RAGE,)
    assert requested_features("I enter a rage!") == (RAGE,)
    assert requested_features(None, "Action Surge") == (ACTION_SURGE,)
    # Prose about rage or surges is not a declaration.
    for prose in (
        "The orc's rage boils over as it lunges.",
        "A surge of water rolls down the stairs.",
        "The storm's rage tears at the roof.",
        "I'm into rage metal, so I hum along.",
        "I take advantage of the distraction and slip past.",
    ):
        assert requested_features(prose) == (), prose


def test_rulings_name_the_reason_and_confirm_no_effect() -> None:
    assert "only a fighter" in ruling(ACTION_SURGE, WIZARD_1).lower()
    assert "level 2" in ruling(ACTION_SURGE, _sheet("Fighter", 1)).lower()
    spent = _sheet("Fighter", 2, features={ACTION_SURGE: {"uses_remaining": 0}})
    assert "no uses left" in ruling(ACTION_SURGE, spent)
    assert "no extra action" in ruling(ACTION_SURGE, spent).lower()
    assert "only a rogue" in ruling(SNEAK_ATTACK, FIGHTER_2).lower()
    assert "advantage" in ruling(SNEAK_ATTACK, ROGUE_1)
    assert "only a barbarian" in ruling(RAGE, FIGHTER_2).lower()
    spent_rage = _sheet("Barbarian", 1, features={RAGE: {"uses_remaining": 0}})
    assert "until a long rest" in ruling(RAGE, spent_rage)
    assert FEATURE_EFFECTS == {ACTION_SURGE, SNEAK_ATTACK, RAGE}


# --------------------------------------------------------------------------------------------
# Combat round: engine dice, uses and recharge
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_surge_adds_one_attack_and_recharges(fake_llm_schema, fake_retrieval) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    # The ogre cannot hit AC 30 (only a natural 20 can), so the hero's HP is not in question.
    safe_ogre = _enemy("Ogre", hp=200, ac=1, init=1, attack_bonus=-20)

    state = _state(FIGHTER_2, [dict(safe_ogre)], "I use Action Surge and attack the ogre")
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert len(_attack_checks(adj.dice_log)) == 2  # the round's attack and the surged one
    assert "Narrative ruling" not in adj.mechanical_summary  # executed, so no ruling
    assert adj.character_update.class_feature_uses == {ACTION_SURGE: 0}

    # Exhausted on the sheet: refused without an extra roll, and nothing is written.
    spent = _sheet("Fighter", 2, features={ACTION_SURGE: {"uses_remaining": 0}})
    state = _state(spent, [dict(safe_ogre)], "I use Action Surge and attack the ogre")
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert len(_attack_checks(adj.dice_log)) == 1
    assert "Action Surge has no uses left" in adj.mechanical_summary
    assert adj.character_update.class_feature_uses is None

    # Level 1 fighters have no Action Surge at all, and the wording gets a ruling, not a roll.
    state = _state(
        _sheet("Fighter", 1), [dict(safe_ogre)], "I use Action Surge and attack the ogre"
    )
    with use_rng(random.Random(5)):
        out = await combat_round_node(state)
    assert len(_attack_checks(out["adjudication_result"].dice_log)) == 1
    assert "needs fighter level 2" in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_class_gating_refuses_a_feature_the_class_lacks(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(
        WIZARD_1,
        [_enemy("Ogre", hp=200, ac=1, init=1, attack_bonus=-20)],
        "I go into a rage and attack the ogre",
    )
    with use_rng(random.Random(2)):
        out = await combat_round_node(state)
    assert "only a barbarian has rage" in out["adjudication_result"].mechanical_summary.lower()
    assert not out["combat_state"].get("raging")
    assert out["adjudication_result"].character_update is None or (
        out["adjudication_result"].character_update.class_feature_uses is None
    )


@pytest.mark.asyncio
async def test_sneak_attack_needs_engine_advantage_and_finesse_or_ranged(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    # A restrained foe gives the engine's advantage on attacks against it (attack_roll_mode).
    restrained = {
        **_enemy("Ogre", hp=200, ac=1, init=1, attack_bonus=-20),
        "effects": [{"kind": "restrained", "source": "hands", "applied_round": 1}],
    }
    plain = _enemy("Ogre", hp=200, ac=1, init=1, attack_bonus=-20)

    sneak_hits = 0
    for seed in range(20):
        state = _state(ROGUE_1, [dict(restrained)], "I stab the ogre with my dagger")
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        adj = out["adjudication_result"]
        sneak = _purpose(adj.dice_log, "sneak attack")
        hit = _purpose(adj.dice_log, "player damage")
        assert len(sneak) <= 1  # once per turn
        if hit:
            # One d6 for a level-1 rogue, two on a critical hit (dice double).
            assert len(sneak) == 1 and sneak[0]["expression"] in ("1d6", "2d6")
            assert "Sneak Attack adds" in adj.mechanical_summary
            sneak_hits += 1
        else:
            assert not sneak
    assert sneak_hits > 0

    # No advantage (a plain foe), a longsword, or a fighter: no Sneak Attack, ever.
    for seed in range(10):
        state = _state(ROGUE_1, [dict(plain)], "I stab the ogre with my dagger")
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        assert not _purpose(out["adjudication_result"].dice_log, "sneak attack")
        blade = _state(
            _sheet("Rogue", 1, weapons=("longsword",)), [dict(restrained)], "I hit the ogre"
        )
        with use_rng(random.Random(seed)):
            out = await combat_round_node(blade)
        assert not _purpose(out["adjudication_result"].dice_log, "sneak attack")
    assert sneak_hits > 0


def test_sneak_attack_dice_scale_and_double_on_a_critical() -> None:
    from agent.class_features import sneak_attack_roll
    from agent.resolution import CheckResult, resolve_attack

    rogue_5 = _sheet("Rogue", 5, weapons=("rapier",))
    attack = resolve_attack(rogue_5, 1, rng=random.Random(1))
    assert attack.weapon is not None and attack.weapon.finesse
    attack = CheckResult(**{**attack.__dict__, "success": True, "critical": False})
    dice = sneak_attack_roll(rogue_5, attack, mode="advantage", used=False, rng=random.Random(1))
    assert dice is not None and dice.expression == "3d6"  # (5 + 1) // 2 dice
    crit = CheckResult(**{**attack.__dict__, "critical": True})
    assert sneak_attack_roll(rogue_5, crit, mode="advantage", used=False).expression == "6d6"
    assert sneak_attack_roll(rogue_5, attack, mode="advantage", used=True) is None
    assert sneak_attack_roll(rogue_5, attack, mode="normal", used=False) is None


@pytest.mark.asyncio
async def test_rage_adds_damage_uses_recharge_and_ends_with_the_encounter(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    # AC 1 is hit by any roll but a natural 1. The ogre cannot hit the hero (AC 30).
    ogre = _enemy("Ogre", hp=400, ac=1, init=1, attack_bonus=-20)

    rage_hits = []
    for seed in range(20):
        state = _state(BARBARIAN_1, [dict(ogre)], "I rage and swing the greataxe at the ogre")
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        adj = out["adjudication_result"]
        assert out["combat_state"]["raging"] is True
        assert adj.character_update.class_feature_uses == {RAGE: 1}
        assert "flies into a rage" in adj.mechanical_summary
        hits = _purpose(adj.dice_log, "player damage")
        if hits:
            # Greataxe 1d12, STR 16 (+3), rage +2 on the same engine roll.
            assert hits[0]["expression"] in ("1d12+5", "2d12+5")
            rage_hits.append(hits[0])
    assert rage_hits

    # Already raging: the next declaration spends nothing and is not applied again.
    raging_state = _state(BARBARIAN_1, [dict(ogre)], "I rage again and attack the ogre")
    raging_state["combat_state"]["combatants"][0]["raging"] = True
    with use_rng(random.Random(1)):
        out = await combat_round_node(raging_state)
    assert out["adjudication_result"].character_update.class_feature_uses is None

    # Exhausted: refused, and no raging flag is set.
    spent = _sheet("Barbarian", 1, weapons=("greataxe",), features={RAGE: {"uses_remaining": 0}})
    with use_rng(random.Random(1)):
        out = await combat_round_node(_state(spent, [dict(ogre)], "I rage and attack the ogre"))
    assert "until a long rest" in out["adjudication_result"].mechanical_summary
    assert not out["combat_state"].get("raging")

    # The encounter ends with the foe's defeat: rage does not carry into a later fight.
    weak = _enemy("Rat", hp=1, ac=1, init=1, attack_bonus=-20)
    rat_turn = ParsedInput(intent="combat", check="attack", target="rat")
    won = None
    for seed in range(20):
        with use_rng(random.Random(seed)):
            won = await combat_round_node(
                _state(BARBARIAN_1, [dict(weak)], "I rage and attack the rat", parsed=rat_turn)
            )
        if won["combat_state"]["ended"]:
            break
    assert won["combat_state"]["ended"] is True
    assert won["combat_state"]["raging"] is False


def _brute(damage_type: str) -> dict:
    """An exact record whose only attack (Club, +3, 1d6+1) deals `damage_type`."""
    stats = MonsterStats(
        name="Brute",
        hp_max=400,
        ac=1,
        abilities={"DEX": 1},
        attacks=(MonsterAttack("Club", 3, "1d6+1", damage_type),),
    )
    return {
        "name": "Brute",
        "is_player": False,
        "disposition": "active",
        "alive": True,
        "init": 1,
        "init_bonus": 1,
        "monster_stats": stats.to_dict(),
        **combat_fields(stats),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("damage_type", "armor", "taken"),
    [("slashing", "", 3), ("piercing", "", 3), ("bludgeoning", "", 3), ("fire", "", 6),
     ("slashing", "chain mail", 6)],
)  # fmt: skip
async def test_rage_halves_physical_damage_taken_outside_heavy_armor(
    fake_llm_schema, fake_retrieval, damage_type, armor, taken
) -> None:
    """PHB Barbarian, Rage, PDF p.49: resistance to bludgeoning, piercing and slashing damage."""
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    sheet = {**BARBARIAN_1, "inventory": {"weapons": ["greataxe"], "armor": armor}}
    state = _state(sheet, [_brute(damage_type)], "I attack the brute")
    state["parsed_input"] = ParsedInput(intent="combat", check="attack", target="brute")
    hero = state["combat_state"]["combatants"][0]
    hero.update(ac=10, raging=True, rage_round=1)
    # The hero hits (d20 10 against AC 1) for 1d12 = 5; the brute hits (15 + 3 against AC 10) for
    # 1d6 + 1 = 6 before resistance.
    with use_rng(ScriptedDice([10, 5, 15, 5])):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert adj.character_update.hp_delta == -taken
    assert ("halved by Rage resistance" in adj.mechanical_summary) is (taken == 3)


@pytest.mark.asyncio
async def test_rage_ends_after_one_minute(fake_llm_schema, fake_retrieval) -> None:
    """PHB Barbarian, Rage, PDF p.49: "Your rage lasts for 1 minute", that is 10 rounds."""
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    ogre = _enemy("Ogre", hp=400, ac=1, init=1, attack_bonus=-20)
    expected = {10: ("1d12+5", True), 11: ("1d12+3", False)}  # begun in round 1: ends in round 11
    for round_num, (expression, raging) in expected.items():
        state = _state(BARBARIAN_1, [dict(ogre)], "I attack the ogre")
        state["combat_state"]["round"] = round_num
        state["combat_state"]["combatants"][0].update(raging=True, rage_round=1)
        with use_rng(ScriptedDice([10, 5, 10])):
            out = await combat_round_node(state)
        adj = out["adjudication_result"]
        assert _purpose(adj.dice_log, "player damage")[0]["expression"] == expression
        assert out["combat_state"]["raging"] is raging
        assert ("rage ends after 1 minute" in adj.mechanical_summary) is not raging


@pytest.mark.asyncio
async def test_declaring_rage_in_heavy_armor_spends_the_use_for_no_benefit(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    sheet = {**BARBARIAN_1, "inventory": {"weapons": ["greataxe"], "armor": "chain mail"}}
    state = _state(
        sheet,
        [_enemy("Ogre", hp=400, ac=1, init=1, attack_bonus=-20)],
        "I rage and swing the greataxe at the ogre",
    )
    with use_rng(ScriptedDice([10, 5, 10])):
        out = await combat_round_node(state)
    adj = out["adjudication_result"]
    assert adj.character_update.class_feature_uses == {RAGE: 1}
    assert "Heavy armor blocks its benefits" in adj.mechanical_summary
    assert _purpose(adj.dice_log, "player damage")[0]["expression"] == "1d12+3"  # no rage bonus


@pytest.mark.asyncio
async def test_rage_is_a_bonus_action_beside_the_attack(fake_llm_schema, fake_retrieval) -> None:
    """EDG-044 x EDG-145: Rage takes the bonus-action slot; the turn's attack still happens."""
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    ogre = _enemy("Ogre", hp=400, ac=1, init=1, attack_bonus=-20)
    state = _state(BARBARIAN_1, [dict(ogre)], "I rage and swing the greataxe at the ogre")
    with use_rng(random.Random(3)):
        out = await combat_round_node(state)
    economy = out["combat_state"]["economy"]
    assert economy["bonus_action"] == "Rage" and economy["action"] == "attack"
    assert _attack_checks(out["adjudication_result"].dice_log)  # the attack was still rolled


@pytest.mark.asyncio
async def test_rage_does_not_apply_to_a_ranged_attack(fake_llm_schema, fake_retrieval) -> None:
    from agent.graph_combat import combat_round_node

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    bow = _sheet("Barbarian", 1, weapons=("longbow",))
    hits = 0
    for seed in range(10):
        state = _state(
            bow,
            [_enemy("Ogre", hp=400, ac=1, init=1, attack_bonus=-20)],
            "I rage and shoot the ogre with my longbow",
        )
        with use_rng(random.Random(seed)):
            out = await combat_round_node(state)
        for hit in _purpose(out["adjudication_result"].dice_log, "player damage"):
            hits += 1
            # Longbow 1d8 with DEX +2 (14): no Strength bonus and no rage bonus.
            assert hit["expression"] in ("1d8+2", "2d8+2")
    assert hits > 0


# --------------------------------------------------------------------------------------------
# Out of combat: only rest recovery; declared features are ruled, not applied
# --------------------------------------------------------------------------------------------


def test_out_of_combat_declarations_are_ruled_and_rest_recovers_uses() -> None:
    rage_out = AdjudicationResult(success=True)
    enforce_side_effects(
        rage_out,
        character=BARBARIAN_1,
        check_result=None,
        player_input="I rage at the innkeeper",
        rng=random.Random(2),
    )
    assert not rage_out.dice_log and rage_out.character_update is None

    spent_surge = _sheet("Fighter", 2, features={ACTION_SURGE: {"uses_remaining": 0}})
    spent_rage = _sheet("Barbarian", 1, features={RAGE: {"uses_remaining": 0}})
    short = AdjudicationResult(success=True)
    enforce_side_effects(
        short,
        character=spent_rage,
        check_result=None,
        player_input="I take a short rest",
        rng=random.Random(2),
    )
    assert short.character_update.class_feature_uses is None  # short rests do not restore Rage

    long_rest = AdjudicationResult(success=True)
    enforce_side_effects(
        long_rest,
        character=spent_rage,
        check_result=None,
        player_input="I take a long rest",
        rng=random.Random(2),
    )
    assert long_rest.character_update.class_feature_uses == {RAGE: 2}

    surge_rest = AdjudicationResult(success=True)
    enforce_side_effects(
        surge_rest,
        character=spent_surge,
        check_result=None,
        player_input="I take a short rest",
        rng=random.Random(2),
    )
    assert surge_rest.character_update.class_feature_uses == {ACTION_SURGE: 1}


def test_model_proposed_feature_uses_are_never_persisted() -> None:
    from agent.models.adjudication import CharacterUpdate

    adj = AdjudicationResult(
        success=True,
        character_update=CharacterUpdate(class_feature_uses={RAGE: 6, ACTION_SURGE: 2}),
    )
    enforce_side_effects(
        adj,
        character=BARBARIAN_1,
        check_result=None,
        player_input="I look around the room",
        rng=random.Random(1),
    )
    assert adj.character_update is None or adj.character_update.class_feature_uses is None


# --------------------------------------------------------------------------------------------
# Declared-action boundary and narration guard
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kinds", "character"),
    [
        ("I use Action Surge and attack twice", ["feature"], None),
        # Without the engine having executed it, a declared feature always gets its ruling.
        ("I use Action Surge and attack twice", ["feature"], FIGHTER_2),
        (
            "I use Action Surge and attack twice",
            ["feature"],
            _sheet("Fighter", 2, features={ACTION_SURGE: {"uses_remaining": 0}}),
        ),
        ("I stab the ogre and sneak attack it", ["feature"], FIGHTER_2),
        ("I rage at the goblin", ["feature"], BARBARIAN_1),
        ("I rage at the goblin", ["feature"], WIZARD_1),
        ("I smite the ogre with divine smite", ["feature"], FIGHTER_2),
        ("I take a short rest", [], FIGHTER_2),
        ("The orc's rage boils over as the blade falls", [], BARBARIAN_1),
    ],
)
def test_implemented_features_are_only_ruled_when_not_executed(text, kinds, character) -> None:
    assert [a.kind for a in classify_unsupported_actions(text, None, character)] == kinds


def test_executed_feature_is_not_a_declared_action_and_keeps_its_narration() -> None:
    surge_text = "I use Action Surge and attack the ogre"
    assert (
        classify_unsupported_actions(
            surge_text, None, FIGHTER_2, applied=EngineApplied(effects=("action_surge",))
        )
        == ()
    )
    narration = (
        "You use Action Surge and drive a second strike home. "
        "The ogre staggers. Its rage is spent, and the cave falls quiet."
    )
    # Without a declared unsupported action nothing is stripped; with one, the executed feature stays.
    declared = classify_unsupported_actions(
        "I use Action Surge and divine smite the ogre",
        None,
        FIGHTER_2,
        applied=EngineApplied(effects=("action_surge",)),
    )
    assert [a.kind for a in declared] == ["feature"]
    kept = apply_narration_boundary(
        narration, declared, applied=EngineApplied(effects=("action_surge",))
    )
    assert "You use Action Surge and drive a second strike home." in kept
    assert "The ogre staggers." in kept
    assert "Its rage is spent" in kept  # bare "rage" is prose, not a claim
    # The same declared turn without the engine effect: the Action Surge sentence is a claim.
    unexecuted = apply_narration_boundary(narration, declared)
    assert "Action Surge and drive" not in unexecuted
    assert "The ogre staggers." in unexecuted


def test_narration_about_rage_or_surge_in_prose_is_not_stripped() -> None:
    declared = classify_unsupported_actions("I cast fireball at the goblins", None, FIGHTER_2)
    prose = (
        "A surge of cold air rolls over the ruins. The orc's rage boils over, and the bridge sways. "
        "The wolves surge forward in a wave of fur."
    )
    assert apply_narration_boundary(prose, declared).startswith(prose)


def test_maneuver_claims_stay_stripped_when_only_a_feature_was_executed() -> None:
    declared = classify_unsupported_actions("I shove the ogre and pin it", None, ROGUE_1)
    narration = "The ogre is pinned to the wall. Your sneak attack lands cleanly."
    kept = apply_narration_boundary(
        narration, declared, applied=EngineApplied(effects=("sneak_attack",))
    )
    assert "pinned to the wall" not in kept  # the engine did not apply the maneuver
    assert "Your sneak attack lands cleanly." in kept  # the engine did apply Sneak Attack


# --------------------------------------------------------------------------------------------
# Sheet PATCH keeps engine-owned uses
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_patch_keeps_class_feature_uses(monkeypatch) -> None:
    from apps.api.routers import campaign_characters
    from apps.api.routers.campaign_characters import patch_campaign_character
    from apps.api.schemas import character as campaign_schema

    assert CLASS_FEATURES_KEY in ENGINE_STATS_KEYS

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _Db:
        async def execute(self, _query):
            return _Result(
                SimpleNamespace(
                    id=3, owner_user_id=1, name="Brann", character_class="Barbarian", level=1
                )
            )

        async def commit(self):
            return None

        async def refresh(self, _obj):
            return None

    assignment = SimpleNamespace(
        id=9,
        level=1,  # EDG-045: the assignment owns the level the engine reads
        milestones_pending=0,
        hp_current=12,
        hp_max=12,
        stats={
            "conditions": [],
            CLASS_FEATURES_KEY: {RAGE: {"uses_remaining": 0}},
            "second_wind": {"uses_remaining": 0},
        },
    )

    async def load(_db, _character_id, _campaign_id):
        return assignment

    monkeypatch.setattr(campaign_characters, "load_active_assignment", load)
    body = campaign_schema.CampaignCharacterPatch(stats={"STR": 18, CLASS_FEATURES_KEY: {}})
    await patch_campaign_character(1, 3, body, auth=None, db=_Db(), owner_id=1)
    assert assignment.stats["STR"] == 18
    assert assignment.stats[CLASS_FEATURES_KEY] == {RAGE: {"uses_remaining": 0}}
    assert assignment.stats["second_wind"] == {"uses_remaining": 0}


def test_scope_payload_lists_the_implemented_features_and_narrows_the_rest() -> None:
    from agent.supported_rules import contract_payload

    payload = contract_payload()
    executed = {m["id"] for m in payload["engine_executed"]}
    assert {
        "class_feature_action_surge",
        "class_feature_sneak_attack",
        "class_feature_rage",
    } <= executed
    not_supported = {m["id"]: m for m in payload["not_supported"]}
    boundary = not_supported["class_features"]["boundary"]
    assert "Divine Smite" in boundary and "Lay on Hands" in boundary
    assert "Action Surge" not in boundary and "Sneak Attack" not in boundary
