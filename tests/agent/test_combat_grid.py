"""Combat grid: the battlefield, the hero's route, foes' movement, reach and range (agent.battlefield,
agent.combat_grid).

The engine owns every square. These tests assert what the engine recorded (squares, refusals, which
dice were rolled and in which mode), never a particular roll.
"""

import json
import random
from copy import deepcopy

import pytest

from agent.battlefield import (
    DIFFICULT,
    WALL,
    distance_ft,
    label,
    line_of_sight,
    open_battlefield,
    parse_label,
    pos_of,
    reachable,
    route_to,
    square_move,
    sync_bands,
    theme_for,
    walking_speed,
)
from agent.combat_grid import preview_route
from agent.graph_combat import combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import MonsterStats, parse_monster_stats
from agent.positioning import MELEE, RANGED
from apps.api.schemas.stream import CombatStatePublic
from apps.api.services.turn_runner import combat_state_public
from tests.agent.confirming import confirm_turn
from tests.agent.test_combat import _player, _state
from tests.agent.test_positioning import _archer, _brute
from tools.dice import use_rng

HERO = "Eda"
FIGHTER = _state([], "")["character"]


def _field(width: int = 14, height: int = 10, terrain: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "width": width,
        "height": height,
        "theme": "open",
        "terrain": terrain or [],
    }


def _at(row: dict, square: str) -> dict:
    return {**row, "pos": list(parse_label(square))}


def _hero(square: str = "B5", init: int = 20) -> dict:
    return _at({**_player(init=init, ac=13), "display_name": HERO}, square)


def _foe(out: dict, name: str) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c["name"] == name)


def _me(out: dict) -> dict:
    return next(c for c in out["combat_state"]["combatants"] if c.get("is_player"))


def _entries(out: dict, purpose: str) -> list[dict]:
    return [d for d in out["adjudication_result"].dice_log if d.get("purpose") == purpose]


async def _grid_turn(
    fake, combatants, message, *, battlefield=None, character=None, seed=0, parsed=None
) -> dict:
    fake.responses[ParsedInput] = parsed or ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(combatants, message)
    state["combat_state"]["battlefield"] = battlefield or _field()
    if character is not None:
        state["character"] = character
    with use_rng(random.Random(seed)):
        out = await combat_round_node(state)
        return await confirm_turn(out, state)


# --- pure: squares, distance and paths ---------------------------------------------------------


def test_squares_are_named_like_a_chess_board_and_diagonals_count_five_feet() -> None:
    assert label((0, 0)) == "A1" and label((3, 4)) == "D5"
    assert (
        parse_label("d5") == (3, 4) and parse_label("D 5") == (3, 4) and parse_label("5D") is None
    )
    assert distance_ft((0, 0), (3, 3)) == 15  # three diagonal steps, 5 feet each (PHB grid rule)
    assert distance_ft((0, 0), (4, 1)) == 20


@pytest.mark.parametrize(
    ("text", "square"),
    [
        ("I move to D4 and attack the goblin", (3, 3)),
        ("Move to C7.", (2, 6)),
        ("I step into square b2", (1, 1)),
        ("I dash to H5", (7, 4)),
        ("I attack the goblin", None),
        ("I move toward the goblin", None),
        ("I do not move to D4", None),
        ('I say, "move to D4"', None),
        ("If the goblin falls I move to D4", None),
    ],
)
def test_a_named_square_is_read_from_typed_or_clicked_moves(text, square) -> None:
    found = square_move(text)
    assert (found[0] if found else None) == square


def test_walls_block_difficult_squares_cost_double_and_foes_cannot_be_entered() -> None:
    battlefield = _field(
        6,
        3,
        terrain=[
            {"x": 2, "y": 0, "kind": WALL, "feature": "boulder"},
            {"x": 2, "y": 1, "kind": WALL, "feature": "boulder"},
            {"x": 1, "y": 2, "kind": DIFFICULT, "feature": "brush"},
        ],
    )
    hero = _at(_player(), "A1")
    goblin = _at(_brute("Goblin", init=1), "D3")
    combatants = [hero, goblin]
    reach = reachable(battlefield, combatants, hero, 1 << 10)
    assert parse_label("C1") not in reach.costs and parse_label("C2") not in reach.costs  # walls
    assert parse_label("D3") not in reach.costs  # a hostile creature's square
    # Around the wall through the gap at C3: A1 -> B2 (5) -> C3 (5) -> D2 (5) = 15 feet.
    assert route_to(battlefield, combatants, hero, parse_label("D2"), 30).cost_ft == 15
    # Entering the brush at B3 costs 10 feet: A1 -> B2 (5) -> B3 (10).
    assert reach.costs[parse_label("B3")] == 15 and reach.costs[parse_label("C3")] == 10
    # Out of the budget, a square is not reachable.
    assert route_to(battlefield, combatants, hero, parse_label("F1"), 15) is None


def test_an_allys_square_costs_double_and_cannot_be_the_end_of_a_move() -> None:
    battlefield = _field(5, 1)
    hero = _at({**_player(), "name": "pc-1", "character_id": 1}, "A1")
    ally = _at({**_player(), "name": "pc-2", "character_id": 2}, "B1")
    combatants = [hero, ally]
    assert route_to(battlefield, combatants, hero, parse_label("B1"), 30) is None
    assert route_to(battlefield, combatants, hero, parse_label("C1"), 30).cost_ft == 15


def test_preview_uses_engine_costs_and_public_reaction_labels_without_mutation():
    hero = _hero("C1")
    foe = _at(_brute("Private identity", init=1), "D1")
    foe["display_name"] = "The guard"
    combat = {"battlefield": _field(10, 1), "combatants": [hero, foe]}
    before = deepcopy(combat)
    preview = preview_route(combat, "A1")
    assert preview["reachable"] and preview["cost_ft"] == 10
    assert preview["budget_ft"] == 30 and preview["provokes"] == ["The guard"]
    assert [row["label"] for row in preview["path"]] == ["C1", "B1", "A1"]
    assert not preview_route(combat, "D1")["reachable"]
    assert combat == before


def test_preview_obeys_prone_costs_dash_and_ended_encounters():
    hero = _hero("A1")
    hero["effects"] = [{"kind": "prone"}]
    combat = {"battlefield": _field(10, 1), "combatants": [hero]}
    assert not preview_route(combat, "E1")["reachable"]  # crawling: 40 feet
    assert preview_route(combat, "E1", dash=True)["cost_ft"] == 40
    assert preview_route(combat, "E1", dash=True)["reachable"]
    combat["ended"] = True
    assert not preview_route(combat, "B1")["reachable"]


@pytest.mark.parametrize(
    ("walls", "end", "visible"),
    [
        ([(1, 0)], (2, 0), False),
        ([(1, 0)], (1, 1), True),
        ([(1, 0), (0, 1)], (1, 1), False),
        ([(1, 1)], (3, 2), False),
        ([(1, 2)], (3, 2), True),
        ([], (0, 4), True),
    ],
)
def test_walls_and_diagonal_corners_block_sight_symmetrically(walls, end, visible):
    field = _field(5, 5, [{"x": x, "y": y, "kind": WALL} for x, y in walls])
    assert line_of_sight(field, (0, 0), end) is visible
    assert line_of_sight(field, end, (0, 0)) is visible


def test_crawling_adds_movement_cost_even_in_difficult_terrain():
    hero = _hero("A1")
    hero["effects"] = [{"kind": "prone"}]
    field = _field(3, 1, [{"x": 1, "y": 0, "kind": DIFFICULT}])
    assert route_to(field, [hero], hero, (1, 0), 30).cost_ft == 15
    assert route_to(field, [hero], hero, (2, 0), 30).cost_ft == 25


def test_fear_prevents_closer_steps_even_without_a_range_band_change():
    hero = _hero("B1")
    hero["effects"] = [{"kind": "frightened", "source": "The guard", "source_id": "Guard"}]
    foe = _at(_brute("Guard", init=1), "H1")
    combat = {"battlefield": _field(10, 2), "combatants": [hero, foe]}
    before = deepcopy(combat)
    preview = preview_route(combat, "D1")  # still beyond melee range
    assert not preview["reachable"] and "frightened" in preview["reason"]
    assert preview_route(combat, "A1")["reachable"]
    assert combat == before


def test_self_healing_is_measured_from_the_casters_planned_square():
    from agent.combat_grid import spell_reach_check
    from tests.agent.test_spells import CURE_WOUNDS

    hero = _hero("A1")
    combat = {"battlefield": _field(), "combatants": [hero]}
    assert spell_reach_check(combat, CURE_WOUNDS, hero, hero, from_square=(5, 0)) is None


@pytest.mark.asyncio
async def test_an_enemy_cannot_shoot_through_a_solid_wall(fake_llm_schema, fake_retrieval):
    field = _field(6, 3, [{"x": 2, "y": y, "kind": WALL} for y in range(3)])
    out = await _grid_turn(fake_llm_schema, [_hero("A2"), _at(_archer("Archer", init=1), "F2")],
                          "I take the dodge action", battlefield=field)
    assert not out["adjudication_result"].dice_log


@pytest.mark.asyncio
async def test_negated_standing_keeps_prone_crawl_cost(fake_llm_schema, fake_retrieval):
    hero = _hero("A1")
    hero["effects"] = [{"kind": "prone"}]
    out = await _grid_turn(fake_llm_schema, [hero, _at(_brute("Goblin", init=1), "L3")],
                          "I do not stand up. I move to C1.")
    assert pos_of(_me(out)) == parse_label("C1")
    assert any(e["kind"] == "prone" for e in _me(out).get("effects", []))
    assert "moves 20 feet" in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_wall_refuses_weapon_and_spell_before_roll_or_slot(fake_llm_schema, fake_retrieval):
    from tests.agent.test_spells import WIZARD

    field = _field(5, 3, [{"x": 2, "y": 1, "kind": WALL}])
    rows = [_hero("B2"), _at(_brute("Goblin", init=1), "D2")]
    out = await _grid_turn(
        fake_llm_schema, deepcopy(rows), "I shoot the goblin with my longbow", battlefield=field
    )
    assert "wall blocks" in out["narration"] and not out["adjudication_result"].dice_log
    out = await _grid_turn(
        fake_llm_schema,
        deepcopy(rows),
        "I cast Magic Missile at the goblin",
        battlefield=field,
        character=deepcopy(WIZARD),
        parsed=ParsedInput(
            intent="combat",
            check="spell_attack",
            target="goblin",
            entities={"spell": "Magic Missile"},
        ),
    )
    assert "wall blocks" in out["narration"] and not out["adjudication_result"].dice_log
    assert not out.get("spell_slots_expended")


@pytest.mark.asyncio
async def test_catalog_spell_range_is_refused_before_slot_or_turn(fake_llm_schema, fake_retrieval):
    from tests.agent.test_spells import WIZARD

    out = await _grid_turn(
        fake_llm_schema,
        [_hero("A1"), _at(_brute("Goblin", init=1), "Z1")],
        "I cast Magic Missile at the goblin",
        battlefield=_field(26, 1),
        character=deepcopy(WIZARD),
        parsed=ParsedInput(
            intent="combat",
            check="spell_attack",
            target="goblin",
            entities={"spell": "Magic Missile"},
        ),
    )
    assert "120-foot range" in out["narration"]
    assert not out["adjudication_result"].dice_log and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_standing_spends_half_speed_and_cannot_reorder_a_crawl(
    fake_llm_schema, fake_retrieval
):
    hero = _hero("A1")
    hero["effects"] = [{"kind": "prone"}]
    rows = [hero, _at(_brute("Goblin", init=1), "L3")]
    out = await _grid_turn(fake_llm_schema, deepcopy(rows), "I stand up and move to D1")
    assert pos_of(_me(out)) == parse_label("D1")
    assert not any(e["kind"] == "prone" for e in _me(out).get("effects", []))
    out = await _grid_turn(fake_llm_schema, deepcopy(rows), "I stand up and move to E1")
    assert "15 feet left" in out["narration"] and not out["adjudication_result"].dice_log
    out = await _grid_turn(fake_llm_schema, deepcopy(rows), "I move to B1 then stand up")
    assert "standing up before moving" in out["narration"]
    hero["effects"].append({"kind": "grappled"})
    out = await _grid_turn(fake_llm_schema, rows, "I stand up")
    assert "speed is zero" in out["narration"] and not out["adjudication_result"].dice_log


def test_walking_speed_reads_the_record_and_flyers_use_their_flying_speed() -> None:
    assert walking_speed("30 ft., fly 60 ft.") == 30
    assert walking_speed("20 ft.") == 20
    assert walking_speed("0 ft., fly 30 ft. (hover)") == 30
    assert walking_speed(None) is None


def test_reach_and_range_are_read_from_the_stat_line_and_survive_storage() -> None:
    text = """Hobgoblin Halberdier
Armor Class 15
Hit Points 22 (4d8 + 4)
Speed 30 ft.
STR DEX CON INT WIS CHA
13 (+1) 12 (+1) 12 (+1) 10 (+0) 10 (+0) 9 (-1)
Challenge 1 (200 XP)
Actions
Halberd. Melee Weapon Attack: +3 to hit, reach 10 ft., one target. Hit: 6 (1d10 + 1) slashing damage.
Longbow. Ranged Weapon Attack: +3 to hit, range 150/600 ft., one target. Hit: 5 (1d8 + 1) piercing damage.
"""  # fmt: skip
    stats = parse_monster_stats(text, name="Hobgoblin Halberdier")
    halberd, longbow = stats.attacks
    assert halberd.reach_ft == 10 and halberd.range_ft is None
    assert longbow.range_ft == (150, 600) and longbow.ranged
    stored = MonsterStats.from_dict(json.loads(json.dumps(stats.to_dict())))
    assert stored.attacks == stats.attacks
    single_range = parse_monster_stats(
        text.replace("150/600 ft.", "150 ft."), name="Hobgoblin Halberdier"
    )
    assert single_range.attacks[1].range_ft == (150, 150)


# --- opening the battlefield --------------------------------------------------------------------


def test_the_battlefield_places_foes_by_their_band_and_keeps_every_combatant_reachable() -> None:
    hero = {**_player(), "init": 15}
    goblin = {**_brute("Goblin", init=10), "band": MELEE}
    archer = {**_archer("Archer", init=5), "band": RANGED}
    combat_state = {"combatants": [hero, goblin, archer]}
    battlefield = open_battlefield(combat_state, scene_text="a ruined temple", seed="s1")
    assert battlefield["theme"] == "ruins"
    assert distance_ft(pos_of(hero), pos_of(goblin)) == 5  # a melee foe starts touching the hero
    assert distance_ft(pos_of(hero), pos_of(archer)) == 30  # a ranged foe 30 feet away
    squares = {tuple(pos_of(c)) for c in (hero, goblin, archer)}
    assert not squares & {(t["x"], t["y"]) for t in battlefield["terrain"]}
    for foe in (goblin, archer):
        assert route_to(battlefield, [hero], hero, pos_of(foe), 1 << 10) is not None
    # The same seed gives the same battlefield (reload and replay are stable).
    again = open_battlefield(
        {"combatants": [{**hero}, {**goblin}, {**archer}]}, scene_text="a ruined temple", seed="s1"
    )
    assert again == battlefield


def test_scenery_follows_the_scene_words() -> None:
    assert theme_for("The goblins wait in the dark cave")[0] == "cave"
    assert theme_for("A forest road")[1] == "tree"
    assert theme_for("somewhere")[0] == "open"


def test_bands_are_the_grid_projection_relative_to_the_hero() -> None:
    hero = _at(_player(), "B5")
    near = _at(_brute("Goblin", init=1), "C6")
    far = _at(_brute("Orc", init=1), "F5")
    combat_state = {"battlefield": _field(), "combatants": [hero, near, far]}
    sync_bands(combat_state, hero)
    assert near["band"] == MELEE and near["distance_ft"] == 5
    assert far["band"] == RANGED and far["distance_ft"] == 20


@pytest.mark.asyncio
async def test_a_new_encounter_opens_a_battlefield_with_every_combatant_on_it(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    import agent.graph_combat as graph_combat

    monkeypatch.setattr(graph_combat.config, "COMBAT_GRID_ENABLED", True)

    async def roster(state, targets, *, budget_summary=None):
        brute = _brute("Goblin", init=1)
        brute["monster_stats"].update(speed="30 ft.", declared=["speed 30 ft.: movement is not simulated"])
        return [brute, _archer("Archer", init=2)]

    monkeypatch.setattr(graph_combat, "_build_enemy_roster", roster)
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    state = _state(None, "I take the dodge action")
    state["combat_state"] = {"id": 7, "round": 1, "ended": False, "combatants": []}
    with use_rng(random.Random(1)):
        out = await combat_round_node(state)
    cs = out["combat_state"]
    assert cs["battlefield"]["width"] >= 14
    assert all(pos_of(c) is not None for c in cs["combatants"])
    assert _foe(out, "Goblin")["band"] == MELEE and _foe(out, "Archer")["band"] == RANGED
    assert "square battlefield" in out["adjudication_result"].mechanical_summary
    assert "walking and Dash use the battlefield" in out["adjudication_result"].mechanical_summary
    assert "movement is not simulated" not in out["adjudication_result"].mechanical_summary


# --- the hero's route ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hero_route_is_refused_beyond_speed_and_dash_doubles_it(
    fake_llm_schema, fake_retrieval
) -> None:
    rows = lambda: [_hero("A5"), _at(_brute("Goblin", init=1), "L2")]  # noqa: E731
    out = await _grid_turn(fake_llm_schema, rows(), "I move to H5")
    assert "35 feet of movement away" in out["narration"] and "30 feet left" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert out["combat_state"]["round"] == 1 and pos_of(_me(out)) == parse_label("A5")

    out = await _grid_turn(fake_llm_schema, rows(), "I dash to H5")
    assert pos_of(_me(out)) == parse_label("H5")
    assert out["combat_state"]["economy"]["action"] == "Dash"
    assert "moves 35 feet from A5 to H5" in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_a_blocked_or_taken_square_is_refused_with_no_dice(
    fake_llm_schema, fake_retrieval
) -> None:
    battlefield = _field(terrain=[{"x": 3, "y": 4, "kind": WALL, "feature": "boulder"}])
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5"), _at(_brute("Goblin", init=1), "C5")],
        "I move to D5",
        battlefield=battlefield,
    )
    assert "blocked or taken" in out["narration"] and out["adjudication_result"].dice_log == []


@pytest.mark.asyncio
async def test_close_in_walks_next_to_the_named_foe_and_the_attack_is_rolled(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5", init=20), _at(_brute("Goblin", init=1), "F5")],
        "I move next to the goblin and attack it",
        seed=2,
    )
    assert distance_ft(pos_of(_me(out)), pos_of(_foe(out, "Goblin"))) == 5
    assert _foe(out, "Goblin")["band"] == MELEE
    assert out["adjudication_result"].dice_log, "the attack was not rolled"
    assert "reach" not in out["narration"]


@pytest.mark.asyncio
async def test_a_melee_attack_out_of_reach_is_refused_before_any_roll(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5"), _at(_brute("Goblin", init=1), "F5")],
        "I attack the goblin",
    )
    assert "20 feet away" in out["narration"] and "melee reach is 5 feet" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_leaving_reach_on_the_grid_provokes_and_disengage_does_not(
    fake_llm_schema, fake_retrieval
) -> None:
    rows = lambda: [_hero("C5", init=10), _at(_brute("Goblin", init=20), "D5")]  # noqa: E731
    out = await _grid_turn(fake_llm_schema, rows(), "I move to A5", seed=6)
    assert len(_entries(out, "Goblin opportunity attack")) == 1
    assert _foe(out, "Goblin")["reaction_used"] is True

    out = await _grid_turn(fake_llm_schema, rows(), "I disengage and move to A5", seed=6)
    assert _entries(out, "Goblin opportunity attack") == []
    assert pos_of(_me(out)) == parse_label("A5")


@pytest.mark.asyncio
async def test_a_retreat_ends_out_of_every_foes_reach(fake_llm_schema, fake_retrieval) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("E5", init=20), _at(_brute("Goblin", init=1), "F5")],
        "I disengage and back away from the goblin",
    )
    # The west edge is 20 feet away: the hero goes straight back to it. The goblin, acting after the
    # hero, walks back up; the hero's own move is in the log.
    summary = out["adjudication_result"].mechanical_summary
    assert "Eda moves 20 feet from E5 to A5" in summary


# --- foes on the grid ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_melee_foe_walks_up_and_attacks_and_a_far_one_dashes_without_attacking(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [
            _hero("B5", init=20),
            _at(_brute("Goblin", init=1), "G5"),
            _at(_brute("Orc", init=2), "M5"),
        ],
        "I take the dodge action",
        seed=4,
    )
    assert distance_ft(pos_of(_me(out)), pos_of(_foe(out, "Goblin"))) == 5
    assert _entries(out, "Goblin Club attack"), "the goblin did not attack after walking up"
    summary = out["adjudication_result"].mechanical_summary
    assert "Orc takes the Dash action and makes no attack" in summary
    assert _entries(out, "Orc Club attack") == []
    assert (
        distance_ft(pos_of(_me(out)), pos_of(_foe(out, "Orc"))) == 5
    )  # 55 feet: 50 walked, then touching


@pytest.mark.asyncio
async def test_a_ranged_foe_shoots_from_where_it_stands(fake_llm_schema, fake_retrieval) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5", init=20), _at(_archer("Archer", init=1), "H5")],
        "I take the dodge action",
        seed=5,
    )
    assert pos_of(_foe(out, "Archer")) == parse_label("H5")
    assert _entries(out, "Archer Shortbow attack")


@pytest.mark.asyncio
async def test_a_ranged_weapon_beyond_normal_range_has_disadvantage_and_beyond_long_is_refused(
    fake_llm_schema, fake_retrieval
) -> None:
    slinger = {**FIGHTER, "inventory": {"weapons": ["sling"], "armor": "leather armor"}}
    battlefield = _field(width=26, height=6)
    out = await _grid_turn(
        fake_llm_schema,
        [
            _hero("A3", init=20),
            _at(_archer("Archer", init=1), "K3"),
        ],  # 50 feet: beyond 30, within 120
        "I shoot the archer with my sling",
        battlefield=battlefield,
        character=slinger,
        parsed=ParsedInput(intent="combat", check="ranged_attack", target="archer"),
        seed=7,
    )
    hero_attack = [
        d
        for d in out["adjudication_result"].dice_log
        if "attack" in str(d.get("purpose")) and "Archer" not in str(d.get("purpose"))
    ]
    assert hero_attack and hero_attack[0]["roll_mode"] == "disadvantage"
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("A3", init=20), _at(_archer("Archer", init=1), "Z3")],  # 125 feet
        "I shoot the archer with my sling",
        battlefield=battlefield,
        character=slinger,
        parsed=ParsedInput(intent="combat", check="ranged_attack", target="archer"),
    )
    assert "beyond the 120-foot long range" in out["narration"]
    assert out["adjudication_result"].dice_log == []


# --- the public view and reload -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_positions_and_battlefield_survive_a_json_reload_and_match_the_frame_contract(
    fake_llm_schema, fake_retrieval
) -> None:
    battlefield = _field(terrain=[{"x": 5, "y": 5, "kind": DIFFICULT, "feature": "brush"}])
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5"), _at(_brute("Goblin", init=1), "C5")],
        "I take the dodge action",
        battlefield=battlefield,
    )
    stored = json.loads(json.dumps(out["combat_state"]))
    payload = combat_state_public(stored)
    CombatStatePublic.model_validate(payload)  # the done frame's combat_state contract
    assert payload["battlefield"]["terrain"] == [
        {"x": 5, "y": 5, "kind": "difficult", "feature": "brush"}
    ]
    hero = next(c for c in payload["combatants"] if c["is_player"])
    goblin = next(c for c in payload["combatants"] if not c["is_player"])
    assert hero["position"] == {"x": 1, "y": 4, "label": "B5"} and hero["speed_ft"] == 30
    assert goblin["position"]["label"] == "C5" and goblin["band"] == MELEE


def test_an_encounter_without_a_battlefield_keeps_its_bands_and_publishes_no_grid() -> None:
    payload = combat_state_public(
        {
            "round": 1,
            "ended": False,
            "combatants": [_player(), {**_brute("Goblin", init=1), "band": RANGED}],
        }
    )
    CombatStatePublic.model_validate(payload)
    assert "battlefield" not in payload
    assert all("position" not in c for c in payload["combatants"])


# --- party fights -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_a_party_fight_a_foe_walks_to_the_hero_it_chose(
    fake_llm_schema, fake_retrieval
) -> None:
    from tests.agent.test_party_combat import (
        FIGHTER as EDA,
        WIZARD,
        _combat,
        _enemy,
        _hero as _pc,
        _state as _party_state,
    )

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    combat = _combat(
        [
            _at(_pc(EDA, 15), "B5"),
            _at(_pc(WIZARD, 10, bonus=2), "B6"),
            _at(_enemy("Goblin", hp=30, init=12), "H5"),
        ],
        cursor=0,
    )
    combat["battlefield"] = _field()
    state = _party_state(combat, EDA, "I take the dodge action")
    with use_rng(random.Random(3)):
        out = await combat_round_node(state)
    rows = out["combat_state"]["combatants"]
    goblin = next(c for c in rows if c["name"] == "Goblin")
    heroes = [c for c in rows if c.get("is_player")]
    assert min(distance_ft(pos_of(goblin), pos_of(h)) for h in heroes) == 5
    assert "Goblin moves 25 feet from H5" in out["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
async def test_grid_is_opt_in_for_new_encounters(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    import agent.graph_combat as graph_combat

    async def roster(state, targets, *, budget_summary=None):
        return [_brute("Goblin", init=1)]

    monkeypatch.setattr(graph_combat, "_build_enemy_roster", roster)
    monkeypatch.setattr(graph_combat.config, "COMBAT_GRID_ENABLED", False)
    state = _state(None, "I take the dodge action")
    state["combat_state"] = {"id": 7, "round": 1, "ended": False, "combatants": []}
    out = await combat_round_node(state)
    assert "battlefield" not in out["combat_state"]
    assert all(pos_of(row) is None for row in out["combat_state"]["combatants"])


@pytest.mark.asyncio
async def test_square_move_after_attack_keeps_attack_before_withdrawal(
    fake_llm_schema, fake_retrieval
) -> None:
    goblin = _at(_brute("Goblin", init=1), "D5")
    goblin["hp_current"] = goblin["hp_max"] = 100
    out = await _grid_turn(
        fake_llm_schema, [_hero("C5"), goblin], "I attack the goblin then move to A5", seed=2
    )
    events = out["combat_state"]["round_events"]
    kinds = [event["kind"] for event in events]
    assert kinds.index("player_attack") < kinds.index("opportunity_attack") < kinds.index("move")
    assert pos_of(_me(out)) == parse_label("A5")


@pytest.mark.asyncio
async def test_two_square_moves_spend_the_combined_route_budget(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("A5"), _at(_brute("Goblin", init=1), "L1")],
        "I move to E5 then move to H5",
    )
    assert "15 feet of movement away" in out["narration"] and "10 feet left" in out["narration"]
    assert out["adjudication_result"].dice_log == []
    assert pos_of(_me(out)) == parse_label("A5") and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_moves_around_attack_are_refused_instead_of_reordered(
    fake_llm_schema, fake_retrieval
) -> None:
    out = await _grid_turn(
        fake_llm_schema,
        [_hero("B5"), _at(_brute("Goblin", init=1), "E5")],
        "I move to D5 then attack the goblin then move to A5",
    )
    assert "separate movement steps" in out["narration"]
    assert out["adjudication_result"].dice_log == [] and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["grappled", "restrained"])
async def test_immobilized_hero_cannot_move_to_a_square(
    fake_llm_schema, fake_retrieval, condition
) -> None:
    from agent.combatants import add_effect

    hero = _hero("B5")
    add_effect(hero, condition, source="Goblin", round_applied=1)
    out = await _grid_turn(
        fake_llm_schema, [hero, _at(_brute("Goblin", init=1), "C5")], "I move to A5"
    )
    assert "0 feet left" in out["narration"]
    assert pos_of(_me(out)) == parse_label("B5") and out["adjudication_result"].dice_log == []


@pytest.mark.asyncio
async def test_frightened_route_cannot_approach_and_then_retreat(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.combatants import add_effect

    hero = _hero("D5")
    add_effect(hero, "frightened", source="Goblin", source_id="Goblin", round_applied=1)
    out = await _grid_turn(
        fake_llm_schema,
        [hero, _at(_brute("Goblin", init=1), "H5")],
        "I move to E5 then move to A5",
    )
    assert "cannot willingly move closer" in out["narration"]
    assert pos_of(_me(out)) == parse_label("D5") and out["adjudication_result"].dice_log == []


@pytest.mark.asyncio
async def test_reach_weapon_can_strike_at_ten_feet_but_cannot_grapple_there(
    fake_llm_schema, fake_retrieval
) -> None:
    fighter = {**FIGHTER, "inventory": {"weapons": ["glaive"], "armor": "chain mail"}}
    rows = lambda: [_hero("B5"), _at(_brute("Goblin", init=1), "D5")]  # noqa: E731
    out = await _grid_turn(
        fake_llm_schema, rows(), "I attack the goblin with my glaive", character=fighter
    )
    assert any(event["kind"] == "player_attack" for event in out["combat_state"]["round_events"])
    out = await _grid_turn(fake_llm_schema, rows(), "I grapple the goblin", character=fighter)
    assert "melee reach is 5 feet" in out["narration"]
    assert out["adjudication_result"].dice_log == []
