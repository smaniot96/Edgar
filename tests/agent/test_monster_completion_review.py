"""Independent EDG-043 integration regressions, using fake providers and typed fixtures."""

import copy
import json
from contextlib import closing
from dataclasses import replace

import pytest
from qdrant_client import QdrantClient

from agent.combatants import add_effect, effects_of
from agent.conditions import apply_spell_condition, settle_concentration, start_concentration
from agent.graph_combat import _exact_enemy, combat_round_node
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.monster_stats import LairAction, parse_lair_actions, parse_monster_stats
from agent.spells import catalog_spell, resolve_monster_spell
from apps.api.services.turn_runner import _debug_payload, combat_state_public
from db.vector.collections import invalidate_collection_cache
from db.vector.creatures import CreatureResolutionError, resolve_creature
from edgar_core.artifacts import text_digest
from edgar_core.manifests import AdventureManifest, manifest_digest
from tests.agent.test_concentration import _summary
from tests.agent.test_monster_riders import WAIT, _exact, _state
from tests.agent.test_spells import ScriptedDice
from tests.test_creature_identity import actor_manifest, put, stat
from tools.dice import use_rng


def _quiet_foe(name: str, initiative: int, *, lair: bool = False) -> dict:
    text = (
        f"{name}\nMedium humanoid, neutral\nArmor Class 12\nHit Points 30 (4d8 + 12)\n"
        "Speed 30 ft.\nActions\nObserve. The creature watches the room.\n"
    )
    foe = {**_exact(text, name, hp=30), "init": initiative}
    if lair:
        stats = parse_monster_stats(text, name=name)
        foe["monster_stats"] = replace(
            stats, lair_actions=(LairAction("Tremor", "DEX", 12, condition="prone"),)
        ).to_dict()
    return foe


@pytest.mark.parametrize("hero_init", [25, 20, 15])
@pytest.mark.asyncio
async def test_lair_count20_interleaves_every_actor_and_loses_all_ties(
    fake_llm_schema, fake_retrieval, hero_init: int
) -> None:
    """The count-20 event must interleave foes on either side of the hero, including ties."""
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(
        success=False, mechanical_summary="HERO TURN"
    )
    scores = [("Fast Guard", 30), ("Tied Guard", 20), ("Slow Guard", 19), ("Lair Owner", 5)]
    foes = [_quiet_foe(name, score, lair=name == "Lair Owner") for name, score in scores]
    state = _state("I wait and watch", foes, WAIT)
    state["combat_state"]["combatants"][0]["init"] = hero_init
    dice = ScriptedDice([20])
    with use_rng(dice):
        out = await combat_round_node(state)
    assert dice.faces == []
    summary = _summary(out)
    observed = {
        name: summary.index(f"{name} has no supported attack") for name, _ in scores
    }
    observed["hero"] = summary.index("HERO TURN")
    lair_index = summary.index("At initiative 20")
    for name, score in [("hero", hero_init), *scores]:
        assert (observed[name] < lair_index) == (score >= 20), summary
    assert summary.count("At initiative 20") == 1


@pytest.mark.parametrize(
    "extra",
    [
        " A creature that fails is also blinded for 1 minute.",
        " The damage repeats at the end of each creature's turn.",
    ],
)
def test_lair_parser_does_not_execute_a_substring_of_an_unsupported_action(extra: str) -> None:
    text = (
        "On initiative count 20 (losing initiative ties), the dragon takes a lair action "
        "to cause one of the following effects; the dragon can't use the same effect two rounds in a row:\n"
        "• Magma erupts from a point on the ground the dragon can see within 120 feet of it, "
        "creating a 20-foot-high, 5-foot-radius geyser. Each creature in the geyser's area "
        "must make a DC 15 Dexterity saving throw, taking 21 (6d6) fire damage on a failed "
        "save, or half as much damage on a successful one."
    )
    assert parse_lair_actions(text), "The named source shape is recognized before alteration."
    assert parse_lair_actions(text + extra) == ()


@pytest.mark.asyncio
async def test_lair_damage_and_no_consecutive_repetition_survive_round_reload(
    fake_llm_schema, fake_retrieval, monkeypatch
) -> None:
    from agent import graph_combat

    request = graph_combat.narrator_request
    seen_effects = []

    def capture_request(state, profile):
        # The narrator's own input: the round's engine effects reach the prompt and boundary.
        seen_effects.append(state["engine_effects"])
        return request(state, profile)

    monkeypatch.setattr(graph_combat, "narrator_request", capture_request)
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    source = (
        "On initiative count 20 (losing initiative ties), the dragon takes a lair action to cause "
        "one of the following effects; the dragon can't use the same effect two rounds in a row:\n"
        "• Magma erupts from a point on the ground the dragon can see within 120 feet of it, "
        "creating a 20-foot-high, 5-foot-radius geyser. Each creature in the geyser's area must "
        "make a DC 15 Dexterity saving throw, taking 21 (6d6) fire damage on a failed save, "
        "or half as much damage on a successful one.\n"
        "• A tremor shakes the lair in a 60-foot radius around the dragon. Each creature other "
        "than the dragon on the ground in that area must succeed on a DC 15 Dexterity saving "
        "throw or be knocked prone."
    )
    foe = _quiet_foe("Lair Owner", 5)
    foe["monster_stats"]["lair_actions"] = [
        action.__dict__ for action in parse_lair_actions(source)
    ]
    state = _state("I wait and watch", [foe], WAIT)
    dice = ScriptedDice([20, 1, 1, 1, 1, 1, 1])
    with use_rng(dice):
        first = await combat_round_node(state)
    assert dice.faces == []
    assert "takes 3 fire damage from Magma Geyser" in _summary(first)
    follow = copy.deepcopy(state)
    follow["combat_state"] = json.loads(json.dumps(first["combat_state"]))
    follow["character"]["hp_current"] = 9
    dice = ScriptedDice([1])
    with use_rng(dice):
        second = await combat_round_node(follow)
    assert dice.faces == []
    assert "uses its lair action: Tremor" in _summary(second)
    assert "Magma Geyser" not in _summary(second)
    owner = next(c for c in second["combat_state"]["combatants"] if c["name"] == "Lair Owner")
    assert (owner["lair_last_action"], owner["lair_last_round"]) == ("Tremor", 2)
    hero = next(c for c in second["combat_state"]["combatants"] if c["is_player"])
    assert any(e["kind"] == "prone" for e in effects_of(hero))
    assert "prone" in seen_effects[-1]  # the narrator boundary recognizes the executed effect


def test_optional_debug_does_not_reveal_unused_lair_or_spell_source_data() -> None:
    combat = {
        "round": 1,
        "combatants": [{
            "name": "Captain Ash",
            "hp_current": 30,
            "ac": 12,
            "source_reference": {"lair_reference": {"passage_id": "PRIVATE_PASSAGE"}},
            "monster_stats": {
                "name": "Captain Ash", "ac": 12, "hp_max": 30,
                "spellcasting": {"spells": ["PRIVATE_SPELL_LIST"]},
                "lair_actions": [{"name": "PRIVATE_LAIR_OPTION"}],
                "declared": ["PRIVATE_SPELL_LIST: not executed"],
            },
        }],
    }
    unchanged = copy.deepcopy(combat)
    public = combat_state_public(combat)
    debug = _debug_payload(
        parsed=None, rules_ctx=[], adv_ctx=[], world_flags_in={}, combat_state=combat, timings={}
    )
    assert "PRIVATE_" not in json.dumps({"public": public, "debug": debug})
    assert combat == unchanged


def test_reviewed_lair_binding_resolves_through_real_disposable_source_index(
    monkeypatch, tmp_path
) -> None:
    """An unlocked, reviewed private passage reaches the roster; flags cannot bypass its gate."""
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(
        "db.vector.retrieval.embed_query", lambda *_: pytest.fail("exact source lookup embedded")
    )
    text = (
        "On initiative count 20 (losing initiative ties), the dragon takes a lair action "
        "to cause one of the following effects; the dragon can't use the same effect two rounds "
        "in a row:\n• A tremor shakes the lair in a 60-foot radius around the dragon. "
        "Each creature other than the dragon on the ground in that area must succeed on a "
        "DC 15 Dexterity saving throw or be knocked prone."
    )
    manifest, chunks = actor_manifest({
        "kind": "template", "creature_name": "Adult Red Dragon",
        "collection": "rules_monster_manual", "source_sha256": "a" * 64,
        "rules_edition": "2014",
    })
    body = manifest.model_dump()
    body["entities"][0]["lair_reference"] = {
        "creature_name": "Adult Red Dragon", "passage_id": "stats",
    }
    body["entities"][0]["surprise"] = "hero"
    body["passages"][1].update({
        "end": len(text), "text_sha256": text_digest(text), "requires": {"lair_open": "true"},
    })
    manifest = AdventureManifest.model_validate(body)
    manifest.reviewed_sha256 = manifest_digest(manifest)
    chunks[1].update({
        "text": text, "text_sha256": text_digest(text), "span_end": len(text),
        "requires": {"lair_open": "true"},
    })
    for chunk in chunks:
        chunk["manifest_sha256"] = manifest.reviewed_sha256
    invalidate_collection_cache()
    try:
        with closing(QdrantClient(":memory:")) as client:
            put(client, [stat("Adult Red Dragon")])
            put(client, chunks, collection="module")
            kwargs = {
                "client": client, "adventure_manifests": {"module": manifest.model_dump()},
                "current_scene_id": "hall",
            }
            with pytest.raises(CreatureResolutionError, match="locked"):
                resolve_creature("Captain Ash", world_flags={"in_lair": "true"}, **kwargs)
            resolved = resolve_creature("Captain Ash", world_flags={"lair_open": "true"}, **kwargs)
            foe = _exact_enemy({**resolved, "encounter_name": "Captain Ash"})
            assert foe["monster_stats"]["lair_actions"][0]["name"] == "Tremor"
            assert foe["hero_surprised"] is True
            assert foe["source_reference"]["lair_reference"]["manifest_sha256"] == manifest.reviewed_sha256
            assert foe["source_reference"]["lair_reference"]["source_sha256"] == "c" * 64
    finally:
        invalidate_collection_cache()


@pytest.mark.parametrize("invalid", ["non_actor", "no_stats", "public", "wrong_creature", "unbound"])
def test_lair_authoring_rejects_unreviewable_actor_bindings(invalid: str) -> None:
    manifest, _ = actor_manifest({
        "kind": "template", "creature_name": "Adult Red Dragon",
        "collection": "rules_monster_manual", "source_sha256": "a" * 64,
        "rules_edition": "2014",
    })
    body = manifest.model_dump()
    actor = body["entities"][0]
    actor["lair_reference"] = {"creature_name": "Adult Red Dragon", "passage_id": "stats"}
    if invalid == "non_actor":
        actor["kind"] = "clue"
    elif invalid == "no_stats":
        actor["stat_reference"] = None
    elif invalid == "public":
        actor["lair_reference"]["passage_id"] = "public"
    elif invalid == "wrong_creature":
        actor["lair_reference"]["creature_name"] = "Adult Black Dragon"
    else:
        body["passages"][1]["entity_ids"] = []
    with pytest.raises(ValueError):
        AdventureManifest.model_validate(body)


@pytest.mark.asyncio
async def test_hold_person_cast_before_hero_initiative_prevents_the_declared_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    text = """Watcher
Medium humanoid, neutral
Armor Class 12
Hit Points 30 (4d8 + 12)
Speed 30 ft.
STR DEX CON INT WIS CHA
10 (+0) 12 (+1) 14 (+2) 16 (+3) 18 (+4) 10 (+0)
Spellcasting. The watcher is a 3rd-level spellcaster. Its spellcasting ability is Wisdom (spell save DC 14, +6 to hit with spell attacks). The watcher has the following cleric spells prepared:
2nd level (2 slots): hold person
Actions
Observe. The watcher watches the room.
"""
    foe = {**_exact(text, "Watcher", hp=30), "init": 30, "band": "melee"}
    state = _state(
        "I attack the Watcher", [foe], ParsedInput(intent="combat", check="attack", target="Watcher")
    )
    dice = ScriptedDice([1, 1])  # failed initial WIS save, then failed hero end-of-turn repeat
    with use_rng(dice):
        out = await combat_round_node(state)
    assert dice.faces == []
    saved = out["combat_state"]["combatants"]
    assert next(c for c in saved if c["name"] == "Watcher")["hp_current"] == 30
    hero = next(c for c in saved if c["is_player"])
    assert any(e["kind"] == "paralyzed" for e in hero["effects"])
    assert "cannot act" in _summary(out)


def test_one_casters_stale_hold_does_not_remove_another_casters_effect_after_reload() -> None:
    first = {"name": "Watcher"}
    second = {"name": "Watcher 2", "display_name": "Watcher"}
    hero = {"name": "player"}
    for caster in (first, second):
        start_concentration(
            caster, "Hold Person", round_applied=1, duration_rounds=10,
            source_id=f"{caster['name']}:hold person",
        )
    assert apply_spell_condition(
        hero, "paralyzed", source="Hold Person", round_applied=1, duration_rounds=10,
        source_id="Watcher 2:hold person",
    )
    first, second, hero = json.loads(json.dumps([first, second, hero]))
    assert settle_concentration(first, [hero], 2) == "Hold Person"
    assert "concentration" not in first
    assert len(effects_of(hero)) == 1
    assert settle_concentration(second, [hero], 2) is None
    assert settle_concentration(second, [hero], 10) == "Hold Person"
    assert effects_of(hero) == []


def test_legacy_concentration_without_source_id_still_expires_its_effect() -> None:
    caster = {"name": "player"}
    foe = {"name": "Guard"}
    start_concentration(caster, "Hold Person", round_applied=1, duration_rounds=10)
    assert apply_spell_condition(
        foe, "paralyzed", source="Hold Person", round_applied=1, duration_rounds=10,
    )
    assert settle_concentration(caster, [foe], 10) == "Hold Person"
    assert effects_of(foe) == []


@pytest.mark.parametrize("attack_kind", ["Melee", "Ranged"])
@pytest.mark.asyncio
async def test_a_successful_close_hit_on_paralyzed_hero_doubles_dice_not_modifier(
    fake_llm_schema, fake_retrieval, attack_kind: str
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    text = (
        "Guard\nMedium humanoid, neutral\nArmor Class 12\nHit Points 30\nActions\n"
        f"Sword. {attack_kind} Weapon Attack: +4 to hit, reach 5 ft., one target. "
        "Hit: 5 (1d6 + 2) slashing damage.\n"
    )
    state = _state("I wait", [{**_exact(text, "Guard", hp=30), "band": "melee"}], WAIT)
    add_effect(state["combat_state"]["combatants"][0], "paralyzed", source="Hold Person", round_applied=1)
    dice = ScriptedDice([15, 14, 3, 4])
    with use_rng(dice):
        out = await combat_round_node(state)
    assert dice.faces == []
    damage = next(d for d in out["adjudication_result"].dice_log if d["purpose"] == "Guard Sword damage")
    assert (damage["expression"], damage["total"]) == ("2d6+2", 9)
    assert "Critical hit!" in _summary(out)
    assert next(c for c in out["combat_state"]["combatants"] if c["is_player"])["hp_current"] == 3


@pytest.mark.asyncio
async def test_close_spell_attack_critically_hits_a_paralyzed_hero(
    fake_llm_schema, fake_retrieval
) -> None:
    from tests.agent.test_monster_spellcasting import SINGLE_BOLT, _priest

    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    foe = _priest(init=5, spells=SINGLE_BOLT)
    foe["band"] = "melee"
    # Exhaust Guiding Bolt: this round must use the listed Fire Bolt.
    foe["monster_spell_slots_expended"] = {"1": 1}
    state = _state("I wait", [foe], WAIT)
    add_effect(state["combat_state"]["combatants"][0], "paralyzed", source="Hold Person", round_applied=1)
    dice = ScriptedDice([15, 14, 2, 3])
    with use_rng(dice):
        out = await combat_round_node(state)
    assert dice.faces == []
    damage = next(d for d in out["adjudication_result"].dice_log if d["purpose"] == "Fire Bolt damage")
    assert (damage["expression"], damage["total"]) == ("2d10", 5)


@pytest.mark.asyncio
async def test_lair_does_not_attack_after_the_hero_has_fled(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    foe = _quiet_foe("Lair Owner", 5, lair=True)
    state = _state("I flee", [foe], WAIT)
    state["combat_state"]["combatants"][0]["init"] = 25
    with use_rng(ScriptedDice([])):
        out = await combat_round_node(state)
    assert out["combat_state"]["ended"]
    assert "At initiative 20" not in _summary(out)


@pytest.mark.parametrize(
    "name,faces,damage,healing,condition",
    [
        ("Fire Bolt", [8, 2, 3], 5, 0, None),  # fifth-level caster: 2d10
        ("Magic Missile", [1, 2, 3], 9, 0, None),  # three automatically hitting darts, +1 each
        ("Sacred Flame", [7, 2, 3], 5, 0, None),  # failed save, fifth-level caster: 2d8
        ("Sacred Flame", [20], 0, 0, None),  # successful save: no damage
        ("Cure Wounds", [3], 0, 7, None),
        ("Healing Word", [2], 0, 6, None),
        ("Guiding Bolt", [8, 1, 2, 3, 4], 10, 0, "marked"),
        ("Blindness/Deafness", [7], 0, 0, "blinded"),
        ("Hold Person", [7], 0, 0, "paralyzed"),
    ],
)
def test_all_monster_catalog_effects_use_record_numbers_and_book_dice(
    name: str, faces: list[int], damage: int, healing: int, condition: str | None
) -> None:
    """2014 PHB spell oracles; monster DC/attack/ability replace hero class-derived numbers."""
    spec = catalog_spell(name)
    assert spec is not None
    dice = ScriptedDice(faces.copy())
    with use_rng(dice):
        result = resolve_monster_spell(
            spec, attack_bonus=7, save_dc=15, caster_level=5,
            target_ac=15, target_save_bonus=2, healing_modifier=4,
        )
    assert dice.faces == []
    assert (result.damage, result.healing, result.condition) == (damage, healing, condition)
    if result.attack is not None:
        assert (result.attack.modifier, result.attack.total, result.attack.success) == (7, 15, True)
    if spec.save_ability:
        assert result.save_dc == 15
