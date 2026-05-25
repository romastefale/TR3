from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


from app.utils.datetime import utcnow_naive as _utcnow_naive  # noqa: F401


class TrackPlay(Base):
    __tablename__ = "track_plays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    track_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    track_name: Mapped[str | None] = mapped_column(String, nullable=True)
    artist_name: Mapped[str | None] = mapped_column(String, nullable=True)
    played_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
