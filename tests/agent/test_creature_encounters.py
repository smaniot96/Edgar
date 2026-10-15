"""Encounter integration rejects identity substitutions before initiative or persistence."""

import pytest

from agent.graph_combat import (
    EncounterSetup,
    EnemyStat,
    _build_enemy_roster,
    _name_matches,
    combat_round_node,
    select_target,
)
from agent.models.parsed_input import ParsedInput
from apps.api.services.turn_runner import TurnError, _debug_payload, combat_state_public
from db.vector.creatures import CreatureResolutionError


def verified_record():
    return {
        "creature_id": "e" * 64,
        "creature_name": "Goblin",
        "creature_key": "goblin",
        "text": "Goblin\nArmor Class 15\nHit Points 7\n"
        + "Synthetic trait. " * 80
        + "FINAL ACTION",
        "source": "Synthetic Manual",
        "collection": "rules_monster_manual",
        "page": 167,
        "page_end": 168,
        "source_pages": [167, 168],
        "rules_edition": "2014",
        "source_sha256": "a" * 64,
        "extraction_sha256": "b" * 64,
        "source_version": "version1",
        "index_version": "index1",
    }


def install_record(monkeypatch):
    record = verified_record()
    monkeypatch.setattr("agent.graph_combat.resolve_creature", lambda *args, **kwargs: record)
    return record


@pytest.mark.asyncio
async def test_builder_receives_full_exact_record_and_persists_compact_provenance(
    fake_llm_schema, monkeypatch
):
    record = install_record(monkeypatch)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name="Goblin", hp_max=8)]
    )
    roster = await _build_enemy_roster({"player_input": "Attack Goblin"}, ["Goblin"])
    assert roster[0]["creature_id"] == record["creature_id"]
    assert roster[0]["source_reference"]["source_sha256"] == record["source_sha256"]
    assert roster[0]["source_reference"]["rules_edition"] == "2014"
    assert roster[0]["source_reference"]["source_version"] == "version1"
    assert "text" not in roster[0]["source_reference"]
    prompt = next(
        messages for schema, messages in fake_llm_schema.calls if schema is EncounterSetup
    )
    assert "FINAL ACTION" in prompt[1].content
    assert roster[0]["stat_adaptation"] == "legacy_solo_encounter"


@pytest.mark.parametrize("names", [["Hobgoblin"], ["Goblin", "Captain Vellum"]])
@pytest.mark.asyncio
async def test_model_cannot_rename_or_add_unverified_roster_identities(
    fake_llm_schema, monkeypatch, names
):
    install_record(monkeypatch)
    fake_llm_schema.responses[EncounterSetup] = EncounterSetup(
        enemies=[EnemyStat(name=n, hp_max=8) for n in names]
    )
    with pytest.raises(CreatureResolutionError, match="unverified creature identity"):
        await _build_enemy_roster({}, ["Goblin"])


@pytest.mark.asyncio
async def test_unknown_name_blocks_new_encounter_without_builder_or_dice(
    fake_llm_schema, fake_retrieval, monkeypatch
):
    def unknown(*args, **kwargs):
        raise CreatureResolutionError("Unknown exact creature: Captain Vellum")

    monkeypatch.setattr("agent.graph_combat.resolve_creature", unknown)
    monkeypatch.setattr(
        "agent.graph_combat.roll", lambda *_: pytest.fail("unresolved encounter rolled dice")
    )
    out = await combat_round_node(
        {
            "player_input": "I attack Captain Vellum",
            "parsed_input": ParsedInput(intent="combat", target="Captain Vellum"),
            "source_mode": "homebrew",
            "adventure_collections": [],
        }
    )
    assert out["error_code"] == "creature_identity_unresolved"
    assert "combat_state" not in out
    assert not any(schema is EncounterSetup for schema, _ in fake_llm_schema.calls)
    error = TurnError(out["error_code"])
    assert error.status_code == 409 and "exact verified creature name" in error.message


def test_targeting_and_ally_matching_never_treat_hobgoblin_as_goblin():
    assert not _name_matches("Goblin", "Hobgoblin")
    assert not _name_matches("Adult Red Dragon", "Ancient Red Dragon")
    hobgoblin = {"name": "Hobgoblin", "alive": True, "hp_current": 9}
    target, question = select_target([hobgoblin], ["Goblin"])
    assert target is None and question


def test_private_creature_provenance_stays_out_of_player_and_debug_payloads():
    combat = {
        "combatants": [
            {
                "name": "Captain Ash",
                "hp_current": 8,
                "creature_id": "private-creature-id",
                "source_reference": {"actor_id": "private_actor", "passage_id": "secret_stats"},
            }
        ]
    }
    public = combat_state_public(combat)
    debug = _debug_payload(
        parsed=None, rules_ctx=[], adv_ctx=[], world_flags_in={}, combat_state=combat, timings={}
    )
    assert "private_actor" not in str(public) + str(debug)
    assert "private-creature-id" not in str(public) + str(debug)
    assert combat["combatants"][0]["source_reference"]["actor_id"] == "private_actor"
