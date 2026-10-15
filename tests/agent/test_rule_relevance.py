"""Frozen useful-evidence/ruling evaluation with disposable Qdrant and a fake provider.

The provider checks its actual prompt and selects the supporting supplied reference. These
checks prove the retrieval/prompt/citation contract, not a real model's rules knowledge.
"""

import random

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from agent.models.adjudication import AdjudicationResult
from agent.models.parsed_input import ParsedInput
from agent.nodes import rules_adjudicator as ra, world_retriever as wr
from db.vector import retrieval
from db.vector.collections import invalidate_collection_cache
from db.vector.versions import INDEX_STATE_ID
from edgar_core.sources import source_citation
from tools.dice import use_rng

BOOK = "rules_player_handbook"
RULES = {
    "Persuasion": (
        179,
        "A Charisma (Persuasion) check influences someone with tact, social graces, or good nature.",
    ),
    "Insight": (
        178,
        "Wisdom (Insight) determines the true intentions of a creature by discerning lies or predicting a move.",
    ),
    "Perception": (
        178,
        "Wisdom (Perception) lets you spot, hear, or otherwise detect the presence of something.",
    ),
    "Investigation": (
        178,
        "Intelligence (Investigation) lets you look around for clues and make deductions based on those clues.",
    ),
    "Short Rest": (
        186,
        "A short rest lasts at least 1 hour; a character can spend Hit Dice at its end.",
    ),
    "Second Wind": (
        72,
        "On your turn, you can use a bonus action to regain hit points. Once used, you must finish a short or long rest before using this feature again.",
    ),
    "Magic Missile": (
        258,
        "You create three glowing darts of magical force. Each dart automatically hits a creature of your choice that you can see within range.",
    ),
}


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(retrieval, "embed_query", lambda text: [1.0, 0.0, 0.0])
    monkeypatch.setattr(wr, "inspect_source", lambda *a, **kw: {"slug": "none", "status": "ready"})
    q = QdrantClient(":memory:")
    q.create_collection(BOOK, vectors_config=VectorParams(size=3, distance=Distance.COSINE))
    points = []
    for i, (title, (page, body)) in enumerate(RULES.items()):
        payload = {
            "text": f"{title}\n{body}",
            "section": title,
            "heading_path": f"PHB > {title}",
            "source": "PHB",
            "source_sha256": "a" * 64,
            "extraction_sha256": "b" * 64,
            "rules_edition": "2014",
            "page": page,
            "source_pages": [page],
            "chunk_index": i * 10,
            "source_version": "v1",
            "index_version": "active",
        }
        # Deliberately worse semantic score than unrelated rooms, initiative and treasure.
        points.append(PointStruct(id=i + 1, vector=[0.05, 1.0, 0.0], payload=payload))
    for i, title in enumerate(("Maze", "Initiative", "Magic Items"), 100):
        points.append(
            PointStruct(
                id=i,
                vector=[1.0, 0.0, 0.0],
                payload={
                    "text": f"{title}\nUnrelated material about a dungeon room and treasure.",
                    "section": title,
                    "source": "PHB",
                    "page": i,
                    "rules_edition": "2014",
                },
            )
        )
    points.extend(
        [
            PointStruct(
                id=200,
                vector=[1.0, 0.0, 0.0],
                payload={
                    **points[4].payload,
                    "text": "Variant: Gritty Realism\nA short rest lasts 8 hours.",
                    "section": "Short Rest",
                    "heading_path": "DMG > Rest Variants > Short Rest",
                },
            ),
            PointStruct(
                id=201,
                vector=[1.0, 0.0, 0.0],
                payload={
                    **points[6].payload,
                    "text": "Magic Missile\nDifferent edition spell text.",
                    "rules_edition": "2024",
                },
            ),
        ]
    )
    q.upsert(BOOK, points)
    invalidate_collection_cache()
    yield q
    q.close()
    invalidate_collection_cache()


# All actions are frozen: independent English and Italian inputs must locate the same
# English source evidence, without using NPC/room names as the core rules query.
@pytest.mark.parametrize(
    "action,parsed,expected",
    [
        (
            "I politely ask Odo to compare the ledgers.",
            ParsedInput(intent="rp", check="persuasion"),
            ["Persuasion"],
        ),
        ("Cerco di capire se Mara mente.", ParsedInput(intent="rp", check="insight"), ["Insight"]),
        ("I listen for movement behind the door.", ParsedInput(check="perception"), ["Perception"]),
        (
            "Esamino i riflessi e cerco indizi.",
            ParsedInput(check="investigation"),
            ["Investigation"],
        ),
        ("I take a short rest and use Second Wind.", ParsedInput(), ["Short Rest", "Second Wind"]),
        (
            "Faccio un riposo breve e uso Recuperare Energie.",
            ParsedInput(),
            ["Short Rest", "Second Wind"],
        ),
        (
            "I cast Magic Missile at the hostile goblin.",
            ParsedInput(intent="combat"),
            ["Magic Missile"],
        ),
        ("Lancio Dardo Incantato sul goblin.", ParsedInput(intent="combat"), ["Magic Missile"]),
    ],
)
async def test_frozen_evidence_and_resulting_cited_ruling(
    corpus, fake_llm_schema, action, parsed, expected
):
    state = {
        "player_input": action,
        "parsed_input": parsed,
        "qdrant_client": corpus,
        "rules_edition": "2014",
        "source_mode": "homebrew",
    }
    out = await wr.world_retriever_node(state)
    evidence = out["rules_context"]
    assert {c["section"] for c in evidence} == set(expected)
    assert all(c["rule_policy"] == "core" for c in evidence)
    assert all(c["rules_edition"] == "2014" for c in evidence)
    assert all(c["reference_match"] == "exact" for c in evidence)
    # Useful precision and focus coverage, rather than a nonzero hit count.
    useful_precision = sum(c["section"] in expected for c in evidence) / len(evidence)
    focus_coverage = len({c["section"] for c in evidence} & set(expected)) / len(expected)
    assert useful_precision == focus_coverage == 1.0

    def grounded_provider(messages):
        prompt = messages[-1]["content"]
        assert "Unrelated material" not in prompt and "8 hours" not in prompt
        assert "Different edition" not in prompt and "Engine outcome (final" in prompt
        refs = []
        summaries = []
        for title in expected:
            chunk = next(c for c in evidence if c["section"] == title)
            ref = source_citation(chunk)
            assert RULES[title][1] in prompt and ref["reference_id"] in prompt
            assert f"physical pages={RULES[title][0]}" in prompt
            refs.append(ref["reference_id"])
            summaries.append(RULES[title][1])
        # Deliberately wrong proposed outcome/dice: the engine must retain authority.
        return AdjudicationResult(
            success=False,
            dice_result=999,
            mechanical_summary=" ".join(summaries),
            citation_ids=refs,
        )

    fake_llm_schema.responses[AdjudicationResult] = grounded_provider
    with use_rng(random.Random(0)):
        result = await ra.adjudicate({**state, **out})
    assert {c["page"] for c in result.citations} == {RULES[t][0] for t in expected}
    assert all(c["edition_status"] == "compatible" for c in result.citations)
    assert result.dice_result != 999
    assert all(RULES[t][1] in result.mechanical_summary for t in expected)
    if parsed.check:
        assert result.dc == 15 and result.success == (result.dice_result >= 15)
    else:
        assert result.success is True and result.dice_result is None


def test_exact_fallback_survives_embedding_outage_and_unspecified_campaign_edition(
    corpus, monkeypatch
):
    def unavailable(text):
        raise ConnectionError("embedding service unavailable")

    monkeypatch.setattr(retrieval, "embed_query", unavailable)
    hits = retrieval.search_rules_context(
        "Magic Missile rules", references=["Magic Missile"], client=corpus
    )
    assert hits and all(c["section"] == "Magic Missile" for c in hits)
    assert {c["rules_edition"] for c in hits} == {"2014", "2024"}


def test_optional_material_is_separate_even_when_it_has_the_highest_vector_score(corpus):
    core = retrieval.search_rules_context(
        "Short Rest rules", references=["Short Rest"], client=corpus
    )
    assert core and all(c["rule_policy"] == "core" for c in core)
    all_rules = retrieval.search_rules_context(
        "Short Rest rules", references=["Short Rest"], client=corpus, include_optional=True
    )
    assert {c["rule_policy"] for c in all_rules} == {"core", "variant"}


def test_legacy_exact_text_fallback_and_unknown_edition_remain_unverified(corpus):
    corpus.upsert(
        BOOK,
        [
            PointStruct(
                id=300,
                vector=[0.0, 1.0, 0.0],
                payload={
                    "text": "Feather Fall\nChoose up to five falling creatures within range.",
                    "source": "legacy PHB",
                    "page": 239,
                },
            )
        ],
    )
    hits = retrieval.search_rules_context(
        "Feather Fall rules", references=["Feather Fall"], client=corpus, rules_edition="2014"
    )
    assert len(hits) == 1 and hits[0]["reference_match"] == "exact"
    assert source_citation(hits[0])["edition_status"] == "unverified"


def test_neighbors_require_same_section_source_version_and_edition(corpus):
    seed = corpus.retrieve(BOOK, [6])[0].payload
    good = {
        **seed,
        "text": "Regain use when you finish a short or long rest.",
        "page": 73,
        "source_pages": [73],
        "chunk_index": 51,
    }
    corpus.upsert(
        BOOK,
        [
            PointStruct(id=301, vector=[0.0, 1.0, 0.0], payload=good),
            PointStruct(
                id=302,
                vector=[0.0, 1.0, 0.0],
                payload={**good, "section": "Action Surge", "heading_path": "PHB > Action Surge"},
            ),
            PointStruct(
                id=303, vector=[0.0, 1.0, 0.0], payload={**good, "extraction_sha256": "c" * 64}
            ),
            PointStruct(
                id=304, vector=[0.0, 1.0, 0.0], payload={**good, "index_version": "staged"}
            ),
            PointStruct(id=305, vector=[0.0, 1.0, 0.0], payload={**good, "rules_edition": "2024"}),
        ],
    )
    hits = retrieval.search_rules_context(
        "Second Wind rules",
        references=["Second Wind"],
        client=corpus,
        rules_edition="2014",
        limit=2,
    )
    assert len(hits) == 2 and hits[1]["text"] == good["text"]
    assert hits[1]["retrieval_method"] == "same_section_neighbor"
    assert hits[1]["page"] == 73 and hits[1]["index_version"] == "active"


def test_direct_exact_route_excludes_staged_and_obsolete_points(corpus):
    seed = corpus.retrieve(BOOK, [7])[0].payload
    corpus.upsert(
        BOOK,
        [
            PointStruct(
                id=400, vector=[0.0, 1.0, 0.0], payload={**seed, "index_version": "staged"}
            ),
            PointStruct(
                id=401, vector=[0.0, 1.0, 0.0], payload={**seed, "index_version": "obsolete"}
            ),
            PointStruct(
                id=INDEX_STATE_ID,
                vector={},
                payload={"schema": 1, "sources": {"PHB": {"active": {"index_version": "active"}}}},
            ),
        ],
    )
    hits = retrieval._lookup_rule_candidates(corpus, BOOK, ["Magic Missile"], "2014")
    assert hits and all(c["index_version"] == "active" for c in hits)


def test_missing_relevant_evidence_does_not_fall_back_to_unrelated_high_scores(corpus):
    assert (
        retrieval.search_rules_context("Teleport rules", references=["Teleport"], client=corpus)
        == []
    )


def test_marked_optional_evidence_cannot_enter_ruling_or_narration_from_stale_state():
    from agent.nodes.narrator import narrator_context

    variant = {
        "kind": "rules",
        "section": "Variant: Gritty Realism",
        "text": "A short rest takes eight hours.",
    }
    state = {"rules_context": [variant], "rules_edition": "2014"}
    assert ra._rules_context(state) == []
    assert narrator_context(state) == ([], [])


def test_optional_exact_heading_does_not_hide_core_lexical_fallback(corpus, monkeypatch):
    # Only the variant has exact metadata; a legacy core paragraph still supplies the rule.
    core = corpus.retrieve(BOOK, [5])[0].payload
    corpus.upsert(
        BOOK,
        [
            PointStruct(
                id=5,
                vector=[0.05, 1.0, 0.0],
                payload={
                    **core,
                    "section": "Recovering",
                    "heading_path": "PHB > Recovering",
                    "text": "Recovering\nA Short Rest lasts at least 1 hour, allowing Hit Dice recovery.",
                },
            )
        ],
    )
    monkeypatch.setattr(
        retrieval, "embed_query", lambda _: (_ for _ in ()).throw(ConnectionError())
    )
    hits = retrieval.search_rules_context(
        "Short Rest rules", references=["Short Rest"], rules_edition="2014", client=corpus
    )
    assert len(hits) == 1
    assert hits[0]["section"] == "Recovering" and hits[0]["rule_policy"] == "core"
    assert hits[0]["retrieval_method"] == "lexical"


def test_independent_queries_preserve_distinct_citations(monkeypatch):
    chunks = [
        {"source": "PHB", "text": "Shared rule wording", "page": page, "rules_edition": "2014"}
        for page in (10, 20)
    ]
    monkeypatch.setattr(wr, "search_rules_context", lambda *a, **kw: chunks)
    hits, degraded = wr._search_rule_queries(
        [{"query": "feature", "references": ["feature"]}], client=None, rules_edition="2014"
    )
    assert [c["page"] for c in hits] == [10, 20] and not degraded


def test_native_qdrant_exact_rules_filter_edition_and_staged_evidence(corpus, monkeypatch):
    from contextlib import closing

    from testcontainers.qdrant import QdrantContainer

    from db.vector.versions import begin_index, promote_index

    monkeypatch.setattr(
        retrieval, "embed_query", lambda _: (_ for _ in ()).throw(ConnectionError())
    )
    with QdrantContainer("qdrant/qdrant:v1.16.3") as container:
        with closing(container.get_client()) as q:
            q.create_collection(BOOK, vectors_config=VectorParams(size=3, distance=Distance.COSINE))
            points, _ = corpus.scroll(BOOK, limit=100, with_vectors=True)
            q.upsert(
                BOOK, [PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points]
            )
            token = begin_index(q, BOOK, "PHB", "active")
            promote_index(q, BOOK, "PHB", "active", token, {}, validate=lambda: None)
            q.upsert(
                BOOK,
                [
                    PointStruct(
                        id=900,
                        vector=[1.0, 0.0, 0.0],
                        payload={
                            **points[0].payload,
                            "section": "Magic Missile",
                            "text": "Magic Missile\nIncomplete staged replacement must stay hidden.",
                            "index_version": "staged",
                        },
                    )
                ],
            )
            invalidate_collection_cache()
            hits = retrieval.search_rules_context(
                "Magic Missile rules", references=["Magic Missile"], rules_edition="2014", client=q
            )
            assert len(hits) == 1
            assert "three glowing darts" in hits[0]["text"]
            assert hits[0]["rules_edition"] == "2014" and hits[0]["index_version"] == "active"
