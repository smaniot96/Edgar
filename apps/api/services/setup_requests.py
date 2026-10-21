"""Durable create retries for guided setup; no process-local cache or entity guessing.

The transaction advisory lock serializes a request key before lookup/create. Scoped unique
constraints remain the database backstop. The original payload digest survives later edits,
so a retry replays the same entity without mutating it or bypassing future play readiness.
"""

import hashlib
import json

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import Campaign, Character, Session


def creation_identity(body: BaseModel, *, context: dict | None = None) -> dict[str, str | None]:
    request_id = getattr(body, "request_id", None)
    if request_id is None:
        return {"creation_request_id": None, "creation_request_hash": None}
    value = body.model_dump(mode="json", exclude={"request_id"})
    # Optional party/name fields were added after campaign retry receipts shipped.
    # Keep the original digest for clients resuming their unchanged older request.
    if "new_campaign" in value:
        for key in ("title", "character_ids"):
            if value.get(key) is None:
                value.pop(key, None)
    if context is not None:
        value = {"request": value, "context": context}
    payload = json.dumps(
        value,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return {
        "creation_request_id": str(request_id),
        "creation_request_hash": hashlib.sha256(payload.encode()).hexdigest(),
    }


async def creation_replay[CreationModel: (Character, Session, Campaign)](
    db: AsyncSession, model: type[CreationModel], scope_id: int, body: BaseModel,
    *, context: dict | None = None,
) -> CreationModel | None:
    identity = creation_identity(body, context=context)
    key = identity["creation_request_id"]
    if key is None:
        return None
    lock_name = f"setup:{model.__tablename__}:{scope_id}:{key}"
    lock_id = int.from_bytes(hashlib.sha256(lock_name.encode()).digest()[:8], "big", signed=True)
    await db.execute(select(func.pg_advisory_xact_lock(lock_id)))
    scope = {
        Character: Character.owner_user_id,
        Session: Session.campaign_id,
        Campaign: Campaign.created_by,
    }[model]
    existing = await db.scalar(select(model).where(
        scope == scope_id, model.creation_request_id == key,
    ))
    if existing is not None and existing.creation_request_hash != identity["creation_request_hash"]:
        raise HTTPException(status_code=409, detail={
            "code": "validation_failed",
            "message": "This setup request was already used with different details. Resume the saved setup or start a new attempt.",
        })
    return existing
