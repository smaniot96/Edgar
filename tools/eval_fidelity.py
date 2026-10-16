"""Offline real-source retrieval and applied-ruling regression evaluation (EDG-054).

This harness uses pinned source excerpts and disposable local Qdrant collections. It invokes the
production exact-heading/creature/scene retrieval paths and deterministic engine helpers; it never
loads credentials or calls an embedding/model provider. Its relevance score is exact/lexical fixture
relevance, not semantic-vector recall.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "tests" / "fixtures" / "fidelity" / "corpus.json"
PROMPT_PATH = ROOT / "apps" / "agent" / "src" / "agent" / "prompts.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qdrant_client import QdrantClient  # noqa: E402
from qdrant_client.models import Distance, PointStruct, VectorParams  # noqa: E402

from agent.monster_stats import attack_plan, parse_monster_stats, regenerate  # noqa: E402
from agent.resolution import (  # noqa: E402
    declares_second_wind,
    resolve_check,
    spend_second_wind,
)
from agent.spells import cast_request, resolve_spell  # noqa: E402
from db.vector import retrieval  # noqa: E402
from db.vector.collections import invalidate_collection_cache  # noqa: E402
from db.vector.creatures import creature_payload_fields  # noqa: E402
from db.vector.embeddings import VECTOR_SIZE  # noqa: E402
from db.vector.retrieval import (  # noqa: E402
    RetrievalError,
    retrieve_active_scene,
    retrieve_monster,
)
from db.vector.versions import read_index_state  # noqa: E402
from edgar_core.artifacts import text_digest  # noqa: E402
from edgar_core.manifests import AdventureManifest, manifest_digest  # noqa: E402


def load_corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_provenance(corpus: dict[str, Any]) -> None:
    expected_prompt = corpus["prompt_provenance"]["prompt_sha256"]
    actual_prompt = _sha256_file(PROMPT_PATH)
    if actual_prompt != expected_prompt:
        raise ValueError(
            "Production prompt provenance changed: update and review the fidelity corpus before "
            "quoting its ruling baseline."
        )
    for passage in corpus["passages"]:
        expected = passage.get("text_sha256")
        actual = text_digest(passage["text"])
        if expected != actual:
            raise ValueError(
                f"Frozen excerpt digest changed for {passage['id']}: expected {expected}, got {actual}."
            )


def _document(corpus: dict[str, Any], passage: dict[str, Any]) -> dict[str, Any]:
    return corpus["documents"][passage["document"]]


def _create_collection(client: QdrantClient, name: str) -> None:
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )


def _payload(corpus: dict[str, Any], passage: dict[str, Any]) -> dict[str, Any]:
    document = _document(corpus, passage)
    section = passage["section"]
    payload = {
        "text": passage["text"],
        "text_sha256": text_digest(passage["text"]),
        "source": document["source"],
        "source_sha256": document["source_sha256"],
        "extraction_sha256": document["extraction_sha256"],
        "rules_edition": document["edition"],
        "page": passage["page"],
        "page_end": passage["page"],
        "source_pages": [passage["page"]],
        "section": section,
        "heading_path": section,
        "stat_block": passage.get("stat_block", False),
        "chunk_index": 0,
    }
    if passage.get("stat_block"):
        payload.update(creature_payload_fields(payload))
    return payload


def _module_manifest(corpus: dict[str, Any], passage: dict[str, Any]) -> AdventureManifest:
    document = _document(corpus, passage)
    opening = next(p for p in corpus["passages"] if p["id"] == "module-opening-context")
    manifest = AdventureManifest(
        source=document["source"],
        source_sha256=document["source_sha256"],
        extraction_sha256=document["extraction_sha256"],
        rules_edition=document["edition"],
        opening_scene_id="wave_echo_cave",
        scenes=[{"scene_id": "wave_echo_cave", "title": "Wave Echo Cave"}],
        passages=[
            {
                "passage_id": "module_opening",
                "page": opening["page"],
                "start": 0,
                "end": len(opening["text"]),
                "text_sha256": text_digest(opening["text"]),
                "scene_ids": ["wave_echo_cave"],
                "audience": "player",
            },
            {
                "passage_id": "module_dc13",
                "page": passage["page"],
                "start": 0,
                "end": len(passage["text"]),
                "text_sha256": text_digest(passage["text"]),
                "scene_ids": ["wave_echo_cave"],
                "audience": "planner",
            },
        ],
        reviewed_by="EDG-054 deterministic fixture reviewer",
    )
    manifest.reviewed_sha256 = manifest_digest(manifest)
    return manifest


def _populate_retrieval_corpus(
    corpus: dict[str, Any], client: QdrantClient
) -> dict[str, dict[str, Any]]:
    passage_by_id = {p["id"]: p for p in corpus["passages"]}
    rules_collections = {
        _document(corpus, passage)["collection"]
        for passage in corpus["passages"]
        if passage["document"] != "lost_mine"
    }
    module_passage = passage_by_id["module-dc13"]
    module_document = _document(corpus, module_passage)
    module_collection = module_document["collection"]
    for collection in sorted(rules_collections | {module_collection}):
        _create_collection(client, collection)

    points_by_collection: dict[str, list[PointStruct]] = {}
    for i, passage in enumerate(corpus["passages"], start=1):
        if passage["document"] == "lost_mine":
            continue
        collection = _document(corpus, passage)["collection"]
        points_by_collection.setdefault(collection, []).append(
            PointStruct(
                id=i, vector=[1.0] + [0.0] * (VECTOR_SIZE - 1), payload=_payload(corpus, passage)
            )
        )
    for collection, points in points_by_collection.items():
        client.upsert(collection_name=collection, points=points, wait=True)

    manifest = _module_manifest(corpus, module_passage)
    module_points = []
    for point_id, fixture_passage, passage_id in (
        (1000, module_passage, "module_dc13"),
        (
            1001,
            next(p for p in corpus["passages"] if p["id"] == "module-opening-context"),
            "module_opening",
        ),
    ):
        module_points.append(
            PointStruct(
                id=point_id,
                vector=[1.0] + [0.0] * (VECTOR_SIZE - 1),
                payload={
                    **_payload(corpus, fixture_passage),
                    "manifest_sha256": manifest.reviewed_sha256,
                    "passage_id": passage_id,
                    "audience": fixture_passage["audience"],
                    "scene_ids": ["wave_echo_cave"],
                    "entity_ids": [],
                    "requires": {},
                    "span_start": 0,
                    "span_end": len(fixture_passage["text"]),
                },
            )
        )
    client.upsert(
        collection_name=module_collection,
        points=module_points,
        wait=True,
    )
    return {"passages": passage_by_id, "module_manifest": manifest}


def eval_retrieval(corpus: dict[str, Any], client: QdrantClient) -> dict[str, Any]:
    indexed = _populate_retrieval_corpus(corpus, client)
    passages = indexed["passages"]
    manifests = {"lost_mine_of_phandelver": indexed["module_manifest"].model_dump()}

    # The exact/lexical fallback is the production no-provider path. It searches actual local
    # Qdrant collections, with production edition/heading/identity filters and source citations.
    def no_embedding(_query: str):
        raise RuntimeError("embeddings intentionally disabled by offline fidelity harness")

    original_embed_query = retrieval.embed_query
    retrieval.embed_query = no_embedding
    try:
        positive = []
        for case in corpus["retrieval_cases"]:
            passage = passages[case["passage"]]
            document = _document(corpus, passage)
            if passage["document"] == "lost_mine":
                result = retrieve_active_scene(
                    manifests,
                    "wave_echo_cave",
                    {},
                    client=client,
                    rules_edition="2014",
                )
                hits = result["planner_context"]
            elif passage.get("stat_block"):
                hits = retrieve_monster(
                    case["query"],
                    client=client,
                    collections=[document["collection"]],
                    rules_edition=document["edition"],
                )
            else:
                hits = retrieval.search_rules_context(
                    case["query"],
                    client=client,
                    limit=6,
                    min_score=None,
                    rules_edition=document["edition"],
                    references=[case["reference"]],
                )
            match = next(
                (
                    hit
                    for hit in hits
                    if hit.get("source") == case["expected_source"]
                    and hit.get("source_sha256") == document["source_sha256"]
                    and hit.get("extraction_sha256") == document["extraction_sha256"]
                    and hit.get("rules_edition") == document["edition"]
                    and hit.get("text_sha256") == text_digest(passage["text"])
                    and hit.get("section") == passage["section"]
                ),
                None,
            )
            positive.append(
                {
                    "id": case["id"],
                    "retrieved": match is not None,
                    "retrieval_method": (match or {}).get(
                        "retrieval_method", "exact_identity_or_scene"
                    ),
                    "source": (match or {}).get("source"),
                    "page": (match or {}).get("page"),
                    "section": (match or {}).get("section"),
                    "identity_match": match is not None,
                }
            )

        negative = []
        mm = corpus["documents"]["mm"]
        negative_queries = {
            "goblin-does-not-match-hobgoblin": ("Hobgoblin", None),
            "adult-red-dragon-does-not-match-young": ("Young Red Dragon", None),
            "unknown-creature-does-not-match-prose": ("Captain Vellum", None),
            "wrong-source-digest-is-not-identity": ("Goblin", "0" * 64),
        }
        for case in corpus["negative_cases"]:
            case_id = case["id"]
            if case_id == "missing-bound-module-fails-closed":
                try:
                    retrieval.search_adventure_context(
                        "DC 13", [case["collection"]], client=client, require_bound_sources=True
                    )
                except RetrievalError:
                    passed = True
                else:
                    passed = False
                negative.append(
                    {
                        "id": case_id,
                        "passed": passed,
                        "observed": "RetrievalError" if passed else "returned",
                    }
                )
                continue
            query, source_sha = negative_queries[case_id]
            hits = retrieve_monster(
                query,
                client=client,
                collections=[mm["collection"]],
                rules_edition=mm["edition"],
                source_sha256=source_sha,
            )
            negative.append(
                {
                    "id": case_id,
                    "passed": len(hits) == case["expected_hits"],
                    "observed_hits": len(hits),
                }
            )
    finally:
        retrieval.embed_query = original_embed_query

    good = sum(item["retrieved"] for item in positive)
    negative_good = sum(item["passed"] for item in negative)
    return {
        "positive_cases": len(positive),
        "relevant_hits": good,
        "relevance_at_exact_lookup": good / len(positive) if positive else 1.0,
        "identity_correct": good,
        "identity_cases": len(positive),
        "negative_cases": len(negative),
        "negative_passes": negative_good,
        "positive_results": positive,
        "negative_results": negative,
        "interpretation": "Exact/lexical fixture relevance on local Qdrant; no semantic-vector or real-model claim.",
    }


def _eval_rulings(corpus: dict[str, Any]) -> dict[str, Any]:
    passages = {p["id"]: p for p in corpus["passages"]}
    results: list[dict[str, Any]] = []

    # PHB feature: actual engine spend and die roll, with one explicit use.
    fighter = {
        "name": "Lyra",
        "class": "Fighter",
        "level": 5,
        "stats": {"second_wind": {"uses_remaining": 1}},
    }
    feature_text = passages["phb-second-wind"]["text"]
    feature_declared = declares_second_wind("I use Second Wind")
    heal = spend_second_wind(fighter, rng=random.Random(11))
    feature_ok = (
        "second wind" in feature_text.casefold()
        and feature_declared
        and heal is not None
        and heal.source == "second_wind"
        and heal.dice.expression == "1d10+5"
        and heal.amount == heal.dice.total
    )
    results.append(
        {
            "id": "apply-second-wind",
            "passed": feature_ok,
            "applied": {
                "declared": feature_declared,
                "healing": heal.amount if heal else None,
                "dice": heal.dice.expression if heal else None,
            },
        }
    )

    # PHB spell: cast request, slot plan and spell resolution all use production helpers.
    wizard = {"name": "Lyra", "class": "Wizard", "level": 3, "stats": {}}
    spell_request = cast_request("I cast Magic Missile", None, wizard)
    if spell_request is None:
        spell_ok = False
        spell_result = None
    else:
        spec, plan, refusal = spell_request
        spell_result = resolve_spell(wizard, plan, rng=random.Random(11)) if plan else None
        spell_ok = bool(
            refusal is None
            and spec.name == "Magic Missile"
            and spell_result is not None
            and spell_result.darts == 3
            and spell_result.damage is not None
            and spell_result.damage == sum(spell_result.dice_log[0]["rolls"]) + 3
            and spell_result.spec.damage_type == "force"
        )
    results.append(
        {
            "id": "apply-magic-missile",
            "passed": spell_ok,
            "applied": {
                "darts": spell_result.darts if spell_result else None,
                "damage": spell_result.damage if spell_result else None,
                "damage_type": spell_result.spec.damage_type if spell_result else None,
            },
        }
    )

    goblin = parse_monster_stats(passages["mm-goblin"]["text"], name="Goblin")
    goblin_attacks = attack_plan(goblin)
    goblin_ok = (
        goblin.ac == 15
        and goblin.hp_max == 7
        and len(goblin_attacks) == 1
        and goblin_attacks[0].name == "Scimitar"
        and goblin_attacks[0].bonus == 4
        and goblin_attacks[0].damage_dice == "1d6+2"
    )
    results.append(
        {
            "id": "apply-goblin-record",
            "passed": goblin_ok,
            "applied": {
                "ac": goblin.ac,
                "hp": goblin.hp_max,
                "attacks": [a.name for a in goblin_attacks],
            },
        }
    )

    troll = parse_monster_stats(passages["mm-troll"]["text"], name="Troll")
    troll_attacks = attack_plan(troll)
    troll_actor = {
        "name": "Troll",
        "hp_current": 50,
        "hp_max": troll.hp_max,
        "monster_stats": troll.to_dict(),
        "damage_taken_types": ["fire"],
    }
    suppressed, suppression_message = regenerate(troll_actor)
    troll_actor["damage_taken_types"] = []
    healed, _ = regenerate(troll_actor)
    troll_ok = (
        troll.ac == 15
        and troll.hp_max == 84
        and troll.regeneration is not None
        and troll.regeneration.amount == 10
        and set(troll.regeneration.suppressed_by) == {"acid", "fire"}
        and [a.name for a in troll_attacks] == ["Bite", "Claw", "Claw"]
        and suppressed == 0
        and "fire damage" in (suppression_message or "")
        and healed == 10
    )
    results.append(
        {
            "id": "apply-troll-record",
            "passed": troll_ok,
            "applied": {
                "ac": troll.ac,
                "hp": troll.hp_max,
                "regeneration": troll.regeneration.amount if troll.regeneration else None,
                "attack_plan": [a.name for a in troll_attacks],
                "suppressed_heal": suppressed,
                "normal_heal": healed,
            },
        }
    )

    dragon = parse_monster_stats(passages["mm-adult-red-dragon"]["text"], name="Adult Red Dragon")
    dragon_boundary = [value for value in dragon.declared if "Fire Breath" in value]
    dragon_ok = dragon.ac == 19 and dragon.hp_max == 256 and bool(dragon_boundary)
    results.append(
        {
            "id": "adult-dragon-explicit-engine-boundary",
            "passed": dragon_ok,
            "applied": {"ac": dragon.ac, "hp": dragon.hp_max, "declared": dragon_boundary},
        }
    )

    # The module source fact supplies the explicit DC; production resolve_check owns the roll.
    module_text = passages["module-dc13"]["text"]
    dc_match = re.search(r"spell save DC\s+(\d+)", module_text, re.IGNORECASE)
    source_dc = int(dc_match.group(1)) if dc_match else None
    check = resolve_check(
        {"name": "Lyra", "class": "Rogue", "level": 3, "stats": {}},
        "dex",
        dc=source_dc,
        rng=random.Random(11),
    )
    module_ok = (
        source_dc == 13
        and check is not None
        and check.dc == 13
        and check.total == check.natural + check.modifier
    )
    results.append(
        {
            "id": "preserve-module-dc13",
            "passed": module_ok,
            "applied": {
                "dc": check.dc if check else None,
                "total": check.total if check else None,
                "success": check.success if check else None,
            },
        }
    )

    return {
        "cases": len(results),
        "correct": sum(r["passed"] for r in results),
        "accuracy": sum(r["passed"] for r in results) / len(results) if results else 1.0,
        "results": results,
        "interpretation": "Deterministic applied mechanics and explicit engine-boundary accuracy; no LLM adjudication or narration call.",
    }


def _eval_retry_replacement(corpus: dict[str, Any], lock_dir: Path) -> dict[str, Any]:
    from ingestion import embed

    passage = next(p for p in corpus["passages"] if p["id"] == "mm-troll")
    document = _document(corpus, passage)
    collection = "retry_fidelity_monster_manual"
    text_by_hp = {
        84: passage["text"],
        85: passage["text"].replace("Hit Points 84 (8d10 + 40)", "Hit Points 85 (8d10 + 40)"),
    }

    def chunk(hp: int) -> dict[str, Any]:
        text = text_by_hp[hp]
        return {
            "text": text,
            "source": document["source"],
            "source_sha256": document["source_sha256"],
            "extraction_sha256": document["extraction_sha256"],
            "rules_edition": document["edition"],
            "page": passage["page"],
            "page_end": passage["page"],
            "source_pages": [passage["page"]],
            "section": passage["section"],
            "heading_path": passage["section"],
            "stat_block": True,
            "index": 0,
        }

    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        original_get_client, original_get_embeddings = embed.get_qdrant_client, embed.get_embeddings
        import os

        original_lock_dir = os.environ.get("EDGAR_INDEX_LOCK_DIR")
        os.environ["EDGAR_INDEX_LOCK_DIR"] = str(lock_dir)
        embed.get_qdrant_client = lambda: client
        embed.get_embeddings = lambda texts: [[1.0] + [0.0] * (VECTOR_SIZE - 1) for _ in texts]
        try:
            embed._embed_and_upsert([chunk(84)], collection)
            before = retrieve_monster(
                "Troll", client=client, collections=[collection], rules_edition="2014"
            )
            before_version = read_index_state(client, collection)["sources"][document["source"]][
                "active"
            ]["index_version"]
            embed._embed_and_upsert([chunk(85)], collection, replace=True)
            after = retrieve_monster(
                "Troll", client=client, collections=[collection], rules_edition="2014"
            )
            state = read_index_state(client, collection)
        finally:
            embed.get_qdrant_client, embed.get_embeddings = (
                original_get_client,
                original_get_embeddings,
            )
            if original_lock_dir is None:
                os.environ.pop("EDGAR_INDEX_LOCK_DIR", None)
            else:
                os.environ["EDGAR_INDEX_LOCK_DIR"] = original_lock_dir

    before_hp = (
        next((int(v) for v in re.findall(r"Hit Points\s+(\d+)", before[0]["text"])), None)
        if before
        else None
    )
    after_hp = (
        next((int(v) for v in re.findall(r"Hit Points\s+(\d+)", after[0]["text"])), None)
        if after
        else None
    )
    after_version = state["sources"][document["source"]]["active"]["index_version"]
    passed = (
        len(before) == 1
        and before_hp == 84
        and len(after) == 1
        and after_hp == 85
        and bool(after_version)
        and before_version != after_version
    )
    return {
        "id": corpus["retry_case"]["id"],
        "passed": passed,
        "before_retry": {"hits": len(before), "hp": before_hp},
        "after_retry": {
            "hits": len(after),
            "hp": after_hp,
            "attempt_label": "fidelity-attempt-2",
            "index_version": after_version,
        },
        "index_version_changed": before_version != after_version,
        "sentinel_notice": corpus["retry_case"]["note"],
    }


def run_evaluation() -> dict[str, Any]:
    corpus = load_corpus()
    validate_provenance(corpus)
    import os

    previous_lock = os.environ.get("EDGAR_INDEX_LOCK_DIR")
    with TemporaryDirectory(prefix="edg054-index-locks-") as lock_path:
        lock_dir = Path(lock_path)
        os.environ["EDGAR_INDEX_LOCK_DIR"] = str(lock_dir)
        invalidate_collection_cache()
        try:
            with closing(QdrantClient(":memory:")) as client:
                retrieval_report = eval_retrieval(corpus, client)
            ruling_report = _eval_rulings(corpus)
            retry_report = _eval_retry_replacement(corpus, lock_dir)
        finally:
            if previous_lock is None:
                os.environ.pop("EDGAR_INDEX_LOCK_DIR", None)
            else:
                os.environ["EDGAR_INDEX_LOCK_DIR"] = previous_lock
            invalidate_collection_cache()
    return {
        "corpus_id": corpus["corpus_id"],
        "corpus_sha256": _sha256_file(CORPUS_PATH),
        "prompt_sha256": corpus["prompt_provenance"]["prompt_sha256"],
        "provider_calls": corpus["provider_calls"],
        "retrieval": retrieval_report,
        "applied_rulings": ruling_report,
        "retry_replacement": retry_report,
        "all_passed": (
            retrieval_report["relevant_hits"] == retrieval_report["positive_cases"]
            and retrieval_report["negative_passes"] == retrieval_report["negative_cases"]
            and ruling_report["correct"] == ruling_report["cases"]
            and retry_report["passed"]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compact", action="store_true", help="print compact JSON")
    args = parser.parse_args()
    report = run_evaluation()
    print(json.dumps(report, indent=None if args.compact else 2, sort_keys=True))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
