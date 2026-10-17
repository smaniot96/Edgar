"""User endpoints — Phase 10: first-run setup and settings.

GET  /api/users/me   -> 200 UserRead or 404 (no user yet)
POST /api/users      -> create user (email, optional display_name)
PATCH /api/users/me  -> update display_name
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.postgres.models import User

from ..auth import Auth, require_admin
from ..dependencies import get_session
from ..schemas.user import UserCreate, UserRead, UserUpdate

router = APIRouter(tags=["users"])


async def _current_user(db: AsyncSession, auth: Auth) -> User | None:
    """The caller in token mode; otherwise the first user in the DB (single-user seam, lowest id)."""
    if auth is not None:
        return await db.get(User, auth.user_id)
    result = await db.execute(select(User).order_by(User.id).limit(1))
    return result.scalar_one_or_none()


@router.get("/users/me", response_model=UserRead)
async def get_current_user(auth: Auth, db: AsyncSession = Depends(get_session)):
    """Return the current user or 404 if no user exists yet (first-run gate)."""
    user = await _current_user(db, auth)
    if user is None:
        raise HTTPException(status_code=404, detail="No user found")
    return UserRead.model_validate(user)


@router.post("/users", response_model=UserRead, status_code=201, dependencies=[Depends(require_admin)])
async def create_user(
    body: UserCreate, auth: Auth, db: AsyncSession = Depends(get_session)
):
    """Create the application user on first run (local mode).

    Hosted (token) mode: an admin creates accounts for other users; the single-user first-run
    gate does not apply.
    """
    if auth is None:
        existing = await _current_user(db, auth)
        if existing is not None:
            raise HTTPException(
                status_code=409, detail="A user already exists. Use PATCH /api/users/me to update."
            )
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Email already in use.")
    user = User(email=body.email, display_name=body.display_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)


@router.patch("/users/me", response_model=UserRead)
async def update_current_user(
    body: UserUpdate, auth: Auth, db: AsyncSession = Depends(get_session)
):
    user = await _current_user(db, auth)
    if user is None:
        raise HTTPException(status_code=404, detail="No user found")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return UserRead.model_validate(user)
