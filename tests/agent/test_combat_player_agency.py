"""Desktop playtest regressions: the player's actual intent, order, and visible participants."""

import copy
import json

import pytest

from agent.action_economy import has_attack_words
from agent.combat_confirmation import PENDING_KEY
from agent.combat_intent import is_flee, is_surrender
from agent.graph_combat import combat_round_node, visible_scene_actors
from agent.models.adjudication import AdjudicationResult, CombatOffer
from agent.models.parsed_input import ParsedInput
from tests.agent.test_combat import _enemy, _player, _state
from tests.agent.test_declaration_confirmation import _hero, _rolled, _say, _start


def _guz():
    return _start([_player(init=20, ac=99), {**_enemy("Guz", hp=100, ac=1, init=1), "band": "melee", "distance_ft": 5}])


@pytest.mark.parametrize("text", [
    "I do not attack Guz.", "I won't attack Guz.", 'I say "I attack Guz".',
    "If I attack Guz, can I escape?", "The guard attacks Guz.",
])
def test_nonaffirmative_attack_does_not_authorize_damage(text):
    assert not has_attack_words(text)


@pytest.mark.parametrize("text", ["I do not surrender.", 'I say "surrender".', "If I surrender, will Guz listen?"])
def test_nonaffirmative_offer_is_not_surrender(text):
    assert not is_surrender(text)


@pytest.mark.parametrize(
    "text",
    [
        "I offer to hand over the sheep and come peacefully.",
        "I lay down my sword.",
        "I stop fighting.",
    ],
)
def test_natural_handover_terms_are_nonviolent_offers(text):
    assert is_surrender(text) and not is_flee(text)


@pytest.mark.asyncio
async def test_offer_uses_private_canonical_actor_binding_but_keeps_public_name(
    fake_llm_schema, fake_retrieval, monkeypatch
):
    monkeypatch.setattr(
        "agent.combat_offers.observation_span_catalog",
        lambda _: {
            "terms": {
                "text": "Guz accepts surrender when the sheep is offered.",
                "reference_id": "source",
            },
        },
    )

    async def response(*args, **kwargs):
        return AdjudicationResult(
            success=True,
            combat_offer=CombatOffer(
                decision="accepted",
                actor_name="Guz",
                source_span_id="terms",
                action_quote="hand over the sheep",
                achievement_basis="The actual offer meets the demand.",
                public_response="Agreed. We stop fighting.",
            ),
        )

    monkeypatch.setattr("agent.graph_combat.adjudicate", response)
    combat = _guz()
    combat["combatants"][1].update(
        name="the half-orc", canonical_name="Guz", target_aliases=["half-orc"]
    )
    asked = await _say(
        fake_llm_schema,
        combat,
        "I offer to hand over the sheep and come peacefully.",
        parsed=ParsedInput(intent="combat", target="the half-orc"),
    )
    result = await _say(
        fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat")
    )
    assert result["combat_state"]["outcome"] == "surrendered"
    assert "Agreed. We stop fighting." in result["narration"]
    assert "Guz" not in result["narration"] and _rolled(result) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    'I lower my sword and surrender to Guz. "Stop! I will come with you to speak to Noke."',
    "I surrender. I do not attack.",
])
async def test_surrender_never_becomes_parser_attack_or_escape(fake_llm_schema, fake_retrieval, text):
    initial = _guz()
    asked = await _say(fake_llm_schema, initial, text, parsed=ParsedInput(intent="combat", check="attack", target="Guz"))
    assert "offer surrender" in asked["narration"]
    assert _rolled(asked) == [] and not is_flee(text)
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat", check="attack"))
    assert "has not been accepted" in final["narration"]
    assert not final["combat_state"]["ended"] and final["combat_state"]["round"] == 1
    assert _rolled(final) == []
    assert final["combat_state"]["combatants"][1]["hp_current"] == 100


@pytest.mark.asyncio
async def test_negated_attack_with_bad_parser_spends_nothing(fake_llm_schema, fake_retrieval):
    initial = _guz()
    out = await _say(fake_llm_schema, initial, "I do not attack Guz.", parsed=ParsedInput(intent="combat", check="attack", target="Guz"))
    assert _rolled(out) == [] and out["combat_state"]["round"] == 1
    assert "have not declared an attack" in out["narration"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message,first", [
    ("First I use Second Wind to heal myself, then I attack Guz with my longsword.", "Second Wind"),
    ("First I attack Guz with my longsword, then I use Second Wind.", "attack Guz"),
])
async def test_confirmation_and_dice_follow_requested_order(fake_llm_schema, fake_retrieval, message, first):
    parsed = ParsedInput(intent="combat", check="attack", target="Guz", actions=[
        {"kind": "attack", "text": "attack Guz", "target": "Guz"},
        {"kind": "second_wind", "text": "Second Wind"},
    ])
    asked = await _say(fake_llm_schema, _guz(), message, parsed=parsed)
    assert asked["narration"].index(first) < asked["narration"].index("then")
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    purposes = [d["purpose"] for d in _rolled(final)]
    assert purposes.index("second wind") < purposes.index("attack check") if first == "Second Wind" else purposes.index("attack check") < purposes.index("second wind")
    kinds = [e["kind"] for e in final["combat_state"]["round_events"]]
    expected = ["second_wind", "player_attack"] if first == "Second Wind" else ["player_attack", "second_wind"]
    assert kinds == expected + ["enemy_attack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,actions", [
    ("I attack Guz twice with my longsword.", [{"kind": "attack", "target": "Guz"}]),
    ("I attack Guz and then attack Guz.", [{"kind": "attack", "target": "Guz"}, {"kind": "attack", "target": "Guz"}]),
])
async def test_requested_attacks_cannot_silently_disappear(fake_llm_schema, fake_retrieval, text, actions):
    out = await _say(fake_llm_schema, _guz(), text, parsed=ParsedInput(intent="combat", check="attack", target="Guz", actions=actions))
    assert "requested 2 attacks" in out["narration"]
    assert _rolled(out) == [] and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_disengage_withdraw_and_regular_enemy_turn_are_distinct(fake_llm_schema, fake_retrieval):
    parsed = ParsedInput(intent="combat", actions=[{"kind": "disengage"}, {"kind": "move", "destination": "away"}])
    asked = await _say(fake_llm_schema, _guz(), "I disengage and back away from Guz.", parsed=parsed)
    assert "Disengage then move" in asked["narration"]
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    kinds = [e["kind"] for e in final["combat_state"]["round_events"]]
    assert kinds == ["disengage", "withdraw", "enemy_close", "enemy_attack"]
    assert final["combat_state"]["combatants"][1]["distance_ft"] == 5
    from agent.nodes.narrator import build_narrator_prompt
    prompt = build_narrator_prompt({**_state([], "yes"), **final}, "play.combat_narrator")
    context = str(prompt[-1].content)
    assert "withdrawal succeeds" in str(prompt[0].content)
    assert context.index("[disengage]") < context.index("[withdraw]") < context.index("[enemy_close]") < context.index("[enemy_attack]")


@pytest.mark.asyncio
async def test_reversing_move_and_disengage_does_provoke(fake_llm_schema, fake_retrieval):
    parsed = ParsedInput(intent="combat", actions=[{"kind": "disengage"}, {"kind": "move", "destination": "away"}])
    asked = await _say(fake_llm_schema, _guz(), "I back away from Guz, then disengage.", parsed=parsed)
    assert "move then Disengage" in asked["narration"]
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    kinds = [e["kind"] for e in final["combat_state"]["round_events"]]
    assert kinds[0] == "opportunity_attack"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,span_text,accepted", [
    ("accepted", "Guz accepts surrender on the promise to speak to Noke.", True),
    ("accepted", "Noke accepts surrender.", False),
    ("uncertain", "Guz might consider surrender.", False),
])
async def test_source_bound_offer_cannot_accept_for_another_actor(fake_llm_schema, fake_retrieval, monkeypatch, decision, span_text, accepted):
    monkeypatch.setattr("agent.combat_offers.observation_span_catalog", lambda s: {"span": {"text": span_text, "reference_id": "reviewed-source"}})
    async def response(*a, **k):
        return AdjudicationResult(success=True, combat_offer=CombatOffer(decision=decision, actor_name="Guz", source_span_id="span", action_quote="I surrender", achievement_basis="The actual terms satisfy Guz's authored offer."))
    monkeypatch.setattr("agent.graph_combat.adjudicate", response)
    asked = await _say(fake_llm_schema, _guz(), "I surrender", parsed=ParsedInput(intent="combat", target="Guz"))
    before = copy.deepcopy(asked["combat_state"])
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert final["combat_state"]["ended"] is accepted
    assert _rolled(final) == [] and final["combat_state"]["round"] == 1
    if accepted:
        assert final["combat_state"]["outcome"] == "surrendered"
        hero = _hero(final["combat_state"])
        assert hero["hp_current"] == _hero(before)["hp_current"]
        assert hero["combat_offer_receipt"]["reference_id"] == "reviewed-source"


@pytest.mark.asyncio
async def test_supported_offer_refusal_spends_action_and_enemy_turn(fake_llm_schema, fake_retrieval, monkeypatch):
    monkeypatch.setattr("agent.combat_offers.observation_span_catalog", lambda s: {"span": {"text": "Guz refuses surrender unless the sheep is handed over.", "reference_id": "source"}})
    async def response(*a, **k):
        return AdjudicationResult(success=True, combat_offer=CombatOffer(decision="rejected", actor_name="Guz", source_span_id="span", action_quote="I surrender", achievement_basis="The sheep has not been offered.", public_response="Hand over the sheep. I will not accept a promise to talk."))
    monkeypatch.setattr("agent.graph_combat.adjudicate", response)
    asked = await _say(fake_llm_schema, _guz(), "I surrender", parsed=ParsedInput(intent="combat", target="Guz"))
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert final["combat_state"]["round"] == 2 and not final["combat_state"]["ended"]
    assert any(e["kind"] == "enemy_attack" for e in final["combat_state"]["round_events"])
    assert all(d["purpose"] != "attack check" for d in _rolled(final))
    assert "Hand over the sheep" in final["adjudication_result"].mechanical_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["accepted", "uncertain"])
async def test_offer_pronoun_condition_uses_bounded_actor_sentence_and_actionable_reply(fake_llm_schema, fake_retrieval, monkeypatch, decision):
    spans = {
        "actor": {"text": "Guz demands the sheep. ", "reference_id": "source", "start": 0, "end": 23},
        "terms": {"text": "He will stop fighting if it is handed over.", "reference_id": "source", "start": 23, "end": 65},
    }
    monkeypatch.setattr("agent.combat_offers.observation_span_catalog", lambda s: spans)
    reply = "Agreed. Lower your sword and we stop fighting." if decision == "accepted" else "Give me the sheep and lower your sword. Are those your terms?"
    async def response(*a, **k):
        return AdjudicationResult(success=True, combat_offer=CombatOffer(
            decision=decision, actor_name="Guz", source_span_id="terms", actor_source_span_id="actor",
            action_quote="I surrender", achievement_basis="The nearby actor sentence binds these terms to Guz.", public_response=reply,
        ))
    monkeypatch.setattr("agent.graph_combat.adjudicate", response)
    asked = await _say(fake_llm_schema, _guz(), "I surrender and offer the sheep", parsed=ParsedInput(intent="combat", target="Guz"))
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert reply in final["narration"]
    assert final["combat_state"]["ended"] is (decision == "accepted")
    assert final["combat_state"]["round"] == 1 and _rolled(final) == []
    assert "captured" not in final["combat_state"]["combatants"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["different_reference", "distant", "intervening_foe"])
async def test_offer_pronoun_cannot_borrow_another_passage_or_foe(fake_llm_schema, fake_retrieval, monkeypatch, mutation):
    from agent.combat_offers import _actor_span
    guz, noke = {"name": "Guz"}, {"name": "Noke"}
    spans = {"actor": {"text": "Guz demands payment.", "reference_id": "source", "start": 0, "end": 20},
             "terms": {"text": "He accepts surrender.", "reference_id": "source", "start": 20, "end": 41}}
    if mutation == "different_reference":
        spans["terms"]["reference_id"] = "another-source"
    elif mutation == "distant":
        spans["terms"]["start"] = 999
    else:
        spans["other"] = {"text": "Noke now speaks.", "reference_id": "source", "start": 20, "end": 36}
        spans["terms"]["start"] = 36
    offer = CombatOffer(decision="accepted", actor_name="Guz", source_span_id="terms", actor_source_span_id="actor", action_quote="I surrender", achievement_basis="Claimed condition.")
    assert not _actor_span(offer, guz, spans, [guz, noke])


@pytest.mark.asyncio
async def test_careful_withdrawal_clarifies_action_and_risk_without_spending_turn(fake_llm_schema, fake_retrieval):
    parsed = ParsedInput(intent="combat", actions=[{"kind": "disengage"}, {"kind": "move", "destination": "away"}])
    out = await _say(fake_llm_schema, _guz(), "I withdraw carefully without attacking.", parsed=parsed)
    assert "use your action to Disengage" in out["narration"]
    assert "risking opportunity attacks" in out["narration"]
    assert out["combat_state"]["round"] == 1 and _rolled(out) == []


def test_visible_participants_require_public_current_scene_evidence():
    state = {"current_scene_id": "street", "adventure_context": [{"text": "Guz arrives with an iron-collared wolf.", "audience": "player", "scene_ids": ["street"]}], "discovery_entities": [{"kind": "actor", "title": "Guz"}, {"kind": "actor", "title": "iron-collared wolf"}, {"kind": "actor", "title": "Secret dragon"}]}
    assert visible_scene_actors(state) == ["Guz", "iron-collared wolf"]


@pytest.mark.asyncio
async def test_visible_nonparticipant_target_refusal_preserves_turn(fake_llm_schema, fake_retrieval):
    combat = _guz()
    _hero(combat)["encounter_participants"] = [{"id": "wolf", "name": "iron-collared wolf", "participation": "visible_nonparticipant", "reason": "Outside roster."}]
    out = await _say(fake_llm_schema, combat, "I attack the iron-collared wolf.", parsed=ParsedInput(intent="combat", check="attack", target="iron-collared wolf"))
    assert "visible" in out["narration"] and "roster" in out["narration"]
    assert _rolled(out) == [] and out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_severe_encounter_requires_confirmation_before_initiative(fake_llm_schema, fake_retrieval, monkeypatch):
    import agent.graph_combat as graph
    calls = []
    original = graph.roll_initiative
    def initiative(*a, **k):
        calls.append(True)
        return original(*a, **k)
    async def roster(state, targets, *, budget_summary, **kwargs):
        budget_summary.update(warning={"text": "Severe danger: Guz far exceeds this hero's budget."})
        return [_enemy("Guz", hp=100, ac=1)]
    monkeypatch.setattr(graph, "_build_enemy_roster", roster)
    monkeypatch.setattr(graph, "roll_initiative", initiative)
    state = _state([], "I attack Guz", parsed_input=ParsedInput(intent="combat", check="attack", target="Guz"))
    asked = await combat_round_node(state)
    assert not calls and _rolled(asked) == [] and "Severe danger" in asked["narration"]
    cancelled = await _say(fake_llm_schema, asked["combat_state"], "no", parsed=ParsedInput(intent="combat"))
    assert not calls and _rolled(cancelled) == []
    assert cancelled["combat_state"]["ended"]
    assert cancelled["combat_state"]["outcome"] == "cancelled"
    asked = await _say(fake_llm_schema, json.loads(json.dumps(cancelled["combat_state"])), "I attack Guz", parsed=ParsedInput(intent="combat", check="attack", target="Guz"))
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert calls == [True] and _rolled(final)
    assert PENDING_KEY not in _hero(final["combat_state"])


@pytest.mark.asyncio
async def test_changed_normalized_plan_is_reconfirmed_after_reload(fake_llm_schema, fake_retrieval):
    parsed = ParsedInput(intent="combat", check="attack", target="Guz")
    asked = await _say(fake_llm_schema, _guz(), "I use Second Wind then attack Guz", parsed=parsed)
    pending = _hero(asked["combat_state"])[PENDING_KEY]
    assert pending["execution_plan"]["version"] == 1
    pending["execution_plan"]["target_id"] = "A different foe"
    out = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert "encounter changed" in out["narration"]
    assert _rolled(out) == [] and out["combat_state"]["round"] == 1


@pytest.mark.parametrize("text", [
    "I speak to Noke: I have changed my mind about attacking. I want to leave peacefully.",
    "I decide against attacking Noke and lower my sword.",
    "I abandon the plan to attack the guard.",
    "I discuss attacking Guz with Noke.",
    "I remember attacking the guard yesterday.",
    "I no longer attack Guz.",
    "I have stopped attacking Guz.",
])
def test_referenced_or_revoked_action_is_not_authorization(text):
    assert not has_attack_words(text)


@pytest.mark.parametrize("text", [
    "I changed my mind about attacking. I attack Guz now.",
    "I will attack Guz.",
    "I attack Guz, then use Second Wind.",
])
def test_new_explicit_attack_clause_still_authorizes_attack(text):
    assert has_attack_words(text)


@pytest.mark.asyncio
async def test_revoked_attack_cannot_create_confirmation(fake_llm_schema, fake_retrieval):
    text = "I speak to Noke: I have changed my mind about attacking. I want to leave peacefully."
    out = await _say(fake_llm_schema, _guz(), text, parsed=ParsedInput(
        intent="combat", check="attack", target="Guz", actions=[{"kind": "attack", "target": "Guz"}]
    ))
    assert _rolled(out) == []
    assert PENDING_KEY not in _hero(out["combat_state"])
    assert out["combat_state"]["round"] == 1


@pytest.mark.asyncio
async def test_cancel_legacy_unstarted_shell_without_pending_order(fake_llm_schema, fake_retrieval):
    combat = _guz()
    for row in combat["combatants"]:
        row.pop("init", None)
    _hero(combat)["initiative_pending"] = True
    out = await _say(fake_llm_schema, combat, "cancel", parsed=ParsedInput(intent="combat"))
    assert out["combat_state"]["ended"]
    assert out["combat_state"]["outcome"] == "cancelled"
    assert _rolled(out) == []
    assert out["combat_state"]["round"] == 1
    from agent.graph import route_after_input_parser
    assert route_after_input_parser({**out, "parsed_input": ParsedInput(intent="exploration")}) == "normal"


@pytest.mark.asyncio
async def test_back_into_melee_is_move_before_attack(fake_llm_schema, fake_retrieval):
    combat = _guz()
    foe = next(row for row in combat["combatants"] if not row.get("is_player"))
    foe.update(band="ranged", distance_ft=30)
    text = "I move back into melee with Guz and attack him with my longsword."
    parsed = ParsedInput(intent="combat", check="attack", target="Guz", actions=[
        {"kind": "move", "destination": "melee with Guz"},
        {"kind": "attack", "target": "Guz"},
    ])
    asked = await _say(fake_llm_schema, combat, text, parsed=parsed)
    assert "move then attack Guz" in asked["narration"]
    assert _rolled(asked) == []
    final = await _say(fake_llm_schema, asked["combat_state"], "yes", parsed=ParsedInput(intent="combat"))
    assert any(d["purpose"] == "attack check" for d in _rolled(final))
    events = [event["kind"] for event in final["combat_state"]["round_events"]]
    assert events.index("close") < events.index("player_attack")
