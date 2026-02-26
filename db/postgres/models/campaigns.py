from db.postgres.base import Base
from sqlalchemy import String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    system: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
