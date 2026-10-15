"""EDG-022/023: combatant disposition (dead, unconscious, captured) and the acting-enemy contract."""

import random

import pytest

from agent.combatants import (
    apply_defeat,
    can_act,
    declared_lethality,
    disposition_of,
    finish_victory,
    is_living,
    narrator_roster,
    roster_violations,
)
from agent.graph_combat import EncounterSetup, combat_round_node, initiative_view
from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes.narrator import build_narrator_prompt
from apps.api.services.combat import persist_combat_npcs
from apps.api.services.turn_runner import combat_state_public
from tests.agent.test_combat import _enemy, _player, _state
from tools.dice import use_rng

# --- helper module: the single source of truth --------------------------------------------


@pytest.mark.parametrize(
    ("combatant", "disposition", "acts", "living"),
    [
        ({"alive": True, "hp_current": 5}, "active", True, True),
        ({"alive": True, "hp_current": 0}, "defeated", False, False),
        # Legacy rows only carry `alive`: False is "defeated", never proof of death or capture.
        ({"alive": False, "hp_current": 0}, "defeated", False, False),
        ({"alive": False, "hp_current": 7}, "defeated", False, False),
        (
            {"disposition": "unconscious", "alive": True, "hp_current": 0},
            "unconscious",
            False,
            True,
        ),
        ({"disposition": "captured", "alive": True, "hp_current": 0}, "captured", False, True),
        ({"disposition": "dead", "alive": False, "hp_current": 0}, "dead", False, False),
    ],
)
def test_legacy_and_stored_disposition_are_read_honestly(
    combatant, disposition, acts, living
) -> None:
    assert disposition_of(combatant) == disposition
    assert can_act(combatant) is acts
    assert is_living(combatant) is living


@pytest.mark.parametrize(
    ("lethality", "disposition", "alive"),
    [("lethal", "dead", False), ("nonlethal", "unconscious", True), (None, "defeated", False)],
)
def test_defeat_records_declared_intent_only(lethality, disposition, alive) -> None:
    goblin = _enemy("Goblin", hp=3)
    apply_defeat(goblin, lethality)
    assert goblin["hp_current"] == 0
    assert goblin["disposition"] == disposition
    assert goblin["alive"] is alive
    assert not can_act(goblin)


def test_victory_turns_knocked_out_foes_into_living_captives_only() -> None:
    knocked = {**_enemy("Bandit", hp=1), "disposition": "unconscious", "alive": True}
    killed = {**_enemy("Wolf", hp=1), "disposition": "dead", "alive": False}
    unknown = {**_enemy("Rat", hp=1), "disposition": "defeated", "alive": False}
    finish_victory([_player(), knocked, killed, unknown])
    assert (knocked["disposition"], knocked["alive"]) == ("captured", True)
    assert killed["disposition"] == "dead" and unknown["disposition"] == "defeated"


@pytest.mark.parametrize(
    ("parsed", "text", "expected"),
    [
        (ParsedInput(lethality="nonlethal"), "I attack", "nonlethal"),
        (ParsedInput(lethality="lethal"), "I knock him out", "lethal"),
        (ParsedInput(), "I try to knock the goblin out", "nonlethal"),
        (ParsedInput(), "I subdue him and take him prisoner", "nonlethal"),
        (ParsedInput(), "I don't want to kill him, I strike", "nonlethal"),
        (ParsedInput(), "I slay the goblin", "lethal"),
        (ParsedInput(), "I attack the goblin", None),
        (None, "", None),
    ],
)
def test_declared_lethality_comes_from_the_player(parsed, text, expected) -> None:
    assert declared_lethality(parsed, text) == expected


def test_parser_lethality_is_normalised_and_never_guessed() -> None:
    assert ParsedInput(lethality="Non-Lethal").lethality == "nonlethal"
    assert ParsedInput(lethality="kill").lethality == "lethal"
    assert ParsedInput(lethality="maybe").lethality is None


def _roster() -> list[dict]:
    raider = {**_enemy("Raider", hp=1), "disposition": "dead", "alive": False, "hp_current": 0}
    captain = _enemy("Raider Captain", hp=20)
    return [_player(), raider, captain]


def test_roster_check_flags_revived_and_invented_attackers_but_not_real_ones() -> None:
    roster = _roster()
    assert roster_violations("Raider Captain swings at Eda and misses.", roster) == []
    assert roster_violations("Raider swings at Eda.", roster) == ["Raider"]
    assert "other raider" in roster_violations("The other raider lunges at you.", roster)
    assert "second raider" in roster_violations("A second raider stabs Eda.", roster)


def test_roster_check_allows_two_living_similar_foes() -> None:
    raiders = [_enemy("Raider", hp=5), _enemy("Raider 2", hp=5), _player()]
    assert roster_violations("The other raider lunges at you.", raiders) == []


def test_roster_text_names_only_the_able_actor() -> None:
    text = narrator_roster(_roster(), ["Raider Captain"])
    assert "Combatants able to act now: Raider Captain." in text
    assert "Enemy attacks this round were made only by: Raider Captain." in text
    assert 'name "Raider": dead, cannot act' in text


# --- combat round: the engine decides, the narrator must agree -----------------------------


async def _round(fake, state: dict) -> dict:
    state.setdefault("parsed_input", fake.responses.get(ParsedInput))
    return await combat_round_node(state)


def _attack_purposes(out: dict) -> list[str]:
    return [d["purpose"] for d in out["adjudication_result"].dice_log]


@pytest.mark.asyncio
async def test_nonlethal_knockout_is_living_and_cannot_attack(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="bandit", lethality="nonlethal"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    for seed in range(60):
        state = _state(
            [
                _player(init=20, ac=99),
                _enemy("Bandit", hp=1, init=10),
                _enemy("Wolf", hp=30, init=5),
            ],
            "I knock the bandit out",
        )
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        bandit = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Bandit")
        if bandit["hp_current"] == 0:
            break
    else:
        pytest.fail("no seed produced a hit")
    assert bandit["disposition"] == "unconscious" and bandit["alive"] is True
    assert out["combat_state"]["outcome"] is None  # the wolf still stands
    assert "Bandit attack" not in _attack_purposes(out)  # knocked out before its turn
    view = {c["name"]: c for c in out["combat_state"]["initiative"]}
    assert view["Bandit"]["disposition"] == "unconscious" and view["Bandit"]["can_act"] is False
    assert view["Bandit"]["alive"] is True

    # Next round, the captive still never attacks.
    follow = _state(out["combat_state"]["combatants"], "I attack the wolf")
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="wolf"
    )
    with use_rng(random.Random(3)):
        second = await _round(fake_llm_schema, follow)
    assert "Bandit attack" not in _attack_purposes(second)
    assert "Wolf attack" in _attack_purposes(second)


@pytest.mark.asyncio
async def test_lethal_kill_is_dead_and_not_captured_at_victory(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin", lethality="lethal"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    for seed in range(60):
        state = _state(
            [_player(init=20, ac=99), _enemy("Goblin", hp=1, init=1)], "I kill the goblin"
        )
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        if out["combat_state"]["outcome"] == "victory":
            break
    else:
        pytest.fail("no seed produced a kill")
    goblin = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Goblin")
    assert (goblin["disposition"], goblin["alive"]) == ("dead", False)


@pytest.mark.asyncio
async def test_undeclared_defeat_is_neither_dead_nor_captive(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="goblin"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=True)
    for seed in range(60):
        state = _state(
            [_player(init=20, ac=99), _enemy("Goblin", hp=1, init=1)], "I attack the goblin"
        )
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        if out["combat_state"]["outcome"] == "victory":
            break
    else:
        pytest.fail("no seed produced a kill")
    goblin = next(c for c in out["combat_state"]["combatants"] if c["name"] == "Goblin")
    assert (goblin["disposition"], goblin["alive"]) == ("defeated", False)


@pytest.mark.asyncio
async def test_legacy_fallen_foe_is_not_an_attacker_and_is_not_claimed_dead(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="captain"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    legacy = {**_enemy("Raider", hp=1), "alive": False, "hp_current": 0}
    legacy.pop("disposition", None)
    state = _state(
        [_player(init=20, ac=99), legacy, _enemy("Raider Captain", init=5)], "I strike the captain"
    )
    with use_rng(random.Random(5)):
        out = await _round(fake_llm_schema, state)
    assert "Raider attack" not in _attack_purposes(out)
    prompt = build_narrator_prompt(
        {
            **state,
            "combat_state": out["combat_state"],
            "adjudication_result": out["adjudication_result"],
        }
    )[1].content
    assert 'name "Raider": defeated, cannot act' in prompt
    assert "dead" not in prompt.split("Combat roster")[1].split("\n")[1]


@pytest.mark.asyncio
async def test_narration_context_names_one_surviving_attacker(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="raider"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    for seed in range(60):
        state = _state(
            [
                _player(init=20, ac=99),
                _enemy("Raider", hp=1, init=10),
                _enemy("Raider Captain", hp=20, init=5),
            ],
            "I strike the raider",
        )
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        if out["combat_state"]["combatants"][1]["hp_current"] == 0 or any(
            c["name"] == "Raider" and c["hp_current"] == 0
            for c in out["combat_state"]["combatants"]
        ):
            break
    st = {
        **state,
        "combat_state": out["combat_state"],
        "adjudication_result": out["adjudication_result"],
    }
    prompt = build_narrator_prompt(st)[1].content
    assert "Combatants able to act now: Raider Captain." in prompt
    assert "made only by: Raider Captain." in prompt
    assert 'name "Raider": dead' not in prompt  # undeclared -> defeated, never narrated as dead
    assert 'name "Raider": defeated, cannot act' in prompt


@pytest.mark.asyncio
async def test_narration_that_invents_an_attacker_is_replaced_by_the_engine_log(
    fake_llm_schema, fake_retrieval
) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="raider"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    fake_llm_schema.narration = (
        "You strike the raider down. The other raider lunges at you from the dark."
    )
    state = _state(
        [
            _player(init=20, ac=99),
            _enemy("Raider", hp=1, init=10),
            _enemy("Raider Captain", hp=20, init=5),
        ],
        "I strike the raider",
    )
    for seed in range(60):
        state = _state(
            [
                _player(init=20, ac=99),
                _enemy("Raider", hp=1, init=10),
                _enemy("Raider Captain", hp=20, init=5),
            ],
            "I strike the raider",
        )
        with use_rng(random.Random(seed)):
            out = await _round(fake_llm_schema, state)
        if out["combat_state"]["combatants"][1]["hp_current"] == 0:
            break
    assert "other raider" not in out["narration"]
    assert out["narration"] == out["adjudication_result"].mechanical_summary
    assert out["messages"][-1].content == out["narration"]


@pytest.mark.asyncio
async def test_two_similar_names_stay_distinct_in_roster(fake_llm_schema, fake_retrieval) -> None:
    fake_llm_schema.responses[ParsedInput] = ParsedInput(
        intent="combat", check="attack", target="raider"
    )
    fake_llm_schema.responses[AdjudicationResult] = AdjudicationResult(success=False)
    state = _state(
        [
            _player(init=20, ac=99),
            _enemy("Raider", hp=8, init=10),
            _enemy("Raider Captain", hp=20, init=5),
        ],
        "I strike the raider",
    )
    with use_rng(random.Random(2)):
        out = await _round(fake_llm_schema, state)
    text = narrator_roster(out["combat_state"]["combatants"], out["combat_state"]["round_actors"])
    assert 'name "Raider": active' in text and 'name "Raider Captain": active' in text


# --- persistence and payload agreement ----------------------------------------------------


class _FakeDB:
    """The caller-owned session surface that persist_combat_npcs uses: no commit, no rollback."""

    def __init__(self) -> None:
        self.rows: list = []
        self.committed = False

    async def scalar(self, stmt):
        wanted = {c.left.key: c.right.value for c in stmt._where_criteria}  # noqa: SLF001
        for row in self.rows:
            if row.session_id == wanted.get("session_id") and row.story_identity == wanted.get(
                "story_identity"
            ):
                return row
        return None

    def add(self, row) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:  # pragma: no cover - must never be called here
        self.committed = True


@pytest.mark.asyncio
async def test_post_combat_npc_records_captive_and_dead_but_not_unknown() -> None:
    ended = {
        "ended": True,
        "outcome": "victory",
        "combatants": [
            _player(),
            {**_enemy("Bandit", hp=1), "disposition": "captured", "alive": True},
            {**_enemy("Wolf", hp=1), "disposition": "dead", "alive": False},
            {**_enemy("Rat", hp=1), "disposition": "defeated", "alive": False},
        ],
    }
    db = _FakeDB()
    await persist_combat_npcs(db, 1, 1, 42, ended)
    by_name = {row.name: row for row in db.rows}
    assert set(by_name) == {"Bandit", "Wolf"}
    assert by_name["Bandit"].disposition == "captive"
    assert by_name["Bandit"].stat_block["life_state"] == "living"
    assert by_name["Wolf"].disposition == "dead"
    assert by_name["Wolf"].stat_block["life_state"] == "dead"
    # Idempotent: a second write updates the same encounter-owned row.
    await persist_combat_npcs(db, 1, 1, 42, ended)
    assert len(db.rows) == 2 and db.committed is False


def test_combat_payload_and_reload_share_disposition() -> None:
    combatants = [
        _player(),
        {
            **_enemy("Bandit", hp=1),
            "disposition": "captured",
            "alive": True,
            "hp_current": 0,
            "init": 10,
        },
        {**_enemy("Wolf", hp=20), "init": 5},
    ]
    state = {
        "round": 2,
        "ended": True,
        "outcome": "victory",
        "combatants": combatants,
        "initiative": initiative_view(combatants),
        "current_turn": None,
        "current_turn_index": 0,
    }
    public = combat_state_public(state)
    row = next(c for c in public["combatants"] if c["name"] == "Bandit")
    assert row["disposition"] == "captured" and row["can_act"] is False and row["alive"] is True
    initiative = {c["name"]: c for c in public["initiative"]}
    assert initiative["Bandit"]["disposition"] == "captured"
    assert initiative["Wolf"]["disposition"] == "active" and initiative["Wolf"]["can_act"] is True


@pytest.mark.asyncio
async def test_captive_from_an_earlier_fight_never_rejoins_as_an_attacker(
    fake_llm_schema, fake_retrieval
) -> None:
    from agent.graph_combat import _build_enemy_roster

    state = _state(None, "I attack the bandit", npcs=[{"name": "Bandit", "disposition": "captive"}])
    assert await _build_enemy_roster(state, ["Bandit"]) == []
    assert not any(schema is EncounterSetup for schema, _ in fake_llm_schema.calls)


def test_captive_is_known_to_the_narrator_as_a_person_not_an_attacker() -> None:
    state = {
        "player_input": "I talk to the bandit",
        "npcs": [{"name": "Bandit", "disposition": "captive"}],
        "combat_state": {},
    }
    prompt = build_narrator_prompt(state)[1].content
    assert "Bandit: captive" in prompt
    assert "Combatants able to act now" not in prompt


_ROSTER = [
    {"name": "Raider", "is_player": False, "disposition": "dead", "hp_current": 0},
    {"name": "Raider Captain", "is_player": False, "disposition": "active", "hp_current": 9},
]


@pytest.mark.parametrize(
    "prose",
    [
        "The Raider staggers as your blade cuts through his guard. The Raider Captain lunges.",
        "The Raider drops and the Captain strikes you.",
    ],
)
def test_narrating_the_felling_blow_is_not_a_revival(prose):
    assert roster_violations(prose, _ROSTER) == []


@pytest.mark.parametrize(
    "prose",
    ["The Raider rises and strikes you across the shoulder.", "The Raider swings his axe."],
)
def test_an_inactive_foe_as_the_attack_subject_is_flagged(prose):
    assert "Raider" in roster_violations(prose, _ROSTER)
