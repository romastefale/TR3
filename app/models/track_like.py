from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class TrackLike(Base):
    __tablename__ = "track_likes"
    __table_args__ = (UniqueConstraint("user_id", "owner_user_id", "track_id", name="uq_user_owner_track_like"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    track_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    track_name: Mapped[str | None] = mapped_column(String, nullable=True)
    artist_name: Mapped[str | None] = mapped_column(String, nullable=True)
    liked: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
