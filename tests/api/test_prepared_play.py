"""Reusable preparation binds source and edition; a hero is chosen for later play."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from apps.api.services import adventures_meta, prepared_play
from apps.api.services.campaign_prep import runtime


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(
        adventures_meta,
        "load_meta",
        lambda _: {
            "private_preparation": True,
            "rules_edition": "2014",
            "preparation_workflow": "campaign-graph-2.0",
            "prepared_bundle": {"run_id": "campaign-graph-2.0"},
        },
    )
    monkeypatch.setattr(runtime, "private_preparation_ready", lambda *_: True)


def test_prepared_play_binds_edition_and_accepts_hero_chosen_later(prepared):
    with pytest.raises(HTTPException) as edition:
        prepared_play.require_prepared_play(["prepared"], "2024")
    assert edition.value.status_code == 422
    prepared_play.require_prepared_play(["prepared"], "2014")
    with pytest.raises(HTTPException) as hero:
        prepared_play.require_prepared_play(["prepared"], "2014", require_hero=True)
    assert hero.value.status_code == 422
    for chosen_id in (7, 22):
        prepared_play.require_prepared_play(
            ["prepared"], "2014", character_id=chosen_id, require_hero=True
        )


def test_character_sheet_and_progression_do_not_rebind_preparation(prepared):
    hero = SimpleNamespace(id=7, level=1, hp_max=12, base_inventory={})
    prepared_play.require_prepared_play(["prepared"], "2014", character=hero)
    hero.level = 5
    hero.hp_max = 42
    hero.base_inventory = {"lantern": 1}
    prepared_play.require_prepared_play(["prepared"], "2014", character=hero)
    prepared_play.require_prepared_play(
        ["prepared"], "2014", character=SimpleNamespace(id=22), require_hero=True
    )


def test_unready_campaign_refuses_play_before_hero_selection(prepared, monkeypatch):
    monkeypatch.setattr(runtime, "private_preparation_ready", lambda *_: False)
    with pytest.raises(prepared_play.PreparedPlayError) as error:
        prepared_play.require_prepared_play(["prepared"], "2014", character_id=7)
    assert error.value.status_code == 409
    assert error.value.readiness_code == "preparation_in_progress"


def test_saved_prepared_campaign_cannot_rebind_source_or_edition(prepared):
    campaign = SimpleNamespace(
        adventure_collections=["prepared"], rules_edition="2014", source_mode="module"
    )
    prepared_play.protect_prepared_campaign_binding(campaign, {"title": "Renamed"})
    for changes in (
        {"adventure_collections": ["replacement"]},
        {"rules_edition": "2024"},
        {"source_mode": "homebrew"},
    ):
        with pytest.raises(HTTPException) as blocked:
            prepared_play.protect_prepared_campaign_binding(campaign, changes)
        assert blocked.value.status_code == 409
