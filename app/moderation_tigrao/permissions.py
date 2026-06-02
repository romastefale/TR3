from __future__ import annotations

from aiogram.types import CallbackQuery, Message

from app.config.settings import MODERATOR_IDS, OWNER_ID, SECOND_MODERATOR_ID, THIRD_MODERATOR_ID

__all__ = [
    "OWNER_ID",
    "SECOND_MODERATOR_ID",
    "THIRD_MODERATOR_ID",
    "MODERATOR_IDS",
    "is_owner_user",
    "is_moderator_user",
    "is_owner_private_message",
    "is_owner_callback",
]


def is_owner_user(user_id: int | None) -> bool:
    """True somente para o OWNER configurado por ambiente."""
    return user_id == OWNER_ID


def is_moderator_user(user_id: int | None) -> bool:
    """True para qualquer moderador autorizado em MODERATOR_IDS."""
    return user_id in MODERATOR_IDS


def is_owner_private_message(message: Message) -> bool:
    """Autorização de acesso ao painel em DM.

    Nome mantido por estabilidade de API, mas libera qualquer moderador
    autorizado em MODERATOR_IDS.
    """
    return bool(
        message.chat.type == "private"
        and message.from_user
        and is_moderator_user(message.from_user.id)
    )


def is_owner_callback(callback: CallbackQuery) -> bool:
    """Autorização de callbacks do painel para MODERATOR_IDS."""
    return bool(callback.from_user and is_moderator_user(callback.from_user.id))
