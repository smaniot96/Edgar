"""One durable state authority for graph progress, artifacts, dispatches and paid outputs.

LangGraph has no second checkpoint/scheduler. Nodes transact accepted artifacts here and
are idempotent by dependency hash. SQLite lives on the existing campaign bind mount.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .. import adventures_meta

VERSION = "campaign-graph-2.0"


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def atomic_json(path: Path, value):
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NeedsAttention(ValueError):
    """A retained result needs a code fix or explicit reconciliation, never blind payment."""


class Cancelled(Exception):
    pass


class Store:
    def __init__(self, slug: str):
        self.slug = slug
        self.directory = adventures_meta.adventure_path(slug, "preparation-v2")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "graph.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact (name TEXT PRIMARY KEY, dependency TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task (id TEXT PRIMARY KEY, node TEXT NOT NULL, request TEXT NOT NULL, response TEXT, accepted TEXT, state TEXT NOT NULL, started REAL NOT NULL, finished REAL);
                CREATE TABLE IF NOT EXISTS transition (id INTEGER PRIMARY KEY, node TEXT NOT NULL, at REAL NOT NULL, dependency TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, request: dict):
        with self.db() as db:
            if db.execute("SELECT 1 FROM run").fetchone() is None:
                state = {
                    "version": VERSION,
                    "request": request,
                    "request_sha256": digest(request),
                    "status": "queued",
                    "node": "source",
                    "cancel_requested": False,
                    "created_at": time.time(),
                    "active_seconds": 0,
                    "error": None,
                }
                db.execute("INSERT INTO run VALUES(1,?)", (json.dumps(state),))
            elif self.state()["request_sha256"] != digest(request):
                raise NeedsAttention("Frozen preparation request changed.")

    def exists(self):
        with self.db() as db:
            return db.execute("SELECT 1 FROM run").fetchone() is not None

    def state(self):
        with self.db() as db:
            row = db.execute("SELECT state FROM run WHERE id=1").fetchone()
            return json.loads(row[0]) if row else {}

    def update(self, **changes):
        with self.db() as db:
            row = db.execute("SELECT state FROM run WHERE id=1").fetchone()
            state = json.loads(row[0])
            state.update(changes, updated_at=time.time())
            db.execute("UPDATE run SET state=? WHERE id=1", (json.dumps(state),))

    def check(self):
        if self.state().get("cancel_requested"):
            raise Cancelled()

    def enter(self, node, dependency=""):
        self.check()
        self.update(node=node, status="running")
        with self.db() as db:
            db.execute(
                "INSERT INTO transition(node,at,dependency) VALUES(?,?,?)",
                (node, time.time(), dependency),
            )

    def get(self, name, dependency=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM artifact WHERE name=?", (name,)).fetchone()
        if row is None or (dependency is not None and row["dependency"] != dependency):
            return None
        payload = json.loads(row["payload"])
        if digest(payload) != row["sha256"]:
            raise NeedsAttention("Retained artifact integrity check failed: " + name)
        return payload

    def put(self, name, payload, dependency=""):
        self.check()
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO artifact VALUES(?,?,?,?)",
                (name, dependency, json.dumps(payload, ensure_ascii=False), digest(payload)),
            )
        return payload

    def task(self, task_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def dispatch(self, task_id, node, request):
        self.check()
        with self.db() as db:
            db.execute(
                "INSERT INTO task VALUES(?,?,?,?,?,?,?,?)",
                (task_id, node, json.dumps(request), None, None, "dispatching", time.time(), None),
            )

    def response(self, task_id, response):
        with self.db() as db:
            db.execute(
                "UPDATE task SET response=?,state=?,finished=? WHERE id=?",
                (
                    json.dumps(response),
                    response.get("status", "unknown"),
                    time.time()
                    if response.get("status") not in ("queued", "in_progress")
                    else None,
                    task_id,
                ),
            )

    def accept(self, task_id, payload):
        self.check()
        with self.db() as db:
            db.execute(
                "UPDATE task SET accepted=?,state='accepted' WHERE id=?",
                (json.dumps(payload), task_id),
            )

    def usage(self):
        """Provider token measurements and published-rate estimates, never billed amounts."""
        # Requests retain page images and can exceed a gigabyte across a run.
        # Project accounting metadata inside SQLite so neither those inputs nor
        # accepted/output content enter Python's usage-reporting working set.
        with self.db() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    """SELECT id, node, state, started, finished,
                        json_extract(request, '$.model', '$.reasoning.effort',
                                     '$.service_tier') AS request_metadata,
                        json_type(request, '$.service_tier') AS request_tier_type,
                        CASE WHEN response IS NOT NULL AND response != '' THEN
                            json_extract(response, '$.id', '$.model', '$.service_tier', '$.usage')
                        END AS response_metadata
                       FROM task ORDER BY started"""
                )
            ]

        def token_count(value):
            return value if type(value) is int and value >= 0 else None

        calls, embedding_calls = [], []
        for row in rows:
            model, effort, tier = json.loads(row["request_metadata"])
            request = {"model": model, "reasoning": {"effort": effort}}
            if row["request_tier_type"] is not None:
                # A missing tier defaults to standard; an explicit null remains
                # unknown, matching the retained request's original semantics.
                request["service_tier"] = tier
            response = (
                dict(
                    zip(
                        ("id", "model", "service_tier", "usage"),
                        json.loads(row["response_metadata"]),
                        strict=True,
                    )
                )
                if row["response_metadata"]
                else {}
            )
            usage = response.get("usage") or {}
            model = response.get("model") or request.get("model")
            call = {
                "dispatch_id": row["id"],
                "node": row["node"],
                "response_id": response.get("id"),
                "model": model,
                "usage": usage,
                "usage_source": "provider_response" if usage else "unavailable",
                "cost_basis": "published_standard_rate_estimate",
                "billed_cost_microusd": None,
                "seconds": (row["finished"] or time.time()) - row["started"],
                "status": row["state"],
            }
            cost = lower_cost = upper_cost = None
            bounds_reason = None
            if row["node"] == "embedding":
                # Embeddings have input tokens only; the provider's total is equivalent
                # when an older receipt omits its more specific prompt-token field.
                ti = token_count(usage.get("prompt_tokens", usage.get("total_tokens")))
                if ti is not None and model == "text-embedding-3-small":
                    cost = lower_cost = upper_cost = (ti * 20 + 999) // 1000
                call.update(input_tokens=ti, total_tokens=token_count(usage.get("total_tokens")))
                destination = embedding_calls
            else:
                ti = token_count(usage.get("input_tokens"))
                to = token_count(usage.get("output_tokens"))
                details = usage.get("input_tokens_details") or {}
                cached = token_count(details.get("cached_tokens", 0))
                written = token_count(details.get("cache_write_tokens", 0))
                split = details.get("cache_write_tokens_details")
                # Cache write lifetimes are billed at different published rates. A
                # missing split leaves the exact estimate unknown, but valid measured
                # counts support bounds without assuming the request or provider TTL.
                has_split = isinstance(split, dict)
                short = token_count(split.get("ttl_30m_tokens")) if has_split else None
                long = token_count(split.get("ttl_12h_tokens")) if has_split else None
                if not has_split and written == 0:
                    short = long = 0
                counts_valid = (
                    all(v is not None for v in (ti, to, cached, written)) and cached + written <= ti
                )
                split_valid = short is not None and long is not None and short + long == written
                tier = response.get("service_tier") or request.get("service_tier", "default")
                long_context = ti is not None and ti > 272000
                if counts_valid and model == "gpt-6-luna" and tier in ("default", "standard"):
                    # Integer thousandths of microUSD per token. Cache writes replace
                    # ordinary input rates; cached reads and write counts are disjoint.
                    ordinary = ti - cached - written
                    input_factor = 2 if long_context else 1
                    fixed = input_factor * (ordinary * 100 + cached * 10)
                    fixed += to * (750 if long_context else 500)
                    if split_valid:
                        weighted = fixed + input_factor * (short * 125 + long * 250)
                        cost = lower_cost = upper_cost = (weighted + 999) // 1000
                    elif not has_split and split is None and written > 0:
                        lower_cost = (fixed + input_factor * written * 125 + 999) // 1000
                        upper_cost = (fixed + input_factor * written * 250 + 999) // 1000
                        bounds_reason = "cache_write_lifetime_unreported"
                call.update(
                    reasoning=(request.get("reasoning") or {}).get("effort"),
                    service_tier=tier,
                    input_tokens=ti,
                    output_tokens=to,
                    cached_input_tokens=cached,
                    cache_write_tokens=written,
                    cache_write_30m_tokens=short,
                    cache_write_12h_tokens=long,
                    long_context_rates=long_context,
                )
                destination = calls
            call.update(
                cost_microusd=cost,
                cost_estimate_microusd=cost,
                cost_estimate_lower_microusd=lower_cost,
                cost_estimate_upper_microusd=upper_cost,
                cost_bounds_reason=bounds_reason,
            )
            destination.append(call)

        semantic_cost = sum(c["cost_microusd"] or 0 for c in calls)
        embedding_cost = sum(c["cost_microusd"] or 0 for c in embedding_calls)
        unknown_semantic = sum(c["cost_microusd"] is None for c in calls)
        unknown_embedding = sum(c["cost_microusd"] is None for c in embedding_calls)
        unknown = unknown_semantic + unknown_embedding
        known_cost = semantic_cost + embedding_cost
        all_calls = calls + embedding_calls
        unbounded = sum(c["cost_estimate_lower_microusd"] is None for c in all_calls)
        known_lower = sum(c["cost_estimate_lower_microusd"] or 0 for c in all_calls)
        known_upper = sum(c["cost_estimate_upper_microusd"] or 0 for c in all_calls)
        state = self.state()
        return {
            "calls": calls,
            "call_count": len(calls),
            "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "embedding_calls": embedding_calls,
            "embedding_call_count": len(embedding_calls),
            "embedding_input_tokens": sum(c["input_tokens"] or 0 for c in embedding_calls),
            "semantic_cost_microusd": semantic_cost,
            "embedding_cost_microusd": embedding_cost,
            # Backward interface: this is the sum of KNOWN estimates, not a claim
            # that pending/ambiguous calls were free. Completeness must accompany it.
            "cost_microusd": known_cost,
            "known_cost_microusd": known_cost,
            "cost_estimate_microusd": known_cost,
            "billed_cost_microusd": None,
            "cost_basis": "published_standard_rate_estimate_known_usage_only",
            "cost_estimate_complete": unknown == 0,
            # A whole-run range exists only when every dispatch has valid measured
            # usage and a supported rate. Unknown paid work is never priced as zero.
            "cost_estimate_lower_microusd": known_lower if unbounded == 0 else None,
            "cost_estimate_upper_microusd": known_upper if unbounded == 0 else None,
            "known_cost_estimate_lower_microusd": known_lower,
            "known_cost_estimate_upper_microusd": known_upper,
            "cost_bounds_complete": unbounded == 0,
            "unbounded_calls": unbounded,
            "unknown_calls": unknown,
            "unknown_semantic_calls": unknown_semantic,
            "unknown_embedding_calls": unknown_embedding,
            "pricing": {
                "version": "openai-public-standard-2026-10-21",
                "verified_date": "2026-10-21",
                "currency": "USD",
                "rounding": "ceil to whole microUSD per call",
                "kind": "published_rate_estimate_not_billed",
                "semantic": {
                    "model": "gpt-6-luna",
                    "service_tier": "default",
                    "usd_per_million_tokens": {
                        "input": "0.10",
                        "cached_input": "0.01",
                        "cache_write_30m": "0.125",
                        "cache_write_12h": "0.25",
                        "output": "0.50",
                    },
                    "long_context_threshold_input_tokens": 272000,
                    "long_context_input_cache_multiplier": "2",
                    "long_context_output_multiplier": "1.5",
                    "sources": [
                        "https://developers.openai.com/api/docs/models/gpt-6-luna",
                        "https://developers.openai.com/api/docs/pricing",
                        "https://developers.openai.com/api/docs/guides/prompt-caching#monitor-cache-performance",
                    ],
                },
                "embedding": {
                    "model": "text-embedding-3-small",
                    "usd_per_million_tokens": "0.02",
                    "source": "https://developers.openai.com/api/docs/models/text-embedding-3-small",
                },
                "cache_write_bounds": "An unreported lifetime split uses the published 30-minute rate as the lower estimate and 12-hour rate as the upper estimate, without assuming a TTL.",
                "exclusions": "No invoice, account discount, tax or regional premium verification.",
            },
            "elapsed_seconds": (state.get("finished_at") or time.time())
            - state.get("created_at", time.time()),
        }
