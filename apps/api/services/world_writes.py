"""Apply adjudication outcomes to PostgreSQL within the API transaction."""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.models.adjudication import AdjudicationResult
from db.postgres.models import Character, Session as SessionModel, WorldFlag


async def apply_adjudication(
    db: AsyncSession,
    game_session: SessionModel,
    adj: AdjudicationResult,
) -> None:
    """Mutate session and related rows to reflect structured adjudication.

    Caller commits; use the same AsyncSession as the turn endpoint.
    """
    if adj.scene_id and adj.scene_id != game_session.current_scene_id:
        game_session.current_scene_id = adj.scene_id

    if adj.character_update is not None and game_session.active_character_id:
        cresult = await db.execute(
            select(Character).where(Character.id == game_session.active_character_id)
        )
        char = cresult.scalar_one_or_none()
        if char:
            cu = adj.character_update
            if cu.hp_delta is not None:
                new_hp = max(0, min(char.hp_max, char.hp_current + cu.hp_delta))
                char.hp_current = new_hp

            stats = dict(char.stats or {})
            conditions = list(stats.get("conditions", []))
            for name in cu.add_conditions:
                if name not in conditions:
                    conditions.append(name)
            for name in cu.remove_conditions:
                if name in conditions:
                    conditions.remove(name)
            stats["conditions"] = conditions
            char.stats = stats

            inv = dict(char.inventory or {})
            items = list(inv.get("items", []))
            for it in cu.inventory_add:
                items.append(it)
            for it in cu.inventory_remove:
                if it in items:
                    items.remove(it)
            inv["items"] = items
            char.inventory = inv

    for fu in adj.flags_set:
        existing = await db.execute(
            select(WorldFlag).where(
                WorldFlag.campaign_id == game_session.campaign_id,
                WorldFlag.key == fu.key,
            )
        )
        flag = existing.scalar_one_or_none()
        if flag:
            flag.value = fu.value
        else:
            db.add(
                WorldFlag(campaign_id=game_session.campaign_id, key=fu.key, value=fu.value)
            )

    if adj.flags_cleared:
        await db.execute(
            delete(WorldFlag).where(
                WorldFlag.campaign_id == game_session.campaign_id,
                WorldFlag.key.in_(adj.flags_cleared),
            )
        )
