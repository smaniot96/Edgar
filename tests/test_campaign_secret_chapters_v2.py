"""Early secret membership validation and replay of a retained paid recovery."""

# Imported pytest fixtures intentionally share names with their test parameters.
# ruff: noqa: F811

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_campaign_chapter_calls_v2 import chapter_fixture, pdf_store  # noqa: F401
from test_campaign_graph_v2 import ORIGIN
from test_campaign_structural_repair_v2 import broken, recovery, sparse_patch  # noqa: F401

from apps.api.services.campaign_prep import structural_repair
from apps.api.services.campaign_prep.compile import validate_semantics
from apps.api.services.campaign_prep.graph import Workflow
from apps.api.services.campaign_prep.secret_references import (
    SecretChapterJoins,
    chapter_reference_defects,
    reconcile_secret_chapters,
)
from apps.api.services.campaign_prep.store import NeedsAttention
from apps.api.services.campaign_prep.structural_repair import (
    StructuralJoins,
    StructuralLinks,
    repair_structure,
)


def secret(*, invalid=True):
    return {
        "id": "wand_instability",
        "truth": "The damaged wand can fail.",
        "known_by": [],
        "setup_chapters": ["ch_bell", "ch_after"],
        "reveal_chapters": ["ch_after", "wand_and_aftermath"] if invalid else ["ch_after"],
        "reveal_condition": "Examine the damage or question its owner.",
        "origin": ORIGIN,
    }


def correction(owner="spine", *, chapter_ids=None, key="reveal_chapters"):
    return {
        "joins": [
            {
                "owner": owner,
                "index": 0,
                "expected_id": "wand_instability",
                "key": key,
                "chapter_ids": chapter_ids if chapter_ids is not None else ["ch_after"],
                "rationale": "The aftermath is an event in the compound chapter, not a chapter.",
            }
        ]
    }


def secret_call(workflow, result):
    calls = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        assert prompt == "secret-chapter-joins"
        assert schema is SecretChapterJoins
        calls.append(deepcopy(evidence))
        return schema.model_validate(result)

    workflow.semantic = SimpleNamespace(call=call)
    return calls


@pytest.mark.asyncio
async def test_valid_secret_membership_avoids_extra_call(recovery):
    spine, bundles = recovery.store.get("spine"), recovery.store.get("bundles")
    spine["secrets"] = [secret(invalid=False)]
    calls = secret_call(recovery, {})
    assert await reconcile_secret_chapters(recovery, spine, bundles) == (spine, bundles)
    assert calls == []


@pytest.mark.asyncio
async def test_new_spine_corrects_chapter_membership_before_chapter_authoring(pdf_store):
    spine, _ = chapter_fixture()
    spine["secrets"] = [secret()]
    calls = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        calls.append((prompt, evidence))
        return schema.model_validate(spine if prompt == "spine" else correction())

    flow = Workflow(pdf_store, semantic=SimpleNamespace(call=call))
    await flow.spine({"slug": pdf_store.slug})
    accepted = pdf_store.get("spine")
    assert [name for name, _ in calls] == ["spine", "secret-chapter-joins"]
    assert accepted["secrets"][0]["reveal_chapters"] == ["ch_after"]
    assert accepted["secrets"][0]["setup_chapters"] == ["ch_bell", "ch_after"]
    assert pdf_store.get("bundles") is None
    assert not chapter_reference_defects(accepted)
    expected = deepcopy(spine)
    expected["secrets"][0]["reveal_chapters"] = ["ch_after"]
    assert accepted == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["spine", "ch_bell"])
async def test_exact_shared_or_local_secret_join_retains_prose_and_valid_joins(recovery, owner):
    spine, bundles = recovery.store.get("spine"), recovery.store.get("bundles")
    records = spine["secrets"] if owner == "spine" else bundles["chapters"][0]["new_secrets"]
    records.append(secret())
    original_spine, original_bundles = deepcopy(spine), deepcopy(bundles)
    calls = secret_call(recovery, correction(owner))
    fixed, chapters = await reconcile_secret_chapters(recovery, spine, bundles)
    assert len(calls) == 1
    assert not chapter_reference_defects(fixed, chapters)
    assert spine == original_spine and bundles == original_bundles
    expected = deepcopy(original_spine if owner == "spine" else original_bundles)
    target = expected["secrets"] if owner == "spine" else expected["chapters"][0]["new_secrets"]
    target[0]["reveal_chapters"] = ["ch_after"]
    assert (fixed if owner == "spine" else chapters) == expected
    assert (chapters if owner == "spine" else fixed) == (
        original_bundles if owner == "spine" else original_spine
    )
    # Retained response can be re-applied after interruption without paying twice.
    assert await reconcile_secret_chapters(recovery, spine, bundles) == (fixed, chapters)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        correction(chapter_ids=["fabricated_chapter"]),
        correction(chapter_ids=["ch_bell"]),  # would discard the valid original ch_after
        correction(key="setup_chapters"),  # outside the diagnosed list
        {"joins": []},
    ],
)
async def test_bad_correction_is_retained_and_never_resubmitted(recovery, bad):
    spine, bundles = recovery.store.get("spine"), recovery.store.get("bundles")
    spine["secrets"] = [secret()]
    original = deepcopy(spine)
    calls = secret_call(recovery, bad)
    with pytest.raises(NeedsAttention, match="rejected"):
        await reconcile_secret_chapters(recovery, spine, bundles)
    with pytest.raises(NeedsAttention, match="Retained secret"):
        await reconcile_secret_chapters(recovery, spine, bundles)
    assert spine == original
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_old_rejected_checkpoint_reuses_both_paid_candidates_then_corrects_secret(
    recovery,
    monkeypatch,
):
    original = broken(recovery)
    original["chapters"][0]["events"][0]["clue_ids"] = ["unknown_clue"]
    recovery.result["chapters"][0]["events"][0]["clue_ids"] = ["unknown_clue"]
    recovery.store.put("bundles", original)
    spine = recovery.store.get("spine")
    spine["secrets"] = [secret()]
    recovery.store.put("spine", spine)
    calls = []

    async def call(node, prompt, schema, evidence, images, **kwargs):
        calls.append(prompt)
        if schema is StructuralJoins:
            value = sparse_patch(evidence, recovery.result)
        elif schema is StructuralLinks:
            value = {
                "rows": [
                    {
                        "chapter_id": "ch_bell",
                        "field": "events",
                        "index": 0,
                        "expected_id": "event_square_arrival",
                        "changes": [{"key": "clue_ids", "value": []}],
                        "actors": [],
                        "reveals": [],
                    }
                ],
                "items": [],
                "new_entities": [],
            }
        else:
            assert schema is SecretChapterJoins
            value = correction()
        return schema.model_validate(value)

    recovery.semantic = SimpleNamespace(call=call)
    original_helper = structural_repair.reconcile_secret_chapters

    async def old_policy(flow, current, candidate):
        return current, candidate

    # Reproduce the prior release: successful paid sparse/reference joins followed
    # by failure only at secret membership validation. All writes are in tmp_path.
    monkeypatch.setattr(structural_repair, "reconcile_secret_chapters", old_policy)
    with pytest.raises(NeedsAttention, match="Secret chapter references"):
        await repair_structure(recovery)
    assert calls == ["structural-joins", "structural-links"]
    with recovery.store.db() as db:
        key = db.execute(
            "SELECT name FROM artifact WHERE name LIKE 'structural-repair-%'"
        ).fetchone()[0]
    retained = recovery.store.get(key)
    retained.pop("secret_reference_policy")
    recovery.store.put(key, retained)
    assert retained["sparse_candidate"] and retained["reference_candidate"]
    assert recovery.store.get("bundles") == original
    monkeypatch.setattr(structural_repair, "reconcile_secret_chapters", original_helper)
    assert await repair_structure(recovery)
    assert calls == ["structural-joins", "structural-links", "secret-chapter-joins"]
    accepted_spine, accepted = recovery.store.get("spine"), recovery.store.get("bundles")
    validate_semantics(accepted_spine, accepted, recovery.store.get("source"))
    assert accepted_spine["secrets"][0]["reveal_chapters"] == ["ch_after"]
    assert accepted["chapters"][0]["source_notes"] == original["chapters"][0]["source_notes"]
    assert (
        accepted["chapters"][0]["events"][0]["encounters"]
        == original["chapters"][0]["events"][0]["encounters"]
    )
    assert accepted["chapters"][1] == original["chapters"][1]
    assert not await repair_structure(recovery)
    assert len(calls) == 3
