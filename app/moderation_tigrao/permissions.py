from __future__ import annotations

from aiogram.types import CallbackQuery, Message

OWNER_ID = 8505890439


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
