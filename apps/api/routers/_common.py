"""Small helpers shared by the CRUD routers (load-or-404, pagination, structured errors)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# List endpoints are paginated with limit/offset. The default is generous so clients that do
# not pass parameters (the UI fetches whole lists) keep seeing everything in practice.
DEFAULT_PAGE_SIZE = 200
MAX_PAGE_SIZE = 1000


class Page:
    """`Depends(Page)` -> validated `limit` / `offset` query parameters."""

    def __init__(
        self,
        limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Max rows"),
        offset: int = Query(0, ge=0, description="Rows to skip"),
    ) -> None:
        self.limit = limit
        self.offset = offset


async def get_or_404[ModelT](db: AsyncSession, model: type[ModelT], pk: int, name: str) -> ModelT:
    """Load `model` by primary key or raise 404 "<name> not found"."""
    obj = (await db.execute(select(model).where(model.id == pk))).scalar_one_or_none()  # type: ignore[attr-defined]
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return obj


class HTTPErrorWithContext(HTTPException):
    """HTTPException whose body is `{"detail": "<message>", **context}`.

    Keeps `detail` a plain string on every error response (so clients can always show it)
    while still returning machine-readable context such as a conflicting id.
    """

    def __init__(self, status_code: int, detail: str, **context: Any) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.context = context
