from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.utils.datetime import utcnow_naive as _utcnow_naive


class ReactionAudit(Base):
    """Sprint X3: log efêmero de reactions p/ moderação (TTL 24h).

    Diferente de `track_reactions` (que correlaciona reactions ao
    catálogo /playing via card_messages), esta tabela registra QUALQUER
    reaction em QUALQUER mensagem do grupo onde o bot é admin. Serve
    como fonte pro painel rmod listar quem reagiu numa msg específica
    (eliminando a dependência de @username, que falha quando o bot
    nunca interagiu com o user).

    Retenção: 24h via purge_expired (chamado oportunisticamente pelo
    handler `on_message_reaction`). Sem scheduler dedicado.

    BigInteger em chat_id/message_id/user_id: IDs do Telegram já passam
    de 2^31 (canais e supergroups). Integer plain causaria overflow em
    Postgres.
    """

    __tablename__ = "reaction_audit"
    __table_args__ = (
        UniqueConstraint(
            "chat_id", "message_id", "user_id", "emoji",
            name="uq_reaction_audit_msg_user_emoji",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    user_name: Mapped[str | None] = mapped_column(String, nullable=True)
    user_username: Mapped[str | None] = mapped_column(String, nullable=True)
    emoji: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow_naive, index=True,
    )
