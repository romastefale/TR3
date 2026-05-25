"""Sprint X4: watch de membros novos pra alertar primeiras msgs com link.

Fluxo:
- service msg `new_chat_members` → `register_join` (upsert idempotente).
- toda msg em grupo de user marcado → `consume_alert_slot` decide se ainda
  cabe alertar (joined_at < 24h E alerts_sent < 5). Se sim, incrementa e
  retorna info do membro pra preprocessor montar DM.
- purge oportunista (~1% das writes) limpa rows >24h.
"""
from __future__ import annotations

import logging
import random
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.new_member_watch import NewMemberWatch
from app.utils.datetime import utcnow_naive

logger = logging.getLogger(__name__)

WATCH_TTL_HOURS = 24
MAX_ALERTS_PER_MEMBER = 5


class NewMemberWatchService:
    def register_join(
        self,
        chat_id: int,
        user_id: int,
        user_name: str | None,
        user_username: str | None,
    ) -> None:
        """Marca user como 'novo' nesse chat. Idempotente — re-join não
        re-zera contador (UniqueConstraint + IntegrityError silencioso).
        """
        with SessionLocal() as db:
            try:
                db.add(
                    NewMemberWatch(
                        chat_id=chat_id,
                        user_id=user_id,
                        user_name=user_name,
                        user_username=user_username,
                    )
                )
                db.commit()
            except IntegrityError:
                db.rollback()
            except Exception:
                db.rollback()
                logger.exception(
                    "NEW_MEMBER_WATCH_REGISTER_FAILED chat=%s user=%s",
                    chat_id, user_id,
                )

    def consume_alert_slot(self, chat_id: int, user_id: int) -> dict | None:
        """Tenta consumir um slot de alerta. Retorna dict com info do membro
        (incl. alert_index 1..5) se ainda cabe alertar; None caso contrário.

        Caso None significa: user não é 'novo', janela 24h expirou, ou já
        foram emitidos 5 alertas.

        ATOMICIDADE: usa UPDATE ... SET alerts_sent=alerts_sent+1 WHERE
        alerts_sent < MAX em uma única statement. O check + incremento
        acontecem no DB sob o lock do row, eliminando race entre msgs
        simultâneas (architect Sprint X4 review). Se rowcount==1, o slot
        foi capturado por ESTA chamada — SELECT subsequente lê o valor
        já incrementado pra montar o `alert_index` correto.
        """
        cutoff = utcnow_naive() - timedelta(hours=WATCH_TTL_HOURS)
        with SessionLocal() as db:
            try:
                result = db.execute(
                    update(NewMemberWatch)
                    .where(
                        NewMemberWatch.chat_id == chat_id,
                        NewMemberWatch.user_id == user_id,
                        NewMemberWatch.joined_at >= cutoff,
                        NewMemberWatch.alerts_sent < MAX_ALERTS_PER_MEMBER,
                    )
                    .values(alerts_sent=NewMemberWatch.alerts_sent + 1)
                )
                rowcount = getattr(result, "rowcount", 0) or 0
                if rowcount < 1:
                    db.commit()
                    return None
                row = db.execute(
                    select(NewMemberWatch).where(
                        NewMemberWatch.chat_id == chat_id,
                        NewMemberWatch.user_id == user_id,
                    )
                ).scalar_one_or_none()
                db.commit()
                if row is None:
                    return None
                return {
                    "user_id": row.user_id,
                    "user_name": row.user_name,
                    "user_username": row.user_username,
                    "joined_at": row.joined_at,
                    "alert_index": row.alerts_sent,
                    "alert_max": MAX_ALERTS_PER_MEMBER,
                }
            except Exception:
                db.rollback()
                logger.exception(
                    "NEW_MEMBER_WATCH_CONSUME_FAILED chat=%s user=%s",
                    chat_id, user_id,
                )
                return None

    def purge_expired(self) -> int:
        cutoff = utcnow_naive() - timedelta(hours=WATCH_TTL_HOURS)
        with SessionLocal() as db:
            try:
                result = db.execute(
                    delete(NewMemberWatch).where(NewMemberWatch.joined_at < cutoff)
                )
                db.commit()
                count = getattr(result, "rowcount", 0) or 0
                if count:
                    logger.info("NEW_MEMBER_WATCH_PURGED rows=%d", count)
                return count
            except Exception:
                db.rollback()
                logger.exception("NEW_MEMBER_WATCH_PURGE_FAILED")
                return 0

    def maybe_purge(self) -> None:
        """Chamada oportunista (~1% das writes) pra evitar scheduler."""
        if random.random() < 0.01:
            try:
                self.purge_expired()
            except Exception:
                logger.exception("NEW_MEMBER_WATCH_OPPORTUNISTIC_PURGE_FAILED")


new_member_watch_service = NewMemberWatchService()
