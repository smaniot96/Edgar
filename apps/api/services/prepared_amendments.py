"""Session-owned persistence and deterministic replay for accepted prepared amendments.

Accepted amendments are private EventLog records written in the caller's narration
transaction. The reviewed campaign manifest and vector index are never changed. Replaying
records requires the exact same reviewed source and exact prepared digest chain.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.prepared_amendments import accept_runtime_amendment
from db.postgres.models import EventLog, Session as SessionModel
from edgar_core.manifests import require_published
from edgar_core.prepared import (
    PreparedAmendment,
    PreparedCampaign,
    prepared_digest,
    validate_prepared_amendment,
)

PREPARED_AMENDMENT_EVENT = "prepared_amendment"
_MAX_SESSION_AMENDMENTS = 64


class PreparedAmendmentConflict(ValueError):
    """A proposal or saved amendment no longer matches the current session binding."""


async def load_session_prepared_amendments(
    db: AsyncSession,
    session_id: int,
    campaign_id: int,
    manifests: dict[str, dict],
) -> tuple[dict[str, dict], list[dict]]:
    """Replay this session's additions over exact reviewed base definitions."""
    owner = await db.scalar(select(SessionModel.campaign_id).where(SessionModel.id == session_id))
    if owner != campaign_id:
        raise PreparedAmendmentConflict("Prepared session does not belong to this campaign")
    rows = await db.scalars(
        select(EventLog)
        .where(
            EventLog.session_id == session_id,
            EventLog.event_type == PREPARED_AMENDMENT_EVENT,
        )
        .order_by(EventLog.id)
    )
    events = [dict(row.payload or {}) for row in rows]
    if len(events) > _MAX_SESSION_AMENDMENTS:
        raise PreparedAmendmentConflict("Session prepared amendment limit was exceeded")

    bases: dict[str, PreparedCampaign] = {}
    current: dict[str, PreparedCampaign] = {}
    parsed_events: list[dict] = []
    for collection, raw_manifest in manifests.items():
        manifest = require_published(raw_manifest)
        if manifest.prepared is not None:
            base = PreparedCampaign.model_validate(manifest.prepared)
            bases[collection] = base
            current[collection] = base

    for payload in events:
        collection = payload.get("collection")
        if not isinstance(collection, str) or collection not in current:
            raise PreparedAmendmentConflict("Saved amendment references an unavailable source")
        manifest = require_published(manifests[collection])
        prepared = current[collection]
        amendment = PreparedAmendment.model_validate(payload.get("amendment"))
        if (
            payload.get("source_sha256") != manifest.source_sha256
            or payload.get("manifest_sha256") != manifest.reviewed_sha256
            or payload.get("base_prepared_sha256") != prepared_digest(prepared)
        ):
            raise PreparedAmendmentConflict("Saved amendment source or base version is stale")
        checked = validate_prepared_amendment(
            manifest,
            prepared,
            amendment,
            payload.get("committed_facts") or [],
            payload.get("completed_outcomes") or [],
        )
        if not checked.valid or checked.amended is None:
            raise PreparedAmendmentConflict("Saved amendment no longer validates")
        if payload.get("prepared_sha256") != prepared_digest(checked.amended):
            raise PreparedAmendmentConflict("Saved amendment result digest is invalid")
        current[collection] = checked.amended
        parsed_events.append(payload)

    overlays = {
        collection: prepared.model_dump(mode="json")
        for collection, prepared in current.items()
        if collection in bases and prepared_digest(prepared) != prepared_digest(bases[collection])
    }
    return overlays, parsed_events


def revalidate_runtime_amendment(
    receipt: dict | None,
    *,
    state: dict[str, Any],
    manifests: dict[str, dict],
) -> dict | None:
    """Re-run the engine gate against locked turn state before any event is appended."""
    if receipt is None:
        return None
    collection = receipt.get("collection")
    if not isinstance(collection, str) or collection not in manifests:
        raise PreparedAmendmentConflict("Accepted amendment source is no longer bound")
    manifest = require_published(manifests[collection])
    if (
        receipt.get("source_sha256") != manifest.source_sha256
        or receipt.get("manifest_sha256") != manifest.reviewed_sha256
    ):
        raise PreparedAmendmentConflict("Accepted amendment source version changed")
    candidate_state = {**state, "adventure_manifests": manifests, "manifest_status": "reviewed"}
    checked = accept_runtime_amendment(
        receipt.get("amendment"),
        candidate_state,
        in_combat=bool(state.get("combat_state") and not state["combat_state"].get("ended")),
    )
    if checked is None or checked != receipt:
        raise PreparedAmendmentConflict("Accepted amendment no longer matches engine state")
    return checked


async def append_session_prepared_amendment(
    db: AsyncSession,
    *,
    session_id: int,
    game_session: SessionModel,
    receipt: dict,
    turn_id: str,
) -> None:
    """Append the engine-sealed addition inside the caller's narration transaction."""
    locked = await db.scalar(
        select(SessionModel).where(SessionModel.id == session_id).with_for_update()
    )
    if locked is None or locked.campaign_id != game_session.campaign_id:
        raise PreparedAmendmentConflict("Prepared session ownership changed")
    event_count = await db.scalar(
        select(func.count(EventLog.id)).where(
            EventLog.session_id == session_id,
            EventLog.event_type == PREPARED_AMENDMENT_EVENT,
        )
    )
    if (event_count or 0) >= _MAX_SESSION_AMENDMENTS:
        raise PreparedAmendmentConflict("Session prepared amendment limit was reached")
    db.add(
        EventLog(
            session_id=session_id,
            event_type=PREPARED_AMENDMENT_EVENT,
            payload={**receipt, "turn_id": turn_id},
        )
    )


__all__ = [
    "PREPARED_AMENDMENT_EVENT",
    "PreparedAmendmentConflict",
    "append_session_prepared_amendment",
    "load_session_prepared_amendments",
    "revalidate_runtime_amendment",
]
