from pydantic import BaseModel


class AdventureRead(BaseModel):
    slug: str
    title: str
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None


class AdventureUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    level_range: str | None = None
    cover_image: str | None = None
