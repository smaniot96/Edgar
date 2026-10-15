"""Load the active combat row for a session and persist updates after a turn.

Convention: agent-state `combat_state` carries the DB row id when it has been persisted.
Derived/HUD keys (`initiative`, `current_turn`, `player_ac`, `second_wind_used`, `raging`) are rebuilt
from `combatants` on load (each combatant carries its `init`; the player carries `ac`,
`second_wind_used` and `raging`), so no extra columns are needed.
`persist_combat` uses that id to UPDATE the existing row; missing id means a new encounter
and we INSERT. The agent's combat subgraph spreads the existing dict on every update so the
id flows through unchanged.
"""

import copy
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.combatants import NPC_DISPOSITION, disposition_of
from db.postgres.models import NPC, CombatState, Session as SessionModel

from .story_state import persist_captive_fact

# Combat-owned NPC dispositions a player can act on after the fight (EDG-146).
CAPTIVE_NPC_DISPOSITIONS = frozenset({"captive", "released", "dead"})
_CAPTIVE_NPC_DISPOSITION = {"release": "released", "kill": "dead"}


async def load_active_combat(db: AsyncSession, session_id: int) -> dict | None:
    """Latest non-ended row for this session, returned as the dict the agent expects."""
    result = await db.execute(
        select(CombatState)
        .where(CombatState.session_id == session_id, CombatState.ended.is_(False))
        .order_by(CombatState.created_at.desc())
        .limit(1)
    )
    cs = result.scalar_one_or_none()
    if cs is None:
        return None
    combatants = cs.combatants or []
    player = next((c for c in combatants if c.get("is_player")), None) or {}
    out = {
        "id": cs.id,
        "initiative_order": cs.initiative_order,
        "round": cs.round,
        "current_turn_index": cs.current_turn_index,
        "ended": cs.ended,
        "combatants": combatants,
        "outcome": cs.outcome,
        # Per-encounter facts live on the player combatant (the table has no columns for them).
        "player_ac": player.get("ac"),
        "second_wind_used": bool(player.get("second_wind_used", False)),
        "raging": bool(player.get("raging", False)),
        "solo_budget": player.get("solo_budget"),
        # Additive (EDG-044): what the hero's last turn spent, read from the persisted player row.
        "economy": player.get("action_economy"),
    }
    if all("init" in c for c in combatants) and combatants:
        from agent.graph_combat import initiative_view

        out["initiative"] = initiative_view(combatants)
        out["current_turn"] = (
            None if cs.ended else (player.get("display_name") or player.get("name"))
        )
    return out


async def load_resolved_combat_outcomes(db: AsyncSession, session_id: int) -> list[dict]:
    """Return this session's durable engine outcomes, including actual encounter identities.

    Ended CombatState rows remain authoritative; no synthetic StoryRef or duplicate outcome
    ledger is created. The planner may use retained private source_reference identities;
    player serialization must use resolved_combat_outcomes_public instead.
    """
    rows = await db.scalars(
        select(CombatState)
        .where(CombatState.session_id == session_id, CombatState.ended.is_(True))
        .order_by(CombatState.id)
    )
    fields = {
        "name",
        "display_name",
        "creature_id",
        "is_player",
        "alive",
        "disposition",
        "source_reference",
    }
    return [
        {
            "id": row.id,
            "outcome": row.outcome,
            "round": row.round,
            "combatants": [
                {key: copy.deepcopy(value) for key, value in combatant.items() if key in fields}
                for combatant in row.combatants or []
            ],
        }
        for row in rows
    ]


def resolved_combat_outcomes_public(outcomes: list[dict]) -> list[dict]:
    """Known engine results and encountered roster labels; exclude private source bindings."""
    return [
        {
            "id": outcome["id"],
            "outcome": outcome["outcome"],
            "round": outcome["round"],
            "combatants": [
                {
                    key: copy.deepcopy(combatant[key])
                    for key in ("name", "display_name", "is_player", "alive", "disposition")
                    if key in combatant
                }
                for combatant in outcome.get("combatants", [])
            ],
        }
        for outcome in outcomes
    ]


async def persist_combat(
    db: AsyncSession, session_id: int, combat_state: dict | None
) -> int | None:
    """Insert or update the encounter row (caller commits). Returns the row id."""
    if not combat_state:
        return None
    if combat_state.get("id"):
        result = await db.execute(
            select(CombatState)
            .where(
                CombatState.id == combat_state["id"],
                CombatState.session_id == session_id,
            )
            .with_for_update()
        )
        cs = result.scalar_one()
        if cs.ended:
            fields = (
                "initiative_order",
                "round",
                "current_turn_index",
                "ended",
                "combatants",
                "outcome",
            )
            if any(combat_state.get(key, getattr(cs, key)) != getattr(cs, key) for key in fields):
                raise ValueError("A resolved combat outcome cannot be rewritten")
            return cs.id
        cs.initiative_order = combat_state.get("initiative_order", cs.initiative_order)
        cs.round = combat_state.get("round", cs.round)
        cs.current_turn_index = combat_state.get("current_turn_index", cs.current_turn_index)
        cs.ended = combat_state.get("ended", cs.ended)
        cs.combatants = combat_state.get("combatants", cs.combatants)
        cs.outcome = combat_state.get("outcome", cs.outcome)
        return cs.id
    row = CombatState(
        session_id=session_id,
        initiative_order=combat_state["initiative_order"],
        round=combat_state["round"],
        current_turn_index=combat_state.get("current_turn_index", 0),
        ended=combat_state.get("ended", False),
        combatants=combat_state.get("combatants", []),
        outcome=combat_state.get("outcome"),
    )
    db.add(row)
    await db.flush()
    return row.id


def _combat_npc_identity(combat_id: int, combatant_id: str) -> str:
    digest = hashlib.sha256(combatant_id.encode()).hexdigest()[:16]
    return f"combat:{combat_id}:{digest}"


async def persist_combat_npcs(
    db: AsyncSession,
    session_id: int,
    campaign_id: int,
    combat_id: int | None,
    combat_state: dict | None,
) -> None:
    """Record captives and dead foes of an ended encounter as session NPC rows (caller commits).

    Only rows owned by this encounter are touched (identity `combat:<id>:<hash>` inside the
    session), so manual campaign NPCs and story-effect NPCs are never overwritten. Defeated foes
    with an unknown cause are not recorded: no canonical death or capture is claimed for them.
    """
    if not combat_state or not combat_state.get("ended") or combat_id is None:
        return
    for c in combat_state.get("combatants") or []:
        if c.get("is_player"):
            continue
        npc_disposition = NPC_DISPOSITION.get(disposition_of(c))
        if npc_disposition is None:
            continue
        combatant_id = str(c.get("name") or "")
        identity = _combat_npc_identity(combat_id, combatant_id)
        stat_block = {
            "life_state": "dead" if npc_disposition == "dead" else "living",
            "source": "combat",
            "combat_id": combat_id,
            "combatant_id": combatant_id,
        }
        npc = await db.scalar(
            select(NPC).where(NPC.session_id == session_id, NPC.story_identity == identity)
        )
        if npc is None:
            db.add(
                NPC(
                    campaign_id=campaign_id,
                    session_id=session_id,
                    story_identity=identity,
                    name=str(c.get("display_name") or combatant_id)[:255],
                    disposition=npc_disposition,
                    stat_block=stat_block,
                )
            )
        else:
            npc.disposition = npc_disposition
            npc.stat_block = stat_block
    await db.flush()


async def load_combat_captives(db: AsyncSession, session_id: int) -> list[dict]:
    """Combat-owned captive, released and dead foes of this session, for the captive ruling.

    Only session rows whose identity is `combat:<id>:<hash>` qualify, so manual campaign NPCs and
    story-effect NPCs never reach the captive path.
    """
    rows = list(
        await db.scalars(
            select(NPC)
            .where(NPC.session_id == session_id, NPC.story_identity.like("combat:%"))
            .order_by(NPC.id)
        )
    )
    owned = [
        (npc, (npc.stat_block or {}).get("combat_id"), (npc.stat_block or {}).get("combatant_id"))
        for npc in rows
        if npc.disposition in CAPTIVE_NPC_DISPOSITIONS
    ]
    combat_ids = {cid for _, cid, _ in owned if isinstance(cid, int)}
    rounds = (
        dict(
            (
                await db.execute(
                    select(CombatState.id, CombatState.round).where(CombatState.id.in_(combat_ids))
                )
            ).all()
        )
        if combat_ids
        else {}
    )
    return [
        {
            "name": npc.name,
            "disposition": npc.disposition,
            "combat_id": cid,
            "combatant_id": combatant_id,
            "combat_round": rounds.get(cid),
        }
        for npc, cid, combatant_id in owned
        if isinstance(cid, int) and isinstance(combatant_id, str)
    ]


async def persist_captive_outcome(
    db: AsyncSession,
    game_session: SessionModel,
    outcome: dict,
    *,
    event_id: int,
    turn_id: str,
) -> None:
    """Apply an engine-decided release or kill to its combat-owned NPC row and the ledger.

    Caller commits. A replay that already holds the target state is a no-op; any other state
    other than `captive` is a stale plan and fails the whole turn.
    """
    action = outcome.get("action")
    if _CAPTIVE_NPC_DISPOSITION.get(action) != outcome.get("npc_disposition"):
        raise ValueError("Captive outcome is not a supported engine action")
    combat_id, combatant_id = outcome.get("combat_id"), outcome.get("combatant_id")
    if not isinstance(combat_id, int) or not isinstance(combatant_id, str):
        raise ValueError("Captive outcome has no encounter identity")
    npc = await db.scalar(
        select(NPC)
        .where(
            NPC.session_id == game_session.id,
            NPC.story_identity == _combat_npc_identity(combat_id, combatant_id),
        )
        .with_for_update()
    )
    if npc is None:
        raise ValueError("Captive record for this encounter is missing")
    target = outcome["npc_disposition"]
    if npc.disposition != target:
        if npc.disposition != "captive":
            raise ValueError("A captive outcome cannot be rewritten")
        npc.disposition = target
        npc.stat_block = {
            **(npc.stat_block or {}),
            "life_state": "dead" if action == "kill" else "living",
        }
    await persist_captive_fact(
        db,
        game_session,
        combat_id=combat_id,
        combatant_id=combatant_id,
        status="dead" if action == "kill" else "released",
        display_name=npc.name,
        combat_round=outcome.get("combat_round"),
        event_id=event_id,
        turn_id=turn_id,
    )
    await db.flush()
