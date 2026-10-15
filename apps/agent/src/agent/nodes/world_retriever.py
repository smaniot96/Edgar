"""Retrieve edition-filtered rules and reviewed active-scene passages concurrently.

Only player passages enter adventure_context/retrieved_context. Relevant private passages
go to planner_context. Missing manifests, unknown scenes and changed indexed spans fail
module play with an explicit review error; homebrew requires no module context.
"""

import asyncio
import logging

from agent.state import AgentState
from db.vector import search_rules_context
from db.vector.collections import effective_adventure_collection_names
from db.vector.retrieval import retrieve_active_scene
from db.vector.source_status import inspect_source
from edgar_core.manifests import ManifestError

log = logging.getLogger(__name__)

_LIMIT_PER_COLLECTION = 5
_MERGED_TOP_K = 20
_MAX_QUERY_LEN = 300


def build_retrieval_query(state: AgentState) -> str:
    player_input = state.get("player_input") or ""
    parsed = state.get("parsed_input")
    query = (getattr(parsed, "retrieval_query", None) or "").strip()
    if not query:
        query = player_input
        if parsed is not None and getattr(parsed, "target", None):
            query = f"{query} {parsed.target}"
    return query[:_MAX_QUERY_LEN]


async def world_retriever_node(state: AgentState) -> dict:
    player_input = state.get("player_input")
    adventure_collections = (
        [] if state.get("source_mode") == "homebrew" else state.get("adventure_collections")
    )

    if not player_input:
        return {"rules_context": [], "adventure_context": [], "retrieved_context": []}

    query = build_retrieval_query(state)
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
            search_rules_context,
            query,
            limit_per_collection=_LIMIT_PER_COLLECTION,
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
                extra={"bucket": name, "error": f"{type(res).__name__}: {res}"},
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
                buckets.append(list(res or []))
    rules_context, adventure_context = buckets

    out: dict = {
        "source_status": statuses or state.get("source_status", []),
        "retrieval_degraded": degraded,
        "rules_context": rules_context,
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
