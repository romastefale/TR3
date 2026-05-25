from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class NewMemberWatch(Base):
    """Sprint X4: rastreia membros novos pra alerta de primeiras msgs com link.

    Inserido quando o bot vê uma service msg `new_chat_members` (alguém
    entrou no grupo). TTL 24h — após isso o user vira membro comum e
    pára de gerar alertas. Cap 5 alertas/user (1 por msg com link) pra
    não floodar o DM do owner.

    BigInteger por motivo idêntico ao reaction_audit (Sprint X3): IDs
    de Telegram passam de 2^31 em alguns canais/supergroups.
    """

    __tablename__ = "new_member_watch"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_new_member_watch_chat_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_name: Mapped[str | None] = mapped_column(String, nullable=True)
    user_username: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
    )
    alerts_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
