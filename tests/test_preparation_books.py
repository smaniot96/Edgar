"""Required rulebook discovery for campaign preparation, without provider or Docker."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.services import preparation_books
from apps.api.services.preparation_grounding import GroundingUnavailable
from edgar_core.safe_errors import classify_failure

HEX = "a" * 64
ROLE_COLLECTIONS = {
    "rules_player_handbook": "player_handbook",
    "rules_dm_guide": "dm_guide",
    "rules_monster_manual": "monster_manual",
}


class FakeQdrant:
    """Minimal Qdrant surface: each collection has its own routing control point."""

    def __init__(self, states: dict[str, dict] | None = None):
        # Collection name -> routing state. A collection without a state is pre-versioning.
        self.states = dict(states or {})
        self.collections = set(self.states) if states is not None else set(ROLE_COLLECTIONS)

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def retrieve(self, collection_name, ids, with_payload=True, with_vectors=False):
        state = self.states.get(collection_name)
        if state is None:
            return []
        return [SimpleNamespace(payload=state)]

    def scroll(self, **kwargs):  # pragma: no cover - discovery never reads corpus payloads
        raise AssertionError("book discovery must not scan rulebook text")


def _versioned_states(edition: str = "2014", *, skip: str | None = None) -> dict[str, dict]:
    states = {}
    for collection, source in ROLE_COLLECTIONS.items():
        if collection == skip:
            continue
        states[collection] = {
            "schema": 1,
            "sources": {
                source: {
                    "active": {
                        "index_version": HEX,
                        "source_sha256": HEX,
                        "extraction_sha256": HEX,
                        "rules_edition": edition,
                        "chunk_count": 3,
                    }
                }
            },
        }
    return states


def _legacy_states() -> dict[str, dict]:
    # Pre-versioning ingestion: the collections exist, but no routing record exposes an exact,
    # hashed active receipt. Grounding must not invent one.
    return {collection: {"schema": 1, "sources": {}} for collection in ROLE_COLLECTIONS}


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))


def test_versioned_rulebooks_are_discovered_for_every_required_role():
    bindings = preparation_books.discover_book_bindings(
        "2014", client=FakeQdrant(_versioned_states("2014"))
    )

    assert [b.role for b in bindings] == ["phb", "dmg", "mm"]
    assert {b.rules_edition for b in bindings} == {"2014"}


def test_pre_versioning_rulebooks_fail_with_a_specific_reindex_code_before_any_provider_work():
    with pytest.raises(GroundingUnavailable) as caught:
        preparation_books.discover_book_bindings("2014", client=FakeQdrant(_legacy_states()))

    # PHB is checked first, so the failure names the first book that lacks an exact version.
    assert caught.value.code == "required_book_phb_unindexed"
    assert "Player's Handbook" in str(caught.value)
    assert "2014" in str(caught.value)


def test_rulebooks_ingested_without_an_edition_name_the_missing_edition_book():
    with pytest.raises(GroundingUnavailable) as caught:
        preparation_books.discover_book_bindings(
            "2014", client=FakeQdrant(_versioned_states("unspecified"))
        )

    assert caught.value.code == "required_book_phb_unindexed"


def test_missing_monster_manual_is_reported_as_that_book():
    with pytest.raises(GroundingUnavailable) as caught:
        preparation_books.discover_book_bindings(
            "2014", client=FakeQdrant(_versioned_states("2014", skip="rules_monster_manual"))
        )

    assert caught.value.code == "required_book_mm_unindexed"
    assert "Monster Manual" in str(caught.value)


@pytest.mark.parametrize(
    ("code", "label"),
    [
        ("required_book_phb_unindexed", "Player's Handbook"),
        ("required_book_dmg_unindexed", "DM's Guide"),
        ("required_book_mm_unindexed", "Monster Manual"),
    ],
)
def test_book_reindex_codes_are_safe_source_failures_with_a_restart_recovery(code, label):
    failure = classify_failure(code)

    assert failure.code == code
    assert failure.category.value == "source"
    assert failure.recovery_action.value == "restart_preparation"
    assert label in failure.message
    assert failure.message != "The adventure source needs attention before play can continue."


@pytest.mark.asyncio
async def test_pre_versioning_rulebooks_stop_at_the_source_check_without_provider_work(
    tmp_path, monkeypatch
):
    """Reproduce the observed live shape: rulebook points exist, but none were promoted.

    Those points carry no exact active receipt, so preparation must stop in P02 with the
    specific book code and must never dispatch a provider request.
    """
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from apps.api.services import adventures_meta
    from apps.api.services.preparation_stages import make_preparation_supervisor
    from test_preparation_pipeline import OfflineAuthor, _binding

    monkeypatch.setenv("EDGAR_INDEX_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(adventures_meta, "ADVENTURES_DIR", tmp_path / "adventures")
    client = QdrantClient(":memory:")
    for collection, source in ROLE_COLLECTIONS.items():
        client.create_collection(collection, vectors_config=VectorParams(size=3, distance=Distance.COSINE))
        client.upsert(
            collection,
            [PointStruct(id=1, vector=[1.0, 0.0, 0.0], payload={"text": "Legacy rule text.", "source": source, "page": 1, "page_end": 1, "section": "Legacy"})],
        )

    author = OfflineAuthor()
    supervisor = make_preparation_supervisor(provider=author, book_client=client)
    binding = _binding()
    supervisor.create_run(binding)
    for _ in range(20):
        step = await supervisor.step(
            binding.source_slug, binding.run_id, "offline-worker", lambda _: True, lambda bundle: None
        )
        if step.state.status.value in ("ready", "blocked", "failed"):
            break

    state = supervisor.store.load_run(binding.source_slug, binding.run_id)
    # "blocked" is the non-retryable terminal state: the commission must be restarted.
    assert state.status.value == "blocked"
    assert state.current_stage.value == "P02"
    assert state.last_safe_error_code == "required_book_phb_unindexed"
    assert author.calls == []
