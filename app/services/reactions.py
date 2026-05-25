"""Sprint 8: reactions tracking pros cards /playing.

Substitui o sistema de likes-via-botão por reactions nativas do Telegram.
- register_card: chamado após enviar card /playing, mapeia message -> track.
- apply_reaction_change: chamado pelo handler @dp.message_reaction com diff.
- count_card_reactions: conta users únicos que reagiram (qualquer emoji).
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.models.card_message import CardMessage
from app.models.track_reaction import TrackReaction

logger = logging.getLogger(__name__)


class ReactionsService:
    async def register_card(
        self,
        chat_id: int,
        message_id: int,
        track_id: str,
        owner_user_id: int,
        track_name: str | None = None,
        artist_name: str | None = None,
    ) -> None:
        if not track_id:
            return
        with SessionLocal() as db:
            try:
                existing = db.execute(
                    select(CardMessage).where(
                        CardMessage.chat_id == chat_id,
                        CardMessage.message_id == message_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(
                        CardMessage(
                            chat_id=chat_id,
                            message_id=message_id,
                            track_id=track_id,
                            owner_user_id=owner_user_id,
                            track_name=track_name,
                            artist_name=artist_name,
                        )
                    )
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "CARD_REGISTER_FAILED chat=%s msg=%s track=%s",
                    chat_id, message_id, track_id,
                )

    async def resolve_card(self, chat_id: int, message_id: int) -> CardMessage | None:
        with SessionLocal() as db:
            return db.execute(
                select(CardMessage).where(
                    CardMessage.chat_id == chat_id,
                    CardMessage.message_id == message_id,
                )
            ).scalar_one_or_none()

    async def apply_reaction_change(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        old_emojis: list[str],
        new_emojis: list[str],
    ) -> None:
        """Diff reactions: insere novos, remove os retirados.

        Silenciosamente ignora se a mensagem não for um card trackado.
        """
        card = await self.resolve_card(chat_id, message_id)
        if card is None:
            return
        added = set(new_emojis) - set(old_emojis)
        removed = set(old_emojis) - set(new_emojis)
        if not added and not removed:
            return
        with SessionLocal() as db:
            try:
                for emoji in added:
                    try:
                        db.add(
                            TrackReaction(
                                chat_id=chat_id,
                                message_id=message_id,
                                user_id=user_id,
                                emoji=emoji,
                                track_id=card.track_id,
                                owner_user_id=card.owner_user_id,
                            )
                        )
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                if removed:
                    db.execute(
                        delete(TrackReaction).where(
                            TrackReaction.chat_id == chat_id,
                            TrackReaction.message_id == message_id,
                            TrackReaction.user_id == user_id,
                            TrackReaction.emoji.in_(removed),
                        )
                    )
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "REACTION_APPLY_FAILED chat=%s msg=%s user=%s",
                    chat_id, message_id, user_id,
                )

    async def count_card_reactions(self, chat_id: int, message_id: int) -> int:
        with SessionLocal() as db:
            return int(
                db.execute(
                    select(func.count(func.distinct(TrackReaction.user_id))).where(
                        TrackReaction.chat_id == chat_id,
                        TrackReaction.message_id == message_id,
                    )
                ).scalar_one()
            )

    async def count_track_reactions(self, track_id: str) -> int:
        """Total de reactions únicas (user, message) numa track ao longo de todos cards."""
        with SessionLocal() as db:
            return int(
                db.execute(
                    select(func.count(TrackReaction.id)).where(
                        TrackReaction.track_id == track_id,
                    )
                ).scalar_one()
            )


reactions_service = ReactionsService()
