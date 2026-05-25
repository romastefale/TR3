from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class TrackReaction(Base):
    """Sprint 8: reactions individuais em cards /playing.

    Substitui o botão ♥ likes — agora users reagem nativamente no
    Telegram (qualquer emoji conta como "like"). Correlação reaction
    -> track passa pela tabela card_messages (chat_id+message_id).
    """

    __tablename__ = "track_reactions"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", "user_id", "emoji", name="uq_card_user_emoji"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    emoji: Mapped[str] = mapped_column(String, nullable=False)
    track_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
