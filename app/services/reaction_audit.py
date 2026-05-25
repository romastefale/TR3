"""Sprint X3: serviço de auditoria de reactions p/ painel rmod.

Recebe diffs do handler `@dp.message_reaction` (telegram.py) e mantém
um log com TTL de 24h. Expõe queries usadas pelos pickers do
painel rmod (listar quem reagiu numa msg ou no chat recentemente).
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from sqlalchemy import delete, desc, select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.reaction_audit import ReactionAudit
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

# TTL escolhido pelo owner: ele só consulta reactions recentes pra
# moderar, não precisa de histórico longo. Mantém tabela pequena.
TTL_HOURS = 24

# Limite de botões mostrados no picker (Telegram aceita até 100 buttons
# por inline keyboard, mas 30 já é o teto prático de UX em DM).
PICKER_LIMIT = 30


class ReactionAuditService:
    async def record_change(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        user_name: str | None,
        user_username: str | None,
        old_emojis: list[str],
        new_emojis: list[str],
    ) -> None:
        """Diff: insere emojis adicionados, remove os retirados.

        Idempotente via UniqueConstraint (chat, msg, user, emoji).
        Atualiza user_name/user_username em cada insert pra refletir
        renames (best-effort; só novos emojis disparam refresh).
        """
        added = set(new_emojis) - set(old_emojis)
        removed = set(old_emojis) - set(new_emojis)
        if not added and not removed:
            return
        with SessionLocal() as db:
            try:
                for emoji in added:
                    try:
                        db.add(
                            ReactionAudit(
                                chat_id=chat_id,
                                message_id=message_id,
                                user_id=user_id,
                                user_name=user_name,
                                user_username=user_username,
                                emoji=emoji,
                            )
                        )
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                if removed:
                    db.execute(
                        delete(ReactionAudit).where(
                            ReactionAudit.chat_id == chat_id,
                            ReactionAudit.message_id == message_id,
                            ReactionAudit.user_id == user_id,
                            ReactionAudit.emoji.in_(removed),
                        )
                    )
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "REACTION_AUDIT_FAILED chat=%s msg=%s user=%s",
                    chat_id, message_id, user_id,
                )
        # Purge oportunista (~1% das writes) — evita scheduler dedicado
        # e mantém tabela enxuta sem latência perceptível.
        if random.random() < 0.01:
            try:
                self.purge_expired()
            except Exception:
                logger.exception("REACTION_AUDIT_PURGE_FAILED")

    def list_message_reactors(
        self, chat_id: int, message_id: int,
    ) -> list[dict]:
        """Reactors únicos numa msg específica.

        Retorna [{user_id, user_name, user_username, emojis: [..]}].
        Filtra TTL (>24h ignorado mesmo se ainda na tabela).
        """
        cutoff = utcnow_naive() - timedelta(hours=TTL_HOURS)
        with SessionLocal() as db:
            rows = db.execute(
                select(ReactionAudit)
                .where(
                    ReactionAudit.chat_id == chat_id,
                    ReactionAudit.message_id == message_id,
                    ReactionAudit.created_at >= cutoff,
                )
                .order_by(desc(ReactionAudit.created_at))
            ).scalars().all()
        return self._group_by_user(rows)

    def list_chat_recent_reactors(
        self, chat_id: int, limit: int = PICKER_LIMIT,
    ) -> list[dict]:
        """Reactors únicos no chat nas últimas 24h, ordenados por
        atividade mais recente. Cap em `limit` users.
        """
        cutoff = utcnow_naive() - timedelta(hours=TTL_HOURS)
        with SessionLocal() as db:
            # Busca rows ordenadas; dedup por user_id em Python pra
            # manter ordem por "última reaction" sem subquery complexa.
            rows = db.execute(
                select(ReactionAudit)
                .where(
                    ReactionAudit.chat_id == chat_id,
                    ReactionAudit.created_at >= cutoff,
                )
                .order_by(desc(ReactionAudit.created_at))
                .limit(limit * 10)  # margem pra dedup
            ).scalars().all()
        grouped = self._group_by_user(rows)
        return grouped[:limit]

    def purge_expired(self) -> int:
        """Apaga rows com mais de TTL_HOURS. Retorna count apagado."""
        cutoff = utcnow_naive() - timedelta(hours=TTL_HOURS)
        with SessionLocal() as db:
            result = db.execute(
                delete(ReactionAudit).where(ReactionAudit.created_at < cutoff)
            )
            db.commit()
            count = getattr(result, "rowcount", 0) or 0
        if count:
            logger.info("REACTION_AUDIT_PURGED rows=%d", count)
        return count

    @staticmethod
    def _group_by_user(rows: list[ReactionAudit]) -> list[dict]:
        seen: dict[int, dict] = {}
        for r in rows:
            entry = seen.get(r.user_id)
            if entry is None:
                seen[r.user_id] = {
                    "user_id": r.user_id,
                    "user_name": r.user_name,
                    "user_username": r.user_username,
                    "emojis": [r.emoji],
                }
            else:
                if r.emoji not in entry["emojis"]:
                    entry["emojis"].append(r.emoji)
        return list(seen.values())


reaction_audit_service = ReactionAuditService()
