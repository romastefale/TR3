from __future__ import annotations

from aiogram.types import CallbackQuery, Message

# S1: OWNER_ID centralizado em app.config.settings (lê de env). Antes esse
# módulo hardcodava 8505890439, criando duas fontes da verdade — se o env
# var OWNER_ID fosse mudado no Railway, btb/moderation_tigrao/storage
# continuariam usando o ID antigo. Re-export pra manter API pública estável.
from app.config.settings import OWNER_ID

__all__ = ["OWNER_ID", "is_owner_user", "is_owner_private_message", "is_owner_callback"]


def is_owner_user(user_id: int | None) -> bool:
    return user_id == OWNER_ID


def is_owner_private_message(message: Message) -> bool:
    return bool(
        message.chat.type == "private"
        and message.from_user
        and is_owner_user(message.from_user.id)
    )


def is_owner_callback(callback: CallbackQuery) -> bool:
    return bool(callback.from_user and is_owner_user(callback.from_user.id))
