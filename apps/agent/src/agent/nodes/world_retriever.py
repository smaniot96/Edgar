"""Retrieve edition-filtered rules and reviewed active-scene passages concurrently.

Only player passages enter adventure_context/retrieved_context. Relevant private passages
go to planner_context. Missing manifests, unknown scenes and changed indexed spans fail
module play with an explicit review error; homebrew requires no module context.
"""

import asyncio
import logging
import time

from agent.scene_guidance import cached_scene_context
from agent.state import AgentState
from agent.turn_diagnostics import current_collector
from db.vector import search_rules_context
from db.vector.collections import effective_adventure_collection_names
from db.vector.references import normalize_reference
from db.vector.retrieval import RetrievalError, retrieve_active_scene
from db.vector.source_status import inspect_source
from edgar_core.log_safety import error_summary
from edgar_core.manifests import ManifestError
from edgar_core.sources import source_citation

log = logging.getLogger(__name__)

_LIMIT_PER_COLLECTION = 5
_MERGED_TOP_K = 20
_MAX_RULE_QUERIES = 4


# Canonical rulebook headings, with action-language aliases used by the existing UI.
# These are retrieval topics, never implementations of mechanics or spell effects.
_RULE_TOPICS = (
    ("Second Wind", ("second wind", "recuperare energie")),
    ("Short Rest", ("short rest", "riposo breve")),
    ("Long Rest", ("long rest", "riposo lungo")),
    ("Magic Missile", ("magic missile", "dardo incantato", "dardi incantati")),
    ("Grappling", ("grapple", "grappling", "afferrare", "afferro", "lotta")),
    ("Hiding", ("hide", "hiding", "nascondo", "nascondersi")),
)
_SKILLS = {
    "athletics": "Athletics",
    "acrobatics": "Acrobatics",
    "stealth": "Stealth",
    "sleight_of_hand": "Sleight of Hand",
    "perception": "Perception",
    "investigation": "Investigation",
    "insight": "Insight",
    "survival": "Survival",
    "medicine": "Medicine",
    "arcana": "Arcana",
    "history": "History",
    "nature": "Nature",
    "religion": "Religion",
    "animal_handling": "Animal Handling",
    "persuasion": "Persuasion",
    "deception": "Deception",
    "intimidation": "Intimidation",
    "performance": "Performance",
    "str": "Strength",
    "dex": "Dexterity",
    "con": "Constitution",
    "int": "Intelligence",
    "wis": "Wisdom",
    "cha": "Charisma",
    "thieves_tools": "Using Ability Scores",
    "attack": "Making an Attack",
    "spell_attack": "Spellcasting",
}


def build_rule_queries(state: AgentState) -> list[dict]:
    """Plan independent mechanics/feature queries; story names never enter core queries.

    The parser's enriched scene query can identify a known topic, but does not become the
    core query itself. Trivial conversation needs no rules. Reviewed story scope continues
    to come exclusively from current_scene_id and its manifest.
    """
    parsed = state.get("parsed_input")
    text = normalize_reference(
        f"{state.get('player_input') or ''} {getattr(parsed, 'retrieval_query', '') or ''}"
    )
    topics = [
        title
        for title, aliases in _RULE_TOPICS
        if any(f" {normalize_reference(alias)} " in f" {text} " for alias in aliases)
    ]
    # Exact named entities have their own focused lookup, independent of rest/check topics.
    entities = getattr(parsed, "entities", {}) or {}
    for key in ("spell", "spell_name", "feature", "feature_name"):
        value = entities.get(key)
        if isinstance(value, str) and 0 < len(value.split()) <= 8:
            topics.append(value.strip()[:100])
    check = getattr(parsed, "check", None)
    if check in _SKILLS:
        topics.append(_SKILLS[check])
    elif check and check.endswith("_save"):
        topics.append("Saving Throws")
    elif not topics and getattr(parsed, "intent", None) == "combat":
        topics.append("Making an Attack")
    elif not topics and any(
        word in text.split() for word in ("explore", "exploration", "esploro", "esplorazione")
    ):
        topics.append("Perception")
    return [{"query": f"{title} rules", "references": [title]} for title in dict.fromkeys(topics)][
        :_MAX_RULE_QUERIES
    ]


def _search_rule_queries(queries: list[dict], *, client, rules_edition: str) -> tuple[list, bool]:
    """Keep evidence from each focus instead of letting one high vector score crowd it out."""
    buckets = []
    failures = 0
    for query in queries:
        try:
            buckets.append(
                search_rules_context(
                    query["query"],
                    references=query["references"],
                    limit_per_collection=_LIMIT_PER_COLLECTION,
                    limit=3,
                    client=client,
                    rules_edition=rules_edition,
                )
            )
        except Exception as exc:
            failures += 1
            log.warning("retrieval: focused rule query failed (%s)", type(exc).__name__)
    if failures and failures == len(queries):
        raise RetrievalError("Every focused rule query failed.")
    merged = []
    seen = set()
    # Round robin gives each independent mechanic its leading evidence before continuations.
    for rank in range(3):
        for bucket in buckets:
            if rank < len(bucket):
                chunk = bucket[rank]
                identity = source_citation(chunk, rules_edition)["reference_id"]
                if identity not in seen:
                    merged.append(chunk)
                    seen.add(identity)
    return merged[:6], bool(failures)


async def world_retriever_node(state: AgentState) -> dict:
    """Retrieve rules and adventure context; record latency, chunk counts and degradation (EDG-053)."""
    started = time.monotonic()
    out = await _retrieve(state)
    collector = current_collector()
    if collector is not None and state.get("player_input"):
        collector.record_retrieval(
            latency_ms=int((time.monotonic() - started) * 1000),
            rules_chunks=len(out.get("rules_context") or []),
            adventure_chunks=len(out.get("adventure_context") or []),
            degraded=bool(out.get("retrieval_degraded")),
        )
    return out


async def _retrieve(state: AgentState) -> dict:
    player_input = state.get("player_input")
    adventure_collections = (
        [] if state.get("source_mode") == "homebrew" else state.get("adventure_collections")
    )

    if not player_input:
        return {"rules_context": [], "adventure_context": [], "retrieved_context": []}

    queries = build_rule_queries(state)
    client = state.get("qdrant_client")

    previous_sources = {s["slug"]: s for s in state.get("source_status", [])}
    expected = {slug: source.get("expected_chunks") for slug, source in previous_sources.items()}
    names = (
        []
        if state.get("source_mode") == "homebrew"
        else effective_adventure_collection_names(adventure_collections)
    )
    statuses = await asyncio.to_thread(
        lambda: [inspect_source(n, expected_chunks=expected.get(n), client=client) for n in names]
    )
    statuses = [{**previous_sources.get(s["slug"], {}), **s} for s in statuses]
    source_degraded = any(s["status"] != "ready" for s in statuses)

    def adventure_search():
        if state.get("source_mode") == "homebrew":
            return {"adventure_context": [], "planner_context": [], "manifest_status": "homebrew"}
        manifests = state.get("adventure_manifests") or {}
        if state.get("source_mode") == "module" or manifests:
            if set(manifests) != set(names):
                raise ManifestError("Every bound adventure needs its reviewed scene manifest.")
            if cached := cached_scene_context(state):
                return cached
            return {
                **retrieve_active_scene(
                    manifests,
                    state.get("current_scene_id"),
                    state.get("world_flags"),
                    client=client,
                    rules_edition=state.get("rules_edition", "2014"),
                ),
                "manifest_status": "reviewed",
            }
        # Legacy sources remain available for explicit inspection, not automatic narration.
        return {
            "adventure_context": [],
            "planner_context": [],
            "manifest_status": "review_required",
        }

    results = await asyncio.gather(
        asyncio.to_thread(
            _search_rule_queries,
            queries,
            client=client,
            rules_edition=state.get("rules_edition", "2014"),
        ),
        asyncio.to_thread(
            adventure_search,
        ),
        return_exceptions=True,
    )

    degraded = source_degraded or bool(state.get("retrieval_degraded"))
    buckets: list[list] = []
    scene_context = {}
    manifest_error = None
    for name, res in zip(("rules", "adventure"), results, strict=True):
        if isinstance(res, BaseException):
            # Qdrant blip / embeddings outage: keep the turn playable with empty context.
            log.warning(
                "retrieval_degraded",
                extra={"bucket": name, "error": error_summary(res)},
            )
            degraded = True
            if isinstance(res, ManifestError):
                manifest_error = str(res)
                scene_context["manifest_status"] = "review_required"
            elif name == "adventure" and names:
                source_degraded = True
                statuses = [
                    {
                        **s,
                        "status": "unavailable",
                        "message": "Adventure retrieval failed. Try again or choose homebrew play.",
                    }
                    for s in statuses
                ]
            buckets.append([])
        else:
            if name == "adventure":
                scene_context = res
                buckets.append(res["adventure_context"])
            else:
                hits, rules_degraded = res
                degraded = degraded or rules_degraded
                buckets.append(hits)
    rules_context, adventure_context = buckets

    out: dict = {
        "source_status": statuses or state.get("source_status", []),
        "retrieval_degraded": degraded,
        "rules_context": rules_context,
        "rule_queries": queries,
        "adventure_context": adventure_context,
        "retrieved_context": (rules_context + adventure_context)[:_MERGED_TOP_K],
        "planner_context": scene_context.get("planner_context", []),
        "allowed_scene_ids": scene_context.get("allowed_scene_ids", []),
        "discovery_entities": scene_context.get("discovery_entities", []),
        "manifest_status": scene_context.get("manifest_status", "review_required"),
    }
    if source_degraded and state.get("source_mode") == "module":
        out["error"] = "Adventure source availability changed during retrieval."
        out["error_code"] = "campaign_sources_unavailable"
    elif manifest_error and state.get("source_mode") == "module":
        out["error"] = manifest_error
        out["error_code"] = "adventure_manifest_review_required"
    return out
