#!/usr/bin/env python3
"""Load the recorded solo playthrough into the live DB so it's viewable in the UI.

Inserts each turn's (player_input, narration) from logs/transcript.jsonl as narration events
on the given session (default: the seeded Chalice session 1), ordered by created_at so the
chat history reads top-to-bottom. Run on the host: `uv run python claude-report/restore_playthrough.py`.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select

from db.postgres import session as db_session_module
from db.postgres.models import EventLog, Session as SessionModel

SESSION_ID = 1
LOG = Path(__file__).parent / "logs" / "transcript.jsonl"


async def main() -> None:
    rows = [json.loads(l) for l in LOG.read_text().splitlines()]
    async with db_session_module.async_session_factory() as db:
        s = (await db.execute(select(SessionModel).where(SessionModel.id == SESSION_ID))).scalar_one_or_none()
        if s is None:
            raise SystemExit(f"Session {SESSION_ID} not found — run POST /api/seed first.")
        # Clear any existing narration so re-runs are idempotent.
        await db.execute(
            delete(EventLog).where(
                EventLog.session_id == SESSION_ID, EventLog.event_type == "narration"
            )
        )
        base = datetime.now(timezone.utc) - timedelta(minutes=len(rows) + 1)
        for i, r in enumerate(rows):
            if r.get("error"):
                continue
            db.add(
                EventLog(
                    session_id=SESSION_ID,
                    event_type="narration",
                    payload={"player_input": r.get("input", ""), "narration": r.get("narration", "")},
                    created_at=base + timedelta(minutes=i),
                )
            )
        await db.commit()
    print(f"Restored {len(rows)} turns into session {SESSION_ID}. Open /ui/play/{SESSION_ID}")


if __name__ == "__main__":
    asyncio.run(main())
