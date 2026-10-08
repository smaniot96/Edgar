"""db.vector.retrieval ranking/dedupe/threshold/cap + embedding cache, against a fake Qdrant."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from db.vector import embeddings as emb, retrieval
from db.vector.collections import invalidate_collection_cache


class FakeQdrant:
    """Minimal QdrantClient stand-in: `get_collections` + `query_points`."""

    def __init__(self, data: dict[str, list[tuple[float, dict[str, Any]]]], fail: set[str] | None = None):
        self.data = data
        self.fail = fail or set()
        self.queried: list[str] = []
        self.list_calls = 0

    def get_collections(self):
        self.list_calls += 1
        return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in self.data])

    def query_points(self, collection_name: str, query, limit: int, with_payload: bool):
        self.queried.append(collection_name)
        if collection_name in self.fail:
            raise ConnectionError("qdrant down")
        if collection_name not in self.data:
            raise ValueError(f"Collection {collection_name} not found")
        hits = sorted(self.data[collection_name], key=lambda h: h[0], reverse=True)[:limit]
        return SimpleNamespace(points=[SimpleNamespace(score=s, payload=p) for s, p in hits])


def _p(text: str, source: str = "book", page: int = 1, **extra: Any) -> dict[str, Any]:
    return {"text": text, "source": source, "page": page, **extra}


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def _fake(texts: list[str], model: str | None = None) -> list[list[float]]:
        calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    emb.clear_query_embedding_cache()
    invalidate_collection_cache()
    monkeypatch.setattr(emb, "get_embeddings", _fake)
    yield calls
    emb.clear_query_embedding_cache()
    invalidate_collection_cache()


def test_results_ranked_globally_across_collections() -> None:
    client = FakeQdrant(
        {
            "rules_player_handbook": [(0.5, _p("phb grapple rules")), (0.3, _p("phb shove rules"))],
            "rules_dm_guide": [(0.35, _p("dmg improvising damage"))],
            "rules_monster_manual": [(0.9, _p("goblin stat block")), (0.4, _p("hobgoblin lore"))],
        }
    )
    out = retrieval.search_rules_context("grapple a goblin", client=client, limit=None, min_score=None)
    assert [c["score"] for c in out] == [0.9, 0.5, 0.4, 0.35, 0.3]
    assert out[0]["collection"] == "rules_monster_manual"
    assert all(c["kind"] == "rules" for c in out)
    assert set(out[0]) >= {"text", "source", "page", "kind", "collection", "score"}


def test_duplicates_and_near_duplicates_are_dropped() -> None:
    words = " ".join(f"word{i}" for i in range(40))
    client = FakeQdrant(
        {
            "rules_monster_manual": [(0.8, _p("The goblin hides.  " + words))],
            # Same PDF under the legacy name: identical text modulo whitespace/case.
            "monster_manual": [(0.79, _p("the GOBLIN hides. " + words))],
            "rules_player_handbook": [
                (0.7, _p("The goblin hides quickly. " + words)),  # near-identical
                (0.6, _p("Completely different text about spellcasting.")),
            ],
        }
    )
    out = retrieval.search_rules_context("goblin", client=client, min_score=None)
    assert [c["score"] for c in out] == [0.8, 0.6]


def test_min_score_threshold() -> None:
    client = FakeQdrant({"chalice_of_the_mountain_god": [(0.6, _p("temple")), (0.1, _p("noise"))]})
    out = retrieval.search_adventure_context("temple", None, client=client, min_score=0.2)
    assert [c["text"] for c in out] == ["temple"]
    assert out[0]["kind"] == "adventure"
    out_all = retrieval.search_adventure_context("temple", None, client=client, min_score=None)
    assert len(out_all) == 2


def test_total_cap_applied() -> None:
    client = FakeQdrant(
        {
            "rules_player_handbook": [(0.9 - i * 0.01, _p(f"phb chunk {i}")) for i in range(5)],
            "rules_dm_guide": [(0.85 - i * 0.01, _p(f"dmg chunk {i}")) for i in range(5)],
        }
    )
    out = retrieval.search_rules_context("anything", client=client, limit=3)
    assert len(out) == 3
    assert [c["score"] for c in out] == sorted((c["score"] for c in out), reverse=True)
    # Default cap is finite even when callers only pass limit_per_collection.
    assert len(retrieval.search_rules_context("anything", client=client)) == retrieval.DEFAULT_RULES_LIMIT


def test_missing_collections_are_not_queried() -> None:
    client = FakeQdrant({"rules_player_handbook": [(0.5, _p("phb"))]})
    out = retrieval.search_rules_context("q", client=client)
    assert len(out) == 1
    assert client.queried == ["rules_player_handbook"]  # legacy aliases / absent books skipped
    # Collection list is cached between calls.
    retrieval.search_rules_context("q2", client=client)
    assert client.list_calls == 1


def test_campaign_lore_competes_on_score() -> None:
    client = FakeQdrant(
        {
            "chalice_of_the_mountain_god": [(0.4, _p("module text"))],
            "campaign_lore_7": [(0.8, _p("dm note: the priest is a doppelganger"))],
        }
    )
    out = retrieval.search_adventure_context("priest", ["chalice_of_the_mountain_god"], campaign_id=7, client=client)
    assert out[0]["collection"] == "campaign_lore_7"


def test_no_existing_collection_skips_embedding(fake_embeddings: list[list[str]]) -> None:
    client = FakeQdrant({})
    assert retrieval.search_rules_context("q", client=client) == []
    assert fake_embeddings == []


def test_query_embedding_cached_across_buckets(fake_embeddings: list[list[str]]) -> None:
    client = FakeQdrant(
        {
            "rules_player_handbook": [(0.5, _p("phb"))],
            "chalice_of_the_mountain_god": [(0.5, _p("module"))],
        }
    )
    retrieval.search_rules_context("I open the door", client=client)
    retrieval.search_adventure_context("I open the door", None, client=client)
    assert fake_embeddings == [["I open the door"]]
    retrieval.search_rules_context("something else", client=client)
    assert len(fake_embeddings) == 2


def test_failed_collection_logged_and_others_returned(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeQdrant(
        {"rules_player_handbook": [(0.5, _p("phb"))], "rules_dm_guide": [(0.6, _p("dmg"))]},
        fail={"rules_dm_guide"},
    )
    with caplog.at_level(logging.WARNING, logger="db.vector.retrieval"):
        out = retrieval.search_rules_context("q", client=client)
    assert [c["text"] for c in out] == ["phb"]
    assert any("rules_dm_guide" in r.getMessage() and "ConnectionError" in r.getMessage() for r in caplog.records)


def test_total_outage_raises() -> None:
    client = FakeQdrant(
        {"rules_player_handbook": [(0.5, _p("phb"))]}, fail={"rules_player_handbook"}
    )
    with pytest.raises(retrieval.RetrievalError):
        retrieval.search_rules_context("q", client=client)


def test_heading_payload_fields_passed_through() -> None:
    client = FakeQdrant(
        {
            "rules_player_handbook": [
                (0.5, _p("Chapter 9 > Grappling\nWhen you want to grab", section="Grappling", chapter="Chapter 9", heading_path="Chapter 9 > Grappling")),
                (0.4, _p("legacy chunk without structure")),
            ]
        }
    )
    out = retrieval.search_rules_context("grapple", client=client)
    assert out[0]["section"] == "Grappling"
    assert out[0]["heading_path"] == "Chapter 9 > Grappling"
    assert "section" not in out[1]


def test_retrieve_monster_prefers_named_stat_block() -> None:
    stat = (
        "Goblin\n_Small humanoid (goblinoid), neutral evil_\n\nArmor Class 15 (leather armor, shield) "
        "Hit Points 7 (2d6) Speed 30 ft.\n\nACTIONS\n\nScimitar. Melee Weapon Attack: +4 to hit"
    )
    client = FakeQdrant(
        {
            "rules_monster_manual": [
                (0.52, _p("Goblinoids are a family of creatures ruled by hobgoblins and bugbears.")),
                (0.50, _p(stat, section="Goblin", heading_path="Goblin", stat_block=True)),
                (0.49, _p("Bugbear\nArmor Class 16 Hit Points 27 (5d8 + 5)", section="Bugbear", stat_block=True)),
            ],
            "rules_player_handbook": [(0.95, _p("goblin mentioned in the PHB"))],
        }
    )
    out = retrieval.retrieve_monster("Goblins", client=client)
    assert out[0]["section"] == "Goblin"
    assert out[0]["kind"] == "rules"
    assert all(c["collection"] == "rules_monster_manual" for c in out)
    assert len(out) == retrieval.DEFAULT_MONSTER_LIMIT
    assert "rules_player_handbook" not in client.queried


def test_retrieve_monster_without_monster_manual_returns_empty() -> None:
    client = FakeQdrant({"rules_player_handbook": [(0.9, _p("x"))]})
    assert retrieval.retrieve_monster("goblin", client=client) == []
    assert retrieval.retrieve_monster("  ", client=client) == []


# --- session intro reads the earliest pages, not an arbitrary scroll window -----------------


class _FakeScrollClient:
    """Points with hash-like ids in arbitrary order; supports order_by or range filter."""

    def __init__(self, *, has_index: bool, index_creatable: bool):
        self.has_index = has_index
        self.index_creatable = index_creatable
        self.points = [
            SimpleNamespace(id=(page * 7919) % 1009, payload={"text": f"page {page} part {i}", "page": page, "chunk_index": page * 10 + i})
            for page in range(300, 0, -1)
            for i in (1, 0)
        ]

    def scroll(self, collection_name, limit, with_payload, with_vectors, order_by=None, scroll_filter=None):
        pts = self.points
        if order_by is not None:
            if not self.has_index:
                raise RuntimeError("No range index for `order_by` key `page`")
            pts = sorted(pts, key=lambda p: p.payload["page"])
        if scroll_filter is not None:
            lte = scroll_filter.must[0].range.lte
            pts = [p for p in pts if p.payload["page"] <= lte]
        return pts[:limit], None

    def create_payload_index(self, collection_name, field_name, field_schema, wait=True):
        if not self.index_creatable:
            raise RuntimeError("forbidden")
        self.has_index = True


@pytest.mark.parametrize(
    ("has_index", "index_creatable"), [(True, False), (False, True), (False, False)]
)
def test_intro_reads_first_pages_in_order(monkeypatch: pytest.MonkeyPatch, has_index: bool, index_creatable: bool) -> None:
    from apps.api.services import session_intro as si

    fake = _FakeScrollClient(has_index=has_index, index_creatable=index_creatable)
    monkeypatch.setattr(si, "get_qdrant_client", lambda: fake)
    monkeypatch.setattr(si, "_page_index_attempted", set())
    text = si._fetch_opening_text(["big_module"], max_chunks=3)
    assert text.splitlines() == ["page 1 part 0", "page 1 part 1", "page 2 part 0"]
