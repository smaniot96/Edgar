"""Public campaign roster projection, batched for the campaign library."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.death import life_state_of
from db.postgres.models import Character, CharacterAssignment

from ..schemas.character import CampaignCharacterRead


def roster_row(assignment: CharacterAssignment, character: Character) -> CampaignCharacterRead:
    return CampaignCharacterRead(
        assignment_id=assignment.id,
        character_id=character.id,
        name=character.name,
        character_class=character.character_class,
        level=assignment.level,
        hp_current=assignment.hp_current,
        hp_max=assignment.hp_max,
        milestones_pending=assignment.milestones_pending,
        life_state=life_state_of(assignment.stats or {}),
    )


async def campaign_rosters(
    db: AsyncSession, campaign_ids: list[int]
) -> dict[int, list[CampaignCharacterRead]]:
    if not campaign_ids:
        return {}
    rows = await db.execute(
        select(CharacterAssignment, Character)
        .join(Character, Character.id == CharacterAssignment.character_id)
        .where(
            CharacterAssignment.campaign_id.in_(campaign_ids),
            CharacterAssignment.ended_at.is_(None),
        )
        .order_by(CharacterAssignment.id)
    )
    result: dict[int, list[CampaignCharacterRead]] = {identifier: [] for identifier in campaign_ids}
    for assignment, character in rows:
        result[assignment.campaign_id].append(roster_row(assignment, character))
    return result
