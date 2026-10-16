"""Prepared edition/hero boundaries preserve sheets while normal play spends resources."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_preparation_pipeline import _binding

from apps.api.services import adventures_meta, preparation_runtime, prepared_play
from apps.api.services.preparation_store import PreparationStore


@pytest.fixture
def prepared(monkeypatch):
    binding = _binding()
    monkeypatch.setattr(
        adventures_meta,
        "load_meta",
        lambda _: {
            "private_preparation": True,
            "prepared_bundle": {"run_id": binding.run_id},
            "preparation_run_id": binding.run_id,
        },
    )
    monkeypatch.setattr(preparation_runtime, "private_preparation_ready", lambda *_: True)
    monkeypatch.setattr(PreparationStore, "load_run", lambda *_: SimpleNamespace(binding=binding))
    return binding.hero_party_snapshot["heroes"][0]


def test_prepared_play_requires_commissioned_edition_and_hero(prepared):
    with pytest.raises(HTTPException) as edition:
        prepared_play.require_prepared_play(["prepared"], "2024")
    assert edition.value.status_code == 422
    for wrong in (None, prepared["character_id"] + 1):
        with pytest.raises(HTTPException) as hero:
            prepared_play.require_prepared_play(
                ["prepared"], "2014", character_id=wrong, require_hero=True
            )
        assert hero.value.status_code == 422
    prepared_play.require_prepared_play(
        ["prepared"], "2014", character_id=prepared["character_id"], require_hero=True
    )


def test_initial_assignment_checks_exact_sheet_but_play_does_not_reset_resources(prepared):
    hero = SimpleNamespace(
        id=prepared["character_id"],
        name=prepared["name"],
        character_class=prepared["character_class"],
        level=prepared["level"],
        hp_max=prepared["hp_max"],
        base_stats=deepcopy(prepared["stats"]),
        base_inventory=deepcopy(prepared["inventory"]),
    )
    prepared_play.require_prepared_play(["prepared"], "2014", character=hero)
    hero.level += 1
    with pytest.raises(HTTPException) as changed:
        prepared_play.require_prepared_play(["prepared"], "2014", character=hero)
    assert changed.value.status_code == 409
    # Normal play checks identity only; current HP/slots/inventory evolve in the assignment.
    prepared_play.require_prepared_play(
        ["prepared"], "2014", character_id=hero.id, require_hero=True
    )


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


@pytest.mark.asyncio
async def test_private_generator_dispatches_before_legacy_model_or_index(monkeypatch):
    from apps.api.services import campaign_generator

    calls = []
    monkeypatch.setattr(adventures_meta, "load_meta", lambda _: {"private_preparation": True})

    async def prepare(slug, **kwargs):
        calls.append((slug, kwargs))

    monkeypatch.setattr(preparation_runtime, "prepare_adventure", prepare)
    monkeypatch.setattr(
        adventures_meta, "begin_attempt", lambda *_a, **_k: pytest.fail("legacy attempt")
    )
    await campaign_generator.build_generated_campaign(
        "private", "Title", "Theme", "gigantic", attempt_id="a" * 32
    )
    assert calls == [("private", {"attempt_token": "a" * 32, "model_profiles": None})]


@pytest.mark.parametrize("change", ["party", "edition", "progression", "spell"])
def test_unsupported_commissions_fail_before_provider(change):
    from apps.api.services.preparation_stages import _validate_commission
    from edgar_core.preparation import canonical_sha256

    binding = _binding()
    values = binding.model_dump(mode="json")
    if change == "party":
        second = deepcopy(values["hero_party_snapshot"]["heroes"][0])
        second["character_id"] += 1
        values["hero_party_snapshot"]["heroes"].append(second)
    elif change == "edition":
        values["rules_edition"] = "2024"
    elif change == "progression":
        values["commission"] = {"setup": {"target_level": 20}}
    else:
        values["hero_party_snapshot"]["heroes"][0]["stats"]["known_spells"] = ["Teleport"]
    values["hero_party_sha256"] = canonical_sha256(values["hero_party_snapshot"])
    values["commission_sha256"] = canonical_sha256(values["commission"])
    with pytest.raises(ValueError):
        _validate_commission(type(binding).model_validate(values))
