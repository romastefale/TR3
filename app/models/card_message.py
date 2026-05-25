from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class CardMessage(Base):
    """Sprint 8: mapeia (chat_id, message_id) de cards /playing -> track.

    Necessário pra resolver reactions (que vêm com chat/message_id mas
    não com track_id) de volta pra contar likes por música. Substitui
    o sistema de like-via-botão por reactions nativas do Telegram.
    """

    __tablename__ = "card_messages"

    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    track_name: Mapped[str | None] = mapped_column(String, nullable=True)
    artist_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow_naive)
